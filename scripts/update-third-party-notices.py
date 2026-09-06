#!/usr/bin/env python3
"""Refresh frontend runtime LICENSE/NOTICE texts after `npm --prefix frontend ci`."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    lock = json.loads((ROOT / 'frontend/package-lock.json').read_text())
    sections = [
        'US3000 Monitor — Third-party notices',
        '',
        'This file preserves the license and notice texts of frontend runtime',
        'dependencies recorded in frontend/package-lock.json. The MIT license of',
        'the original project does not replace these third-party licenses.',
        'Regenerate with: python3 scripts/update-third-party-notices.py',
        '',
    ]
    count = 0
    for package_path, metadata in sorted(lock['packages'].items()):
        if not package_path or metadata.get('dev'):
            continue
        package = ROOT / 'frontend' / package_path
        if not package.is_dir() or not package.resolve().is_relative_to((ROOT / 'frontend/node_modules').resolve()):
            raise SystemExit('Run npm --prefix frontend ci before generating notices.')
        installed = json.loads((package / 'package.json').read_text())
        if installed['version'] != metadata['version']:
            raise SystemExit('Installed package versions differ from the lockfile; run npm ci.')
        texts = sorted(path for path in package.iterdir() if path.is_file() and
                       path.name.lower().split('.')[0] in {'license', 'licence', 'notice', 'copying', 'copyrightnotice'})
        if not texts:
            raise SystemExit(f'No license text found for {installed["name"]}; review this dependency.')
        sections.extend(['=' * 72, f'Package: {installed["name"]}@{installed["version"]}',
                         f'Source: {metadata.get("resolved", "npm package")}', ''])
        for path in texts:
            sections.extend([f'--- {path.name} ---', path.read_text().rstrip(), ''])
        count += 1
    (ROOT / 'THIRD_PARTY_NOTICES.txt').write_text('\n'.join(sections) + '\n')
    print(f'Updated THIRD_PARTY_NOTICES.txt for {count} runtime packages.')


if __name__ == '__main__':
    main()
