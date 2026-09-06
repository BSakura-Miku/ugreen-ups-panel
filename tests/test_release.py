"""Check that public exports are installable and never replace an existing archive."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('prepare_release', ROOT / 'scripts/prepare-release.py')
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def test_public_release_contains_deployment_inputs_and_excludes_private_files():
    files = {path.relative_to(ROOT).as_posix() for path in release.public_files()}
    assert {'deploy/ugreen-ups-collector.service', 'deploy/ugreen-ups-panel.tmpfiles.conf',
            'scripts/collector-admin.py', 'scripts/install-collector.sh',
            'frontend/src/HistoryChart.tsx', 'frontend/src/assets/favicon.png',
            'docs/assets/dashboard-demo.png', 'THIRD_PARTY_NOTICES.txt'} <= files
    assert not any(name.startswith(('docs/research/', 'docs/power-investigation/', 'design/')) for name in files)
    assert '.env' not in files


def test_release_version_directory_does_not_overwrite_existing_archive(tmp_path):
    destination = tmp_path / 'ugreen-ups-panel-0.3.0'
    archive = Path(str(destination) + '.tar.gz')
    archive.write_bytes(b'existing archive')
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/prepare-release.py'),
                             '--output', str(destination)], capture_output=True, text=True)
    assert result.returncode != 0
    assert archive.read_bytes() == b'existing archive'
    assert not destination.exists()


def test_frontend_runtime_notices_match_locked_versions():
    lock = json.loads((ROOT / 'frontend/package-lock.json').read_text())
    notices = (ROOT / 'THIRD_PARTY_NOTICES.txt').read_text()
    for path, metadata in lock['packages'].items():
        if not path or metadata.get('dev'):
            continue
        name = path.rsplit('node_modules/', 1)[1]
        assert f'Package: {name}@{metadata["version"]}' in notices
    assert 'Apache Software Foundation' in notices
