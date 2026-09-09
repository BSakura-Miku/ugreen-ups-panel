"""Explain calibration readiness without replacing invalid input with a preset."""
from .calibration import (CalibrationError, DEFAULT_COEFFICIENTS, default_config,
                          inspect_config, normalize_config)
from .config_target import target_identity


REASONS = {
    'snapshot_unavailable': '尚未收到有效采集快照，请检查宿主机采集器。',
    'snapshot_stale': '采集数据已过期，请恢复宿主机采集后再保存。',
    'missing_metadata': '采集器未提供完整校准信息，请更新采集器并等待新快照。',
    'unsupported_profile': '配置名称不受支持；12 V 适配器请明确选择自定义配置和 12 V 档位。',
    'unsupported_schema': '配置使用了当前版本不支持的格式，原文件已保留，请更新面板和采集器。',
    'invalid_config': '校准配置的内容或系数无效，请核对配置。',
    'revision_mismatch': '采样中的校准版本标识与系数不一致，估算已暂停。',
    'config_mismatch': '采样与采集器报告的校准配置不一致，请等待下一次采样或检查采集器。',
    'invalid_estimate': '采样中的估算结果或来源信息无效，原始遥测仍可使用。',
    'path_unconfigured': '采集器未配置可写的校准文件路径，请重新运行官方安装脚本。',
    'target_mismatch': '面板与采集器指向不同的校准文件，请核对数据目录挂载和安装路径。',
    'target_unverified': '旧版采集器尚未报告配置文件身份，路径一致性待更新采集器后确认。',
    'target_unavailable': '校准文件的父目录不可用，请检查数据目录是否已挂载。',
    'file_unreadable': '校准文件不可读取或不是普通文件，请检查权限和文件类型。',
    'file_invalid': '校准文件格式无效，可明确选择校准方式并填写有效参数后重新保存。',
    'collector_config_error': '采集器未能读取新配置，仍保留上一次有效配置，请检查文件状态。',
    'save_failed': '校准配置保存失败，请检查数据目录是否可写。',
}


def readiness_message(code):
    return REASONS.get(code, REASONS['invalid_config'])


def calibration_status(view, path):
    metadata = view.get('calibration')
    metadata = metadata if isinstance(metadata, dict) else {}
    validation = view.get('calibration_validation')
    validation = validation if isinstance(validation, dict) else {'valid': False, 'reason': 'missing_metadata'}
    inspected = inspect_config(path)
    target = target_identity(path)
    issues, blockers = [], []
    problem = None

    def issue(code, *, block=True):
        if code not in REASONS:
            code = 'invalid_config'
        if not any(item['code'] == code for item in issues):
            issues.append({'code': code, 'message': readiness_message(code)})
        if block and code not in blockers:
            blockers.append(code)

    active = None
    raw_active = metadata.get('config')
    if raw_active is not None:
        try:
            active = normalize_config(raw_active)
            if raw_active.get('revision') != active['revision']:
                raise CalibrationError('Revision mismatch', 'revision_mismatch')
        except (CalibrationError, TypeError, AttributeError) as exc:
            code = getattr(exc, 'code', 'invalid_config')
            issue(code)
            active = None
            problem = {'source': 'active', 'code': code}
            if validation.get('profile'):
                problem['profile'] = validation['profile']
    else:
        # Display-only compatibility with old snapshots. This does not satisfy
        # validation or authorize a save.
        profile = validation.get('profile')
        if profile in ('none', 'local-19v-v1'):
            active = default_config(profile)

    if inspected['state'] in ('invalid', 'unreadable'):
        code = inspected['code'] or 'file_invalid'
        issue(code, block=inspected['state'] == 'unreadable' or code == 'unsupported_schema')
        problem = {'source': 'desired', 'code': code}
        if inspected['profile']:
            problem['profile'] = inspected['profile']
    if not view.get('fresh'):
        issue('snapshot_unavailable' if not view.get('sample') else 'snapshot_stale')
    if validation.get('valid') is not True:
        code = validation.get('reason') or 'missing_metadata'
        issue(code)
        if problem is None:
            problem = {'source': 'sample', 'code': code}
            if validation.get('profile'):
                problem['profile'] = validation['profile']
    if metadata.get('configurable') is not True:
        issue('path_unconfigured')

    target_state = 'unverified'
    remote = metadata.get('config_target')
    if target['state'] != 'ready':
        target_state = 'unavailable'
        issue('target_unavailable')
    elif remote is None:
        issue('target_unverified', block=False)
    elif (not isinstance(remote, dict) or remote.get('state') != 'ready'
          or remote.get('identity') != target['identity']):
        target_state = 'mismatched'
        issue('target_mismatch')
    else:
        target_state = 'matched'
    if metadata.get('file_state') == 'unreadable':
        issue('file_unreadable')
    elif metadata.get('error') or metadata.get('file_state') == 'invalid':
        # A readable invalid desired file can be explicitly repaired. A stale
        # error with a healthy file must be resolved by the collector first.
        issue('collector_config_error', block=inspected['state'] != 'invalid')

    desired = inspected['config'] or active or default_config()
    edit_revision = (inspected['edit_revision'] if inspected['state'] != 'missing'
                     else desired['revision'])
    supported = metadata.get('supported_config_schemas', [1])
    if (not isinstance(supported, list) or not supported
            or any(type(version) is not int or version not in (1, 2) for version in supported)):
        supported = [1]
    can_save = bool(not blockers and active and edit_revision)
    errors = [item for item in issues if item['code'] != 'target_unverified']
    ready = can_save and not errors
    primary = errors[0] if errors else issues[0] if issues else None
    return {
        'schema': 1, 'defaults': DEFAULT_COEFFICIENTS, 'desired': desired,
        'active': active, 'collector_ready': ready,
        'supported_config_schemas': sorted(set(supported)),
        'pending': bool(inspected['state'] in ('invalid', 'unreadable') or active is None
                        or desired['revision'] != active['revision']),
        'error': errors[0]['message'] if errors else None,
        'edit_revision': edit_revision, 'configuration_problem': problem,
        'readiness': {'ready': ready, 'can_save': can_save,
                      'code': primary['code'] if primary else None,
                      'message': primary['message'] if primary else None,
                      'issues': issues, 'target': target_state},
    }
