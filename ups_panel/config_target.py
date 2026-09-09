"""Opaque calibration targets that remain stable across directory bind mounts."""
import hashlib
import os
from pathlib import Path
import stat


def target_identity(path):
    """Identify the parent inode and filename, including a not-yet-created file.

    No pathname, device number or inode is exposed. Atomic file replacement does
    not change the identity. This is a comparison aid, not an authentication key.
    """
    if not path:
        return {'identity': None, 'state': 'unconfigured'}
    try:
        target = Path(path)
        if target.name in ('', '.', '..') or any(ord(c) < 32 for c in str(target)):
            return {'identity': None, 'state': 'invalid_path'}
        parent = target.parent.stat()
        if not stat.S_ISDIR(parent.st_mode):
            return {'identity': None, 'state': 'parent_unavailable'}
        token = f'ups-calibration-target-v1\0{parent.st_dev}\0{parent.st_ino}\0'.encode() + os.fsencode(target.name)
        return {'identity': hashlib.sha256(token).hexdigest(), 'state': 'ready'}
    except (OSError, ValueError, TypeError):
        return {'identity': None, 'state': 'parent_unavailable'}


def config_file_state(path):
    """Describe readability without parsing contents or following a file symlink."""
    if not path:
        return 'unconfigured'
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return 'unreadable'
        return 'loaded'
    except FileNotFoundError:
        return 'missing' if target_identity(path)['state'] == 'ready' else 'unreadable'
    except (OSError, ValueError, TypeError):
        return 'unreadable'
