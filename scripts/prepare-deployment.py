#!/usr/bin/env python3
"""Create a NAS deployment bundle for the prebuilt Docker Hub image."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import tarfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('public_release', ROOT / 'scripts/prepare-release.py')
PUBLIC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PUBLIC)
RUNTIME = (
    'compose.yaml', '.env.example', 'LICENSE', 'THIRD_PARTY_NOTICES.txt',
    'README.md', 'README.en.md', 'CONTRIBUTING.md', 'SECURITY.md', 'CHANGELOG.md',
    'scripts/collector-admin.py', 'scripts/install-collector.sh',
    'scripts/rollback-collector.sh', 'scripts/uninstall-collector.sh', 'scripts/migrate-history.py',
    'deploy/ugreen-ups-collector.service', 'deploy/ugreen-ups-panel.tmpfiles.conf',
    'ups_panel/__init__.py', 'ups_panel/collector.py', 'ups_panel/protocol.py',
    'ups_panel/usbmon.py', 'ups_panel/power.py',
    'frontend/src/assets/us3000-logo.png',
)


def deployment_files():
    public = set(PUBLIC.public_files())
    files = {ROOT / name for name in RUNTIME}
    files.update(path for path in public if path.relative_to(ROOT).parts[0] == 'docs')
    if not files <= public:
        raise ValueError('Deployment contains files outside the public release allowlist')
    return sorted(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='New staging directory; must not exist')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    version = json.loads((ROOT / 'frontend/package.json').read_text())['version']
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        parser.error('Invalid version')
    files = deployment_files()
    if args.check:
        print(f'Deployment allowlist passed: {len(files)} files, version {version}.')
        return
    target = args.output or ROOT / 'dist/deployment' / f'ugreen-ups-panel-deploy-{version}'
    archive = Path(str(target) + '.tar.gz')
    checksum = Path(str(archive) + '.sha256')
    if any(p.exists() or p.is_symlink() for p in (target, archive, checksum)):
        parser.error('Destination already exists; choose a new --output directory')
    target.mkdir(parents=True)
    manifest = []
    for path in files:
        relative = path.relative_to(ROOT)
        dest = target / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        dest.chmod(0o755 if relative.parts[0] == 'scripts' else 0o644)
        manifest.append(f'{hashlib.sha256(dest.read_bytes()).hexdigest()}  {relative.as_posix()}')
    (target / 'DEPLOYMENT_MANIFEST.sha256').write_text('\n'.join(manifest) + '\n')
    def clean(info):
        info.uid = info.gid = 0
        info.uname = info.gname = ''
        info.mtime = 0
        return info
    with tarfile.open(archive, 'x:gz') as stream:
        stream.add(target, arcname='ugreen-ups-panel', filter=clean)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum.write_text(f'{digest}  {archive.name}\n')
    print(f'Ready: {archive}\nSHA256: {digest}')


if __name__ == '__main__':
    main()
