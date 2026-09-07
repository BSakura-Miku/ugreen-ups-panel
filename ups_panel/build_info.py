"""Version and reproducible source identity, without invoking Git or a shell."""
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import re

VERSION = '0.10.0'


@lru_cache(maxsize=1)
def _source_digest():
    digest = hashlib.sha256()
    try:
        for path in sorted(Path(__file__).parent.glob('*.py')):
            if path.is_symlink():
                return None
            digest.update(path.name.encode('utf-8') + b'\0')
            digest.update(path.read_bytes())
            digest.update(b'\0')
        return digest.hexdigest()
    except OSError:
        return None


def get_build_info():
    revision = os.getenv('UPS_BUILD_REVISION', '')
    return {'version': VERSION,
            'revision': revision if re.fullmatch(r'[0-9a-f]{7,40}', revision) else None,
            'source_sha256': _source_digest()}
