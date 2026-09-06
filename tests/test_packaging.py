"""Verify the downloadable deployment bundle independently of the source tree."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def bundle(tmp_path_factory):
    workspace = tmp_path_factory.mktemp('deployment-bundle')
    staged = workspace / 'deployment'
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/prepare-deployment.py'),
                             '--output', str(staged)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return staged, Path(str(staged) + '.tar.gz'), workspace


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_files(staged):
    entries = {}
    for line in (staged / 'DEPLOYMENT_MANIFEST.sha256').read_text().splitlines():
        checksum, name = line.split('  ', 1)
        assert name not in entries
        assert not PurePosixPath(name).is_absolute() and '..' not in PurePosixPath(name).parts
        assert digest(staged / name) == checksum
        entries[name] = checksum
    return entries


def test_archive_and_manifest_cover_exactly_the_distributable_files(bundle):
    staged, archive, _ = bundle
    checksum, filename = Path(str(archive) + '.sha256').read_text().strip().split('  ', 1)
    assert filename == archive.name and checksum == digest(archive)
    entries = manifest_files(staged)
    with tarfile.open(archive, 'r:gz') as stream:
        members = stream.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            assert not path.is_absolute() and '..' not in path.parts
            assert path.parts[0] == 'ugreen-ups-panel'
            assert member.isdir() or member.isfile()
            assert (member.uid, member.gid, member.uname, member.gname, member.mtime) == (0, 0, '', '', 0)
        files = {PurePosixPath(m.name).relative_to('ugreen-ups-panel').as_posix(): m
                 for m in members if m.isfile()}
        assert set(files) == set(entries) | {'DEPLOYMENT_MANIFEST.sha256'}
        for name, checksum in entries.items():
            assert hashlib.sha256(stream.extractfile(files[name]).read()).hexdigest() == checksum
            assert files[name].mode == (0o755 if name.startswith('scripts/') else 0o644)


def test_bundle_is_runtime_only_and_excludes_private_working_data(bundle):
    staged, _, _ = bundle
    files = set(manifest_files(staged))
    assert {'compose.yaml', '.env.example', 'scripts/collector-admin.py', 'scripts/migrate-history.py',
            'scripts/install-collector.sh', 'scripts/rollback-collector.sh', 'scripts/uninstall-collector.sh',
            'deploy/ugreen-ups-collector.service', 'deploy/ugreen-ups-panel.tmpfiles.conf',
            'ups_panel/__init__.py', 'ups_panel/collector.py', 'ups_panel/protocol.py',
            'ups_panel/power.py', 'ups_panel/usbmon.py', 'README.md', 'README.en.md',
            'docs/assets/dashboard-demo.png', 'frontend/src/assets/us3000-logo.png'} <= files
    assert not {'Dockerfile', 'compose.build.yaml', '.env', 'ups_panel/app.py',
                'frontend/package.json', 'frontend/package-lock.json'} & files
    assert not any(name.startswith(('tests/', 'node_modules/', '.git/', '.github/', 'data/',
                                    'runtime/', 'docs/research/', 'docs/power-investigation/')) for name in files)
    assert not any(name.endswith(('.sqlite', '.sqlite-wal', '.sqlite-shm', '.jsonl')) for name in files)


def test_extracted_collector_and_admin_commands_need_only_standard_library(bundle):
    _, archive, workspace = bundle
    extracted = workspace / 'unpacked'
    with tarfile.open(archive, 'r:gz') as stream:
        stream.extractall(extracted, filter='data')
    package = extracted / 'ugreen-ups-panel'
    commands = [('-m', 'ups_panel.collector'), ('scripts/collector-admin.py',),
                ('scripts/migrate-history.py',)]
    for args in commands:
        result = subprocess.run([sys.executable, '-S', *args, '--help'], cwd=package,
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    assert 'UPS_NUT_TARGET' in (package / 'ups_panel/collector.py').read_text()
    for script in (package / 'scripts').glob('*.sh'):
        result = subprocess.run(['sh', '-n', str(script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def compose_config(directory, files):
    executable = shutil.which('docker')
    if executable is None:
        pytest.skip('Docker CLI is unavailable; GitHub CI validates Compose on Ubuntu')
    version = subprocess.run([executable, 'compose', 'version'], capture_output=True, text=True)
    if version.returncode:
        pytest.skip('Docker Compose plugin is unavailable')
    # Ignore developer-specific Compose settings and select the files explicitly.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('UPS_', 'COMPOSE_'))}
    command = [executable, 'compose', '--project-directory', str(directory)]
    for filename in files:
        command += ['-f', str(directory / filename)]
    command += ['config', '--format', 'json']
    result = subprocess.run(command, cwd=directory, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_minimal_deployment_compose_needs_no_build_context_and_mounts_are_explicit(bundle):
    staged, _, _ = bundle
    configuration = compose_config(staged, ['compose.yaml'])
    panel = configuration['services']['panel']
    version = json.loads((ROOT / 'frontend/package.json').read_text())['version']
    assert panel['image'] == f'bsakuramiku/ugreen-ups-panel:{version}'
    assert 'build' not in panel
    assert configuration['name'] == 'ugreen-ups-panel'
    assert panel['ports'][0]['host_ip'] == '127.0.0.1'
    mounts = {entry['target']: entry for entry in panel['volumes']}
    assert mounts['/capture']['read_only'] is True
    assert mounts['/capture']['source'] == '/run/ugreen-ups-panel'
    assert mounts['/data']['source'] == str(staged / 'data')
    assert mounts['/data']['type'] == mounts['/capture']['type'] == 'bind'
    # Compose may omit false values after normalization. Missing stays false.
    assert mounts['/data'].get('bind', {}).get('create_host_path', False) is False
    assert mounts['/capture'].get('bind', {}).get('create_host_path', False) is False
    assert panel['read_only'] is True and panel['cap_drop'] == ['ALL']
    assert not panel.get('privileged') and not panel.get('devices')
    assert not (staged / 'data').exists()


def test_source_build_override_is_separate_from_production_compose(tmp_path):
    # Resolve an isolated pair so a local .env cannot affect this validation.
    for name in ('compose.yaml', 'compose.build.yaml'):
        shutil.copyfile(ROOT / name, tmp_path / name)
    panel = compose_config(tmp_path, ['compose.yaml', 'compose.build.yaml'])['services']['panel']
    assert panel['image'] == 'ugreen-ups-panel:local'
    assert panel['pull_policy'] == 'never'
    assert panel['build']['context'] == str(tmp_path)


@pytest.mark.parametrize('existing', ['directory', 'archive', 'checksum', 'dangling_symlink'])
def test_deployment_packaging_never_overwrites_prior_outputs(tmp_path, existing):
    output = tmp_path / 'deployment'
    if existing == 'directory':
        occupied = output
        occupied.mkdir()
        (occupied / 'keep.txt').write_text('existing deployment')
    elif existing == 'archive':
        occupied = Path(str(output) + '.tar.gz')
        occupied.write_bytes(b'existing archive')
    elif existing == 'checksum':
        occupied = Path(str(output) + '.tar.gz.sha256')
        occupied.write_bytes(b'existing checksum')
    else:
        occupied = output
        occupied.symlink_to(tmp_path / 'missing')
    before = occupied.read_bytes() if occupied.is_file() else None
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/prepare-deployment.py'),
                             '--output', str(output)], capture_output=True, text=True)
    assert result.returncode != 0
    if before is not None:
        assert occupied.read_bytes() == before
    elif existing == 'directory':
        assert (occupied / 'keep.txt').read_text() == 'existing deployment'
    else:
        assert occupied.is_symlink()
