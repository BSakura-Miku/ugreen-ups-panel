"""History migration runs entirely on temporary SQLite files, without Docker/root."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).parents[1] / 'scripts' / 'migrate-history.py'
SPEC = importlib.util.spec_from_file_location('migrate_history_test', SCRIPT)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


def make_database(path, version=2, wal=False):
    connection = sqlite3.connect(path)
    if wal:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
    extra = ', context TEXT' if version == 2 else ''
    connection.execute(f'''CREATE TABLE samples (resolution INTEGER,bucket INTEGER,first REAL,last REAL,
        count INTEGER,mode TEXT,metrics TEXT{extra})''')
    connection.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,timestamp REAL,kind TEXT,detail TEXT)')
    connection.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
    row = [10, 100, 100, 102, 2, 'online', json.dumps({'soc': [200, 2, 100, 100]})]
    if version == 2:
        row.append('{"calibration_profile":"local-19v-v1"}')
    connection.execute(f'INSERT INTO samples VALUES({",".join("?" for _ in row)})', row)
    connection.execute('INSERT INTO events VALUES(1,100,?,?)', ('connection', 'online:online'))
    connection.execute('INSERT INTO meta VALUES(?,?)', ('last_ts', '102'))
    connection.execute(f'PRAGMA user_version={version}')
    connection.commit()
    return connection


def migrate(source, destination):
    # Real permission checks under the current test user, without chown to
    # another account. The production CLI always uses UID/GID 10001.
    return migration.migrate_history(source, destination, uid=os.geteuid(), gid=os.getegid())


def hash_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('version', [1, 2])
def test_consistent_copy_preserves_source_schema_records_and_permissions(tmp_path, version):
    source = tmp_path / 'old.sqlite'
    make_database(source, version).close()
    before = hash_file(source)
    source_mode = source.stat().st_mode
    target_dir = tmp_path / 'data'
    result = migrate(source, target_dir)
    target = target_dir / 'history.sqlite'
    assert hash_file(source) == before
    assert source.stat().st_mode == source_mode
    assert result['rows'] == {'samples': 1, 'events': 1, 'meta': 1}
    assert result['schema_version'] == version
    assert result['sha256'] == hash_file(target)
    with sqlite3.connect(target) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == version
        assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert db.execute('SELECT value FROM meta WHERE key="last_ts"').fetchone()[0] == '102'
    assert sorted(p.name for p in target_dir.iterdir()) == ['history.sqlite']
    assert target_dir.stat().st_mode & 0o777 == 0o750
    assert target.stat().st_mode & 0o777 == 0o640
    assert target.stat().st_uid == os.geteuid()


def test_backup_includes_committed_wal_without_changing_source_or_wal(tmp_path):
    source = tmp_path / 'old.sqlite'
    holder = make_database(source, wal=True)
    try:
        holder.execute('INSERT INTO events VALUES(2,104,?,?)', ('power', 'online:battery'))
        holder.commit()
        wal = Path(str(source) + '-wal')
        assert wal.stat().st_size > 0
        before, wal_before = hash_file(source), hash_file(wal)
        result = migrate(source, tmp_path / 'data')
        assert hash_file(source) == before and hash_file(wal) == wal_before
        assert result['rows']['events'] == 2
        with sqlite3.connect(tmp_path / 'data' / 'history.sqlite') as db:
            assert db.execute('SELECT detail FROM events ORDER BY id').fetchall() == [('online:online',), ('online:battery',)]
            assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert not list((tmp_path / 'data').glob('*-wal'))
        assert not list((tmp_path / 'data').glob('*-shm'))
    finally:
        holder.close()


@pytest.mark.parametrize('kind', ['empty', 'nonempty', 'file', 'symlink', 'dangling_symlink'])
def test_any_existing_destination_is_preserved(tmp_path, kind):
    source = tmp_path / 'old.sqlite'
    make_database(source).close()
    target = tmp_path / 'data'
    if kind in ('empty', 'nonempty'):
        target.mkdir()
        if kind == 'nonempty': (target / 'history.sqlite').write_text('existing data')
    elif kind == 'file': target.write_text('existing file')
    elif kind == 'symlink': target.symlink_to(source)
    else: target.symlink_to(tmp_path / 'missing')
    before = hash_file(source)
    with pytest.raises(FileExistsError):
        migrate(source, target)
    assert hash_file(source) == before
    if kind == 'nonempty': assert (target / 'history.sqlite').read_text() == 'existing data'
    if kind == 'file': assert target.read_text() == 'existing file'
    if kind.endswith('symlink'): assert target.is_symlink()


def test_failed_backup_cleans_its_partial_destination_and_retains_source(tmp_path, monkeypatch):
    source = tmp_path / 'old.sqlite'
    make_database(source).close()
    before = hash_file(source)
    target = tmp_path / 'data'

    def failing_backup(source_db, target_db, timeout):
        target_db.execute('CREATE TABLE incomplete(value INTEGER)')
        target_db.commit()
        raise RuntimeError('simulated copy failure')

    monkeypatch.setattr(migration, 'copy_database', failing_backup)
    with pytest.raises(RuntimeError, match='copy failure'):
        migrate(source, target)
    assert not target.exists()
    assert hash_file(source) == before


def test_cleanup_never_deletes_unrelated_content_created_concurrently(tmp_path, monkeypatch):
    source = tmp_path / 'old.sqlite'
    make_database(source).close()
    target = tmp_path / 'data'

    def failing_backup(source_db, target_db, timeout):
        (target / 'unrelated.txt').write_text('preserve this')
        raise RuntimeError('simulated copy failure')

    monkeypatch.setattr(migration, 'copy_database', failing_backup)
    with pytest.raises(RuntimeError):
        migrate(source, target)
    assert (target / 'unrelated.txt').read_text() == 'preserve this'
    assert [p.name for p in target.iterdir()] == ['unrelated.txt']


def test_permission_failure_leaves_no_ready_database(tmp_path, monkeypatch):
    source = tmp_path / 'old.sqlite'
    make_database(source).close()

    def denied(*args, **kwargs):
        raise PermissionError('cannot set ownership')

    monkeypatch.setattr(migration.os, 'chown', denied)
    with pytest.raises(PermissionError):
        migrate(source, tmp_path / 'data')
    assert not (tmp_path / 'data').exists()
    assert source.is_file()


def test_invalid_or_missing_source_does_not_create_destination(tmp_path):
    target = tmp_path / 'data'
    with pytest.raises(ValueError, match='absolute'):
        migrate(Path('old.sqlite'), target)
    with pytest.raises(FileNotFoundError):
        migrate(tmp_path / 'missing.sqlite', target)
    invalid = tmp_path / 'invalid.sqlite'
    invalid.write_text('not a database')
    with pytest.raises(sqlite3.DatabaseError):
        migrate(invalid, target)
    empty = tmp_path / 'empty.sqlite'
    sqlite3.connect(empty).close()
    with pytest.raises(ValueError, match='not a panel'):
        migrate(empty, target)
    assert not target.exists()


def test_source_symlink_is_rejected(tmp_path):
    source = tmp_path / 'old.sqlite'
    make_database(source).close()
    link = tmp_path / 'alias.sqlite'
    link.symlink_to(source)
    with pytest.raises(ValueError, match='symlink'):
        migrate(link, tmp_path / 'data')
    assert not (tmp_path / 'data').exists()


def test_cli_requires_stopped_panel_acknowledgement_without_service_calls(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), '--source', str(tmp_path / 'old.sqlite'),
                             '--destination', str(tmp_path / 'data')], capture_output=True, text=True)
    assert result.returncode == 2
    assert '--panel-stopped' in result.stderr
    help_result = subprocess.run([sys.executable, str(SCRIPT), '--help'], capture_output=True, text=True)
    assert help_result.returncode == 0
    help_text = ' '.join(help_result.stdout.split())
    assert 'never stops' in help_text
    assert 'UPS/NUT services running' in help_text


def test_production_ownership_defaults_are_container_uid_and_gid():
    assert migration.migrate_history.__kwdefaults__['uid'] == 10001
    assert migration.migrate_history.__kwdefaults__['gid'] == 10001


def test_database_appearing_during_copy_is_never_replaced(tmp_path, monkeypatch):
    source = tmp_path / 'old.sqlite'
    make_database(source).close()
    destination = tmp_path / 'data'
    original = migration.copy_database

    def collision(source_db, target_db, timeout):
        original(source_db, target_db, timeout)
        (destination / 'history.sqlite').write_text('keep concurrent database')

    monkeypatch.setattr(migration, 'copy_database', collision)
    with pytest.raises(FileExistsError):
        migrate(source, destination)
    assert (destination / 'history.sqlite').read_text() == 'keep concurrent database'
    assert [p.name for p in destination.iterdir()] == ['history.sqlite']


@pytest.mark.parametrize('timeout', [0, -1, float('inf'), float('nan')])
def test_invalid_timeout_cannot_start_unbounded_backup(tmp_path, timeout):
    source = tmp_path / 'old.sqlite'
    make_database(source).close()
    with pytest.raises(ValueError, match='timeout'):
        migration.migrate_history(source, tmp_path / 'data', timeout=timeout)
    assert not (tmp_path / 'data').exists()
