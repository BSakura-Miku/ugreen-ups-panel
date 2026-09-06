#!/usr/bin/env python3
"""Check approved public files and the Git index without creating release artifacts."""
import argparse
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    '.gitignore', '.dockerignore', '.github/workflows/ci.yml',
    'LICENSE', 'THIRD_PARTY_NOTICES.txt', 'README.md', 'README.en.md', 'CHANGELOG.md', 'CONTRIBUTING.md', 'SECURITY.md',
    'Dockerfile', 'docker-compose.yaml', 'compose.build.yaml', 'requirements.txt', 'requirements.lock', 'requirements-dev.txt',
    'deploy/ugreen-ups-collector.service', 'deploy/ugreen-ups-panel.tmpfiles.conf',
    'docs/fields.md', 'docs/calibration.md', 'docs/architecture.md', 'docs/hardware.md',
    'docs/validation.md', 'docs/assets/dashboard-demo.png',
    'docs/assets/ugos-pro-enable-ssh.png', 'docs/assets/ugos-pro-docker-project.png',
    'scripts/install-collector.sh', 'scripts/rollback-collector.sh', 'scripts/uninstall-collector.sh',
    'scripts/collector-admin.py', 'scripts/check-public.py',
    'scripts/update-third-party-notices.py', 'scripts/smoke-image.py',
    'frontend/package.json', 'frontend/package-lock.json', 'frontend/tsconfig.json',
    'frontend/vite.config.ts', 'frontend/index.html',
    'frontend/src/assets/us3000-logo.png', 'frontend/src/assets/favicon.png',
    'frontend/src/assets/apple-touch-icon.png',
)
GLOBS = ('ups_panel/*.py', 'tests/test_*.py', 'fixtures/*.hex', 'frontend/src/**/*.ts',
         'frontend/src/**/*.tsx', 'frontend/src/**/*.css', 'frontend/tests/*.test.mjs')
PATTERNS = (
    re.compile(r'/(?:Users|home)/[A-Za-z0-9_.-]+/'),
    re.compile(r'\b10\.10\.2\.\d+\b'),
    re.compile(r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----'),
    re.compile(r'\b(?:ghp|github_pat|sk_live)_[A-Za-z0-9_]{20,}\b'),
    re.compile(r'eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}'),
)


def validate_content(name, content, location='working tree'):
    if name.endswith('.png'):
        # The existing dashboard screenshot has a .png name but JPEG bytes.
        if not content.startswith((b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff')):
            raise ValueError(f'Invalid image asset: {name} ({location})')
        return
    try:
        text = content.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError(f'Expected UTF-8 public text: {name} ({location})') from None
    if any(pattern.search(text) for pattern in PATTERNS):
        raise ValueError(f'Potential private material: {name} ({location}; review locally)')


def public_files(root=ROOT):
    root = Path(root).resolve()
    paths = {root / name for name in FILES}
    for pattern in GLOBS:
        paths.update(root.glob(pattern))
    for path in sorted(paths):
        relative = path.relative_to(root)
        if (path.is_symlink() or not path.is_file()
                or any((root / parent).is_symlink() for parent in relative.parents if parent != Path('.'))):
            raise ValueError(f'Missing or symlinked public file: {relative.as_posix()}')
        validate_content(relative.as_posix(), path.read_bytes())
    return sorted(paths)


def git_index(root):
    """Return index entries only for this repository, never a containing workspace."""
    try:
        result = subprocess.run(['git', '-C', str(root), 'rev-parse', '--show-toplevel'],
                                capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        if (root / '.git').exists():
            raise RuntimeError('Git is required to inspect this repository index.') from None
        return None
    if result.returncode:
        if (root / '.git').exists():
            raise RuntimeError('Unable to inspect the repository index.')
        return None
    if Path(result.stdout.strip()).resolve() != root:
        return None
    result = subprocess.run(['git', '-C', str(root), 'ls-files', '--stage', '-z'],
                            capture_output=True, timeout=10)
    if result.returncode:
        raise RuntimeError('Unable to enumerate tracked files.')
    entries = {}
    for entry in result.stdout.split(b'\0'):
        if not entry:
            continue
        metadata, name = entry.split(b'\t', 1)
        mode, object_id, stage = metadata.decode('ascii').split()
        name = name.decode('utf-8')
        if mode not in ('100644', '100755') or stage != '0':
            raise ValueError(f'Unsupported Git entry: {name}; resolve conflicts, symlinks or submodules first.')
        entries[name] = object_id
    return entries


def check_public(root=ROOT, *, require_git=False):
    root = Path(root).resolve()
    files = public_files(root)
    entries = git_index(root)
    if entries is None:
        if require_git:
            raise ValueError('This check requires a Git repository rooted at the project directory.')
        return files, None
    allowed = {path.relative_to(root).as_posix() for path in files}
    unexpected = sorted(set(entries) - allowed)
    if unexpected:
        raise ValueError('Tracked files outside the public allowlist: ' + ', '.join(unexpected))
    for name, object_id in sorted(entries.items()):
        # Read raw staged blobs, without textconv/external diff filters. Checking
        # the worktree alone misses a secret already staged and then edited out.
        result = subprocess.run(['git', '-C', str(root), 'cat-file', 'blob', object_id],
                                capture_output=True, timeout=10)
        if result.returncode:
            raise RuntimeError(f'Unable to inspect tracked file: {name}')
        validate_content(name, result.stdout, 'Git index')
    return files, len(entries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-git', action='store_true', help='Require Git index checks (use in repository CI)')
    args = parser.parse_args()
    try:
        files, tracked = check_public(require_git=args.require_git)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'Public check failed: {exc}\n')
    scope = f'{tracked} tracked files checked' if tracked is not None else 'standalone allowlist check; no project Git index'
    print(f'Public check passed: {len(files)} approved files, {scope}.')


if __name__ == '__main__':
    main()
