#!/usr/bin/env python3
"""Build a deterministic code-only GitHub collector release with stdlib Python.

Usage: python3 scripts/package-collector.py --output-dir dist/collector
       --revision <40-hex-commit>

No downloaded code or installer is executed. VERSION is read as an AST string
constant. All archive ownership, mode, timestamp and gzip header fields are fixed.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
from ups_panel.update_release import (  # noqa: E402
    MAX_ARCHIVE_BYTES, MAX_CONTENT_BYTES, MAX_LICENSE_BYTES, MAX_MEMBERS, MAX_SOURCE_BYTES,
    ReleaseError, inspect_package, read_source_version, source_fingerprint, version_tuple,
)


def _plain_path(path):
    path = Path(path).absolute()
    if '..' in path.parts or any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError('symlink or parent traversal is not allowed')
    return path


def _revision(root, revision):
    if revision is None:
        result = subprocess.run(['git', '-C', str(root), 'rev-parse', '--verify', 'HEAD^{commit}'],
                                capture_output=True, text=True, timeout=10, check=False)
        if result.returncode != 0:
            raise ValueError('a verified Git commit or --revision is required')
        revision = result.stdout.strip()
    if not isinstance(revision, str) or re.fullmatch(r'[0-9a-f]{40}', revision) is None:
        raise ValueError('revision must contain exactly 40 lowercase hex characters')
    return revision


def make_package(source_root, revision=None):
    root = _plain_path(source_root)
    source = _plain_path(root / 'ups_panel')
    if not source.is_dir():
        raise ValueError('ups_panel source directory is missing')
    # Reject every symlink within the code tree, including ignored/nested entries.
    for directory, dirs, names in os.walk(source, followlinks=False):
        for name in dirs + names:
            if (Path(directory) / name).is_symlink():
                raise ValueError('symlinks are not allowed in the source tree')
    files = {}
    for path in sorted(source.glob('*.py')):
        if not path.is_file() or path.is_symlink() or not 0 <= path.stat().st_size <= MAX_SOURCE_BYTES:
            raise ValueError('invalid source file')
        files['ups_panel/' + path.name] = path.read_bytes()
    license_path = _plain_path(root / 'LICENSE')
    if not license_path.is_file() or not 0 < license_path.stat().st_size <= MAX_LICENSE_BYTES:
        raise ValueError('a bounded regular LICENSE file is required')
    license_text = license_path.read_bytes()
    if len(files) + 3 > MAX_MEMBERS or sum(map(len, files.values())) + len(license_text) > MAX_CONTENT_BYTES:
        raise ValueError('source tree exceeds package limits')
    if 'ups_panel/build_info.py' not in files:
        raise ValueError('build_info.py is missing')
    version = read_source_version(files['ups_panel/build_info.py'])
    if version_tuple(version) < (0, 9, 0):
        raise ValueError('collector packages require version 0.9.0 or later')
    revision = _revision(root, revision)
    metadata = {'schema': 1, 'version': version, 'revision': revision,
                'source_sha256': source_fingerprint(files), 'minimum_updater_schema': 1,
                'calibration_schemas': [1, 2]}
    files['collector-release.json'] = (json.dumps(metadata, sort_keys=True, separators=(',', ':')) + '\n').encode()
    files['LICENSE'] = license_text
    archive_buffer = io.BytesIO()
    with gzip.GzipFile(filename='', fileobj=archive_buffer, mode='wb', compresslevel=9, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w', format=tarfile.USTAR_FORMAT, encoding='utf-8') as archive:
            directory = tarfile.TarInfo('ups_panel/')
            directory.type = tarfile.DIRTYPE
            directory.mode, directory.uid, directory.gid, directory.mtime = 0o755, 0, 0, 0
            directory.uname = directory.gname = ''
            archive.addfile(directory)
            for name, content in sorted(files.items()):
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.uid, info.gid, info.mtime = len(content), 0o644, 0, 0, 0
                info.uname = info.gname = ''
                archive.addfile(info, io.BytesIO(content))
    data = archive_buffer.getvalue()
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError('compressed package exceeds size limit')
    # Use the same non-executing parser the host updater will use.
    verified = inspect_package(data, expected_version=version, expected_revision=revision,
                               expected_source_sha256=metadata['source_sha256'])
    return data, dict(verified.metadata)


def write_package(source_root, output_dir, revision=None):
    data, metadata = make_package(source_root, revision)
    output = _plain_path(output_dir)
    output.mkdir(mode=0o755, parents=True, exist_ok=True)
    output = _plain_path(output)
    archive_name = 'collector-v' + metadata['version'] + '.tar.gz'
    archive_path = output / archive_name
    sums_path = output / 'SHA256SUMS'
    if archive_path.exists() or archive_path.is_symlink() or sums_path.exists() or sums_path.is_symlink():
        raise ValueError('output artifacts already exist; choose an empty output directory')
    digest = hashlib.sha256(data).hexdigest()
    created = []
    try:
        for path, content in ((archive_path, data), (sums_path, (digest + '  ' + archive_name + '\n').encode('ascii'))):
            with path.open('xb') as destination:
                created.append(path)
                destination.write(content)
                destination.flush()
                os.fsync(destination.fileno())
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return {'archive': str(archive_path), 'checksums': str(sums_path), 'sha256': digest,
            'bytes': len(data), 'metadata': metadata}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--source-root', default=ROOT, type=Path)
    parser.add_argument('--revision')
    args = parser.parse_args(argv)
    try:
        result = write_package(args.source_root, args.output_dir, args.revision)
    except (OSError, ValueError, subprocess.SubprocessError, ReleaseError):
        print('Collector package creation failed: check source, version, revision and output directory.', file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
