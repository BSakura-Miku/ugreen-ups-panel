"""Validate prebuilt-image deployment, optional source builds and host CLI dependencies."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_collector_and_admin_commands_need_only_standard_library():
    commands = [('-m', 'ups_panel.collector'), ('scripts/collector-admin.py',)]
    for args in commands:
        result = subprocess.run([sys.executable, '-S', *args, '--help'], cwd=ROOT,
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    for script in (ROOT / 'scripts').glob('*.sh'):
        result = subprocess.run(['sh', '-n', str(script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def compose_config(directory, files):
    executable = shutil.which('docker')
    if executable is None:
        pytest.skip('Docker CLI is unavailable; repository CI validates Compose on Ubuntu')
    version = subprocess.run([executable, 'compose', 'version'], capture_output=True, text=True)
    if version.returncode:
        pytest.skip('Docker Compose plugin is unavailable')
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('UPS_', 'COMPOSE_'))}
    command = [executable, 'compose', '--project-directory', str(directory)]
    for filename in files:
        command += ['-f', str(directory / filename)]
    command += ['config', '--format', 'json']
    result = subprocess.run(command, cwd=directory, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_production_compose_uses_prebuilt_image_and_protected_bind_mounts(tmp_path):
    shutil.copyfile(ROOT / 'compose.yaml', tmp_path / 'compose.yaml')
    configuration = compose_config(tmp_path, ['compose.yaml'])
    panel = configuration['services']['panel']
    assert panel['image'] == 'bsakuramiku/ugreen-ups-panel:latest'
    assert 'build' not in panel
    assert configuration['name'] == 'ugreen-ups-panel'
    assert panel['ports'][0].get('host_ip', '0.0.0.0') == '0.0.0.0'
    assert str(panel['ports'][0]['published']) == '9086'
    mounts = {entry['target']: entry for entry in panel['volumes']}
    assert mounts['/capture']['read_only'] is True
    assert mounts['/capture']['source'] == '/run/ugreen-ups-panel'
    assert mounts['/data']['source'] == str(tmp_path / 'data')
    assert mounts['/data']['type'] == mounts['/capture']['type'] == 'bind'
    assert mounts['/data'].get('bind', {}).get('create_host_path', False) is False
    assert mounts['/capture'].get('bind', {}).get('create_host_path', False) is False
    assert panel['read_only'] is True and panel['cap_drop'] == ['ALL']
    assert not panel.get('privileged') and not panel.get('devices')
    assert not (tmp_path / 'data').exists()


def test_old_env_variables_do_not_change_the_simple_compose_defaults(tmp_path):
    shutil.copyfile(ROOT / 'compose.yaml', tmp_path / 'compose.yaml')
    (tmp_path / '.env').write_text('UPS_IMAGE=unused:old\nUPS_BIND_IP=127.0.0.1\n'
                                  'UPS_PORT=1234\nUPS_DATA_DIR=./old-data\nTZ=UTC\n')
    panel = compose_config(tmp_path, ['compose.yaml'])['services']['panel']
    assert panel['image'] == 'bsakuramiku/ugreen-ups-panel:latest'
    assert str(panel['ports'][0]['published']) == '9086'
    assert panel['environment']['TZ'] == 'Asia/Shanghai'
    data = next(mount for mount in panel['volumes'] if mount['target'] == '/data')
    assert data['source'] == str(tmp_path / 'data')


def test_source_build_override_is_separate_from_production_compose(tmp_path):
    for name in ('compose.yaml', 'compose.build.yaml'):
        shutil.copyfile(ROOT / name, tmp_path / name)
    panel = compose_config(tmp_path, ['compose.yaml', 'compose.build.yaml'])['services']['panel']
    assert panel['image'] == 'ugreen-ups-panel:local'
    assert panel['pull_policy'] == 'never'
    assert panel['build']['context'] == str(tmp_path)
