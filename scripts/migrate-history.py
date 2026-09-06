#!/usr/bin/env python3
"""Copy a stopped panel's SQLite history into a new bind-mount data directory.

Stop only the dashboard panel container before running this command. Leave the
passive collector and native UPS/NUT services running. This utility never stops
services, deletes the source database, or modifies its schema.
"""
import argparse
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time

CONTAINER_UID = 10001
CONTAINER_GID = 10001
TABLES = ('samples', 'events', 'meta')


def database_summary(connection):
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not set(TABLES) <= tables:
        raise ValueError('Source is not a panel history database: samples, events and meta are required.')
    version = connection.execute('PRAGMA user_version').fetchone()[0]
    if version not in (1, 2):
        raise ValueError(f'Unsupported history schema version: {version}')
    return {'schema_version': version,
            'rows': {table: connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in TABLES}}


def copy_database(source, destination, timeout):
    deadline = time.monotonic() + timeout

    def progress(status, remaining, total):
        if time.monotonic() >= deadline:
            raise TimeoutError('SQLite backup timed out. Confirm the panel is stopped and retry with a new destination.')

    # The SQLite backup API includes committed WAL transactions. Copying only
    # history.sqlite with a filesystem tool can silently omit those records.
    source.backup(destination, pages=256, progress=progress, sleep=.05)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def migrate_history(source, destination, *, uid=CONTAINER_UID, gid=CONTAINER_GID, timeout=60):
    """Retain source and create destination/history.sqlite; destination must be absent.

    The caller must have stopped the panel. uid/gid parameters permit isolated
    tests; the command-line interface always uses the container's UID/GID 10001.
    """
    source, destination = Path(source), Path(destination)
    if not source.is_absolute():
        raise ValueError('--source must be an absolute path to history.sqlite.')
    if source.is_symlink() or not stat.S_ISREG(source.stat().st_mode):
        raise ValueError('Source must be a regular database file, not a symlink or device.')
    source = source.resolve(strict=True)
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Destination already exists. Choose a new directory; no files were replaced.')
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Backup timeout must be positive.')
    if not destination.parent.is_dir():
        raise ValueError('Destination parent directory must already exist.')

    # mode=ro intentionally permits SQLite to read the WAL. immutable=1 would
    # bypass the WAL and is unsafe for this migration, even with a stopped panel.
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=5)) as source_db:
        source_db.execute('PRAGMA query_only=ON')
        source_db.execute('BEGIN')
        expected = database_summary(source_db)
        # Exclusive mkdir is the no-overwrite guard, including concurrent runs.
        # Keep it private until the complete, validated database is published.
        destination.mkdir(mode=0o700)
        temporary = None
        published = False
        target = destination / 'history.sqlite'
        try:
            fd, filename = tempfile.mkstemp(prefix='.history-', suffix='.sqlite', dir=destination)
            os.close(fd)
            temporary = Path(filename)
            with closing(sqlite3.connect(temporary)) as target_db:
                copy_database(source_db, target_db, timeout)
                # Deliver one self-contained database without WAL/SHM dependencies.
                mode = target_db.execute('PRAGMA journal_mode=DELETE').fetchone()[0]
                if mode.lower() != 'delete':
                    raise RuntimeError('Could not make the database backup self-contained.')
                if target_db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise RuntimeError('Database integrity check failed; source was retained.')
                actual = database_summary(target_db)
                if actual != expected:
                    raise RuntimeError('Database verification failed: source and backup summaries differ.')
            with temporary.open('rb') as data:
                os.fsync(data.fileno())
            temporary.chmod(0o640)
            os.chown(temporary, uid, gid)
            # A hard link publishes the complete file atomically and fails if
            # another file already has this name; it can never overwrite it.
            os.link(temporary, target)
            published = True
            temporary.unlink()
            os.chown(destination, uid, gid)
            destination.chmod(0o750)
            sync_directory(destination)
            sync_directory(destination.parent)
        except BaseException:
            # Remove only files created by this invocation, never other content
            # that might have appeared in the destination concurrently.
            if published:
                target.unlink(missing_ok=True)
            if temporary is not None:
                for path in (temporary, Path(str(temporary) + '-wal'), Path(str(temporary) + '-shm'),
                             Path(str(temporary) + '-journal')):
                    path.unlink(missing_ok=True)
            try:
                destination.rmdir()
            except OSError:
                pass
            raise

    digest = hashlib.sha256()
    with target.open('rb') as data:
        for chunk in iter(lambda: data.read(1024 * 1024), b''):
            digest.update(chunk)
    return {'source': str(source), 'destination': str(target), **actual,
            'sha256': digest.hexdigest(), 'uid': uid, 'gid': gid}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='Absolute path to the old history.sqlite')
    parser.add_argument('--destination', type=Path, required=True, help='New data directory, for example ./data (must not exist)')
    parser.add_argument('--panel-stopped', action='store_true', required=True,
                        help='Confirm the dashboard panel container has already been stopped; no service is stopped by this tool')
    parser.add_argument('--timeout', type=float, default=60, help='SQLite backup timeout in seconds (default: 60)')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run as root to set the new data directory ownership to UID/GID 10001.')
    try:
        result = migrate_history(args.source, args.destination, timeout=args.timeout)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        parser.exit(1, f'Migration failed: {exc}\nThe source database has been retained.\n')
    print(json.dumps(result, indent=2))
    print('Migration complete. Source retained; no services were stopped or started.')


if __name__ == '__main__':
    main()
