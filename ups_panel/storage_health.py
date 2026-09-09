"""Stable storage errors safe to show in the UI and diagnostic exports."""
import errno
import sqlite3


STORAGE_MESSAGES = {
    'permission_denied': '历史数据库或目录不可写，请检查数据目录权限。',
    'database_locked': '历史数据库正在被其他进程锁定，写入将自动重试。',
    'disk_full': '历史存储空间已满，请为数据目录释放空间。',
    'database_corrupt': '历史数据库完整性异常，请保留原文件并从备份恢复。',
    'database_unavailable': '历史数据库暂不可用，请检查数据目录和挂载状态。',
    'invalid_history_data': '历史记录包含无法处理的数据，写入将自动重试。',
}


def storage_error_code(exc):
    code = getattr(exc, 'sqlite_errorcode', 0) or 0
    base = code & 0xff
    if isinstance(exc, PermissionError) or getattr(exc, 'errno', None) in (errno.EACCES, errno.EPERM, errno.EROFS) or base in (3, 8):
        return 'permission_denied'
    if getattr(exc, 'errno', None) in (errno.ENOSPC, errno.EDQUOT) or base == 13:
        return 'disk_full'
    if base in (5, 6):
        return 'database_locked'
    if base in (11, 26):
        return 'database_corrupt'
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return 'invalid_history_data'
    # Older Python versions may not attach SQLite result codes.
    if isinstance(exc, sqlite3.Error):
        message = str(exc).lower()
        if 'locked' in message:
            return 'database_locked'
        if 'readonly' in message or 'read-only' in message or 'read only' in message:
            return 'permission_denied'
        if 'disk is full' in message:
            return 'disk_full'
        if 'malformed' in message or 'not a database' in message:
            return 'database_corrupt'
    return 'database_unavailable'


def storage_message(code):
    return STORAGE_MESSAGES.get(code, STORAGE_MESSAGES['database_unavailable'])


def storage_status(state):
    code = state.get('storage_error_code')
    return {'state': 'unavailable' if code else 'ready' if state.get('last_success') else 'initializing',
            'code': code, 'message': storage_message(code) if code else None,
            'last_success': state.get('last_success') or None}
