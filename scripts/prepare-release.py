#!/usr/bin/env python3
"""Export only reviewed public files; never export the containing workspace."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import tarfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    '.gitignore', '.dockerignore', '.env.example', '.github/workflows/ci.yml',
    'LICENSE', 'THIRD_PARTY_NOTICES.txt', 'README.md', 'README.en.md', 'CHANGELOG.md', 'CONTRIBUTING.md', 'SECURITY.md',
    'Dockerfile', 'compose.yaml', 'requirements.txt', 'requirements.lock', 'requirements-dev.txt',
    'deploy/ugreen-ups-collector.service', 'deploy/ugreen-ups-panel.tmpfiles.conf',
    'docs/fields.md', 'docs/calibration.md', 'docs/architecture.md', 'docs/hardware.md',
    'docs/validation.md', 'docs/DELIVERY.md',
    'docs/assets/dashboard-demo.png',
    'scripts/install-collector.sh', 'scripts/rollback-collector.sh', 'scripts/uninstall-collector.sh',
    'scripts/collector-admin.py', 'scripts/capture-power.py', 'scripts/prepare-release.py',
    'scripts/update-third-party-notices.py',
    'frontend/package.json', 'frontend/package-lock.json', 'frontend/tsconfig.json',
    'frontend/vite.config.ts', 'frontend/index.html',
)
GLOBS = ('ups_panel/*.py', 'tests/test_*.py', 'fixtures/*.hex', 'frontend/src/**/*.ts',
         'frontend/src/**/*.tsx', 'frontend/src/**/*.css', 'frontend/src/assets/*.png')
PATTERNS = (
    re.compile(r'/(?:Users|home)/[A-Za-z0-9_.-]+/'),
    re.compile(r'\b10\.10\.2\.\d+\b'),
    re.compile(r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----'),
    re.compile(r'\b(?:ghp|github_pat|sk_live)_[A-Za-z0-9_]{20,}\b'),
    re.compile(r'eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}'),
)


def public_files():
    paths = {ROOT / name for name in FILES}
    for pattern in GLOBS:
        paths.update(ROOT.glob(pattern))
    for path in paths:
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != ROOT and ROOT in parent.parents) or not path.is_file():
            raise ValueError(f'Missing or symlinked release file: {path.relative_to(ROOT)}')
        if path.suffix != '.png':
            content = path.read_text()
            if any(pattern.search(content) for pattern in PATTERNS):
                raise ValueError(f'Potential private material: {path.relative_to(ROOT)} (review locally)')
    return sorted(paths)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Validate the public allowlist without copying')
    parser.add_argument('--output', type=Path, help='New, empty destination directory (must not exist)')
    args = parser.parse_args()
    files = public_files()
    version = json.loads((ROOT / 'frontend/package.json').read_text())['version']
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        parser.error('Invalid release version')
    if args.check:
        print(f'Public release allowlist passed: {len(files)} files, version {version}.')
        return
    target = args.output or ROOT / 'dist/release' / f'ugreen-ups-panel-{version}'
    archive = Path(str(target) + '.tar.gz')
    checksum_file = Path(str(archive) + '.sha256')
    if target.exists() or target.is_symlink() or archive.exists() or archive.is_symlink() or checksum_file.exists() or checksum_file.is_symlink():
        parser.error('Release destination already exists; choose a new --output directory.')
    target.mkdir(parents=True)
    manifest = []
    for source in files:
        relative = source.relative_to(ROOT)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(0o755 if relative.parts[0] == 'scripts' else 0o644)
        manifest.append(f'{hashlib.sha256(destination.read_bytes()).hexdigest()}  {relative.as_posix()}')
    (target / 'SOURCE_MANIFEST.sha256').write_text('\n'.join(manifest) + '\n')
    def clean_info(info):
        info.uid = info.gid = 0
        info.uname = info.gname = ''
        info.mtime = 0
        return info
    with tarfile.open(archive, 'w:gz') as stream:
        stream.add(target, arcname=target.name, filter=clean_info)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum_file.write_text(f'{checksum}  {archive.name}\n')
    print(f'Ready: {target}\nArchive: {archive}\nSHA256: {checksum}')


if __name__ == '__main__':
    main()
