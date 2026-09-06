"""Public source checks reject accidental private commits without creating bundles."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('check_public_test', ROOT / 'scripts/check-public.py')
public = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(public)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(public, 'FILES', ('README.md',))
    monkeypatch.setattr(public, 'GLOBS', ())
    (tmp_path / 'README.md').write_text('Public project documentation.\n')
    return tmp_path


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result


def initialize_git(root):
    git(root, 'init', '--quiet')
    git(root, 'add', 'README.md')


def test_repository_allowlist_contains_runtime_inputs_and_excludes_private_paths():
    files = {path.relative_to(ROOT).as_posix() for path in public.public_files()}
    assert {'deploy/ugreen-ups-collector.service', 'deploy/ugreen-ups-panel.tmpfiles.conf',
            'scripts/collector-admin.py', 'scripts/install-collector.sh', 'scripts/migrate-history.py',
            'scripts/check-public.py', 'frontend/src/HistoryChart.tsx', 'frontend/src/assets/favicon.png',
            'docs/assets/dashboard-demo.png', 'THIRD_PARTY_NOTICES.txt'} <= files
    assert not any(name.startswith(('docs/research/', 'docs/power-investigation/', 'design/')) for name in files)
    assert '.env' not in files


def test_standalone_check_creates_no_artifacts(project):
    before = {path.name: path.read_bytes() for path in project.iterdir()}
    files, tracked = public.check_public(project)
    assert files == [project / 'README.md'] and tracked is None
    assert {path.name: path.read_bytes() for path in project.iterdir()} == before
    with pytest.raises(ValueError, match='requires a Git repository'):
        public.check_public(project, require_git=True)


def test_git_index_is_checked_but_untracked_local_configuration_is_not_included(project):
    initialize_git(project)
    (project / '.env').write_text('Local configuration, outside the public index.\n')
    files, tracked = public.check_public(project, require_git=True)
    assert files == [project / 'README.md'] and tracked == 1


@pytest.mark.parametrize('name', ['.env', 'docs/research/private-notes.md', 'frontend/src/assets/private-screen.png'])
def test_tracked_paths_outside_allowlist_fail_without_exposing_content(project, name):
    initialize_git(project)
    path = project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('private file contents must not appear in diagnostics')
    git(project, 'add', '-f', name)
    with pytest.raises(ValueError, match='outside the public allowlist') as error:
        public.check_public(project, require_git=True)
    assert name in str(error.value)
    assert path.read_text() not in str(error.value)


@pytest.mark.parametrize('content', ['ghp_' + 'x' * 24, '/'.join(('', 'home', 'private-user', 'notes')),
                                   '.'.join(('10', '10', '2', '123'))])
def test_private_text_is_rejected_without_echoing_it(project, content):
    (project / 'README.md').write_text(content)
    with pytest.raises(ValueError, match='Potential private material') as error:
        public.check_public(project)
    assert content not in str(error.value)


def test_staged_private_text_cannot_hide_behind_clean_worktree(project):
    initialize_git(project)
    private_text = 'github_pat_' + 'a' * 24
    (project / 'README.md').write_text(private_text)
    git(project, 'add', 'README.md')
    (project / 'README.md').write_text('Already cleaned in the working tree.\n')
    with pytest.raises(ValueError, match='Git index') as error:
        public.check_public(project, require_git=True)
    assert private_text not in str(error.value)


def test_parent_workspace_is_not_mistaken_for_the_project_repository(project):
    initialize_git(project)
    child = project / 'standalone'
    child.mkdir()
    (child / 'README.md').write_text('Standalone source.\n')
    assert public.check_public(child)[1] is None
    with pytest.raises(ValueError, match='requires a Git repository'):
        public.check_public(child, require_git=True)


def test_symlinked_approved_file_or_parent_is_rejected_before_reading(project, monkeypatch):
    outside = project / 'outside'
    outside.mkdir()
    (outside / 'README.md').write_text('External content.\n')
    (project / 'README.md').unlink()
    (project / 'README.md').symlink_to(outside / 'README.md')
    with pytest.raises(ValueError, match='symlinked'):
        public.check_public(project)
    monkeypatch.setattr(public, 'FILES', ('docs/README.md',))
    (project / 'docs').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='symlinked'):
        public.check_public(project)


def test_staged_symlink_is_rejected_even_when_worktree_is_regular(project):
    initialize_git(project)
    (project / 'README.md').unlink()
    (project / 'README.md').symlink_to('somewhere-else')
    git(project, 'add', 'README.md')
    (project / 'README.md').unlink()
    (project / 'README.md').write_text('Regular working-tree file.\n')
    with pytest.raises(ValueError, match='Unsupported Git entry'):
        public.check_public(project, require_git=True)


def test_approved_image_path_rejects_plain_text(project, monkeypatch):
    monkeypatch.setattr(public, 'FILES', ('logo.png',))
    (project / 'logo.png').write_text('A renamed text file is not an approved image.')
    with pytest.raises(ValueError, match='Invalid image'):
        public.check_public(project)


def test_frontend_runtime_notices_match_locked_versions():
    lock = json.loads((ROOT / 'frontend/package-lock.json').read_text())
    notices = (ROOT / 'THIRD_PARTY_NOTICES.txt').read_text()
    for path, metadata in lock['packages'].items():
        if not path or metadata.get('dev'):
            continue
        name = path.rsplit('node_modules/', 1)[1]
        assert f'Package: {name}@{metadata["version"]}' in notices
    assert 'Apache Software Foundation' in notices
