"""Explain existing telemetry and build explicit, shareable diagnostic exports."""
import csv
import io
import ipaddress
import math
import re
import time

from .build_info import get_build_info
from .calibration_status import REASONS, readiness_message
from .storage_health import STORAGE_MESSAGES, storage_message

NUT_FIELDS = frozenset({
    'ups.status', 'ups.alarm', 'battery.charge', 'battery.runtime',
    'battery.charge.low', 'battery.runtime.low', 'input.voltage', 'output.voltage',
    'ups.load', 'driver.name', 'driver.version', 'driver.version.data',
    'driver.version.internal', 'driver.version.usb', 'ups.firmware',
    'ups.firmware.aux', 'device.mfr', 'device.model',
})
STATUS_TOKENS = frozenset({'OL', 'OB', 'LB', 'HB', 'RB', 'CHRG', 'DISCHRG',
                           'BYPASS', 'CAL', 'OFF', 'OVER', 'TRIM', 'BOOST',
                           'FSD', 'ALARM', 'ECO', 'ESS'})
NUMERIC_NUT_FIELDS = {
    'battery.charge': (0, 100), 'battery.charge.low': (0, 100),
    'battery.runtime': (-1, 4294967295), 'battery.runtime.low': (0, 4294967295),
    'input.voltage': (0, 1000), 'output.voltage': (0, 1000), 'ups.load': (0, 100),
}
POINT_NUMBERS = {
    'timestamp': (0, 1e12), 'segment': (0, 2147483647), 'raw_status': (0, 255),
    'byte_26': (0, 255), 'byte_27': (0, 255), 'byte_28': (0, 255),
    'soc': (0, 100), 'battery_voltage': (0, 30), 'adapter_input_voltage_v': (0, 30),
    'ups_output_voltage_v': (0, 30), 'current': (0, 30),
    'battery_charge_current_candidate_a': (0, 30),
    'battery_discharge_current_candidate_a': (0, 30),
    'decoder_version': (1, 10000), 'formula_version': (1, 10000),
}
POINT_FIELDS = ('timestamp', 'segment', 'device_alias', 'source', 'mode', 'raw_status',
                'byte_26', 'byte_27', 'byte_28', 'soc', 'battery_voltage',
                'adapter_input_voltage_v', 'ups_output_voltage_v', 'current',
                'battery_charge_current_candidate_a', 'battery_discharge_current_candidate_a',
                'decoder_version', 'formula_version', 'calibration_revision', 'calibration_profile')


def mapping(value):
    return value if isinstance(value, dict) else {}


def number(value, low=0, high=1e12):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    try:
        return value if math.isfinite(value) and low <= value <= high else None
    except OverflowError:
        return None


def text(value, limit=160):
    if not isinstance(value, str):
        return None
    value = ' '.join(value.split())
    return value[:limit] if value else None


def numeric_text(value, low, high):
    if not isinstance(value, str) or not re.fullmatch(r'-?\d{1,12}(?:\.\d{1,8})?', value):
        return None
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        return None
    return int(result) if result.is_integer() else result


def age(timestamp, now):
    timestamp = number(timestamp)
    return round(now - timestamp, 1) if timestamp is not None and 0 <= now - timestamp <= 1e12 else None


def system_view(view, now):
    nut = mapping(view.get('nut'))
    values = {key: value for key, raw in mapping(nut.get('values')).items()
              if key in NUT_FIELDS and (value := text(raw)) is not None}
    query_age = age(nut.get('timestamp'), now)
    available = nut.get('available') is True
    fresh = bool(available and query_age is not None and query_age <= 45)
    status = values.get('ups.status')
    tokens = list(dict.fromkeys(token for token in (status or '').split() if token in STATUS_TOKENS))
    raw = values.get('battery.runtime')
    seconds = numeric_text(raw, -1, 4294967295)
    if not available or raw is None:
        quality, label = 'unavailable', '系统未提供可用续航'
    elif not fresh:
        quality, label = 'stale', '查询已过期，原值不作为当前续航'
    elif seconds in (-1, 65535, 4294967295):
        quality, label = 'sentinel', '疑似无效占位值，不换算为剩余时长'
    elif seconds is None or not isinstance(seconds, int):
        quality, label = 'invalid', '续航原值无法作为有效秒数解析'
    else:
        quality, label = 'unverified', '系统报告的剩余秒数，尚未独立验证'
    notices = []
    if fresh:
        for token, label_text in (('LB', '系统报告低电量'), ('RB', '系统报告需要更换电池'),
                                  ('OVER', '系统报告过载'), ('ALARM', '系统报告有活动告警'),
                                  ('FSD', '系统报告进入强制关机流程'), ('OFF', '系统报告输出关闭')):
            if token in tokens:
                notices.append({'code': token, 'label': label_text, 'level': 'warning'})
        if values.get('ups.alarm') and 'ALARM' not in tokens:
            notices.append({'code': 'alarm_text', 'label': '系统提供了告警文字', 'level': 'warning'})
        if 'OL' in tokens and 'DISCHRG' in tokens:
            notices.append({'code': 'online_discharge', 'label': '系统同时报告在线与放电，需结合私有报文及查询时间核对', 'level': 'info'})
    return {
        'available': available, 'fresh': fresh, 'status_raw': status, 'status_tokens': tokens,
        'notices': notices, 'alarm_text': values.get('ups.alarm') if fresh else None,
        'thresholds': {
            'charge_low': numeric_text(values.get('battery.charge.low'), 0, 100) if fresh else None,
            'runtime_low_sec': numeric_text(values.get('battery.runtime.low'), 0, 4294967294) if fresh else None,
        },
        'runtime': {'raw': raw, 'seconds': seconds if quality == 'unverified' else None,
                    'quality': quality, 'label': label},
        'values': values,
    }


def build_view(raw):
    raw = mapping(raw)
    return {'version': text(raw.get('version'), 64), 'revision': text(raw.get('revision'), 40),
            'source_sha256': text(raw.get('source_sha256'), 64)}


def diagnostic_view(view, observation, *, storage_ready=False, storage_error=None,
                    storage_error_code=None, calibration_readiness=None, now=None):
    now = time.time() if now is None else now
    view = mapping(view)
    collector = mapping(view.get('collector'))
    host = mapping(collector.get('host'))
    usb = mapping(collector.get('usb'))
    nut = mapping(view.get('nut'))
    sample = mapping(view.get('sample'))
    capture = mapping(mapping(view.get('diagnostics')).get('capture'))
    system = system_view(view, now)
    query_age = age(nut.get('timestamp'), now)
    usb_age = age(sample.get('timestamp'), now)
    fresh = bool(view.get('fresh') is True and usb_age is not None and usb_age <= 10)
    poll_state = mapping(nut.get('pollonly')).get('state')
    pollonly = True if poll_state == 'enabled' else False if poll_state == 'disabled' else None
    association = mapping(nut.get('association')).get('status')
    association = association if association in ('matched', 'different') else 'unverified'
    checks = []
    def check(key, label, status, detail):
        checks.append({'id': key, 'label': label, 'status': status, 'detail': detail})

    heartbeat_age = age(view.get('heartbeat'), now)
    if heartbeat_age is not None and heartbeat_age <= 10:
        check('snapshot', '采集器快照', 'ok', '已读到持续更新的采集快照')
    else:
        check('snapshot', '采集器快照', 'waiting', '尚无新鲜快照，请检查本项目采集器是否运行')
    if host:
        supported = host.get('python_supported') is True and host.get('system') == 'Linux'
        check('host', '宿主采集条件', 'ok' if supported else 'warning',
              '已报告 Linux 与受支持的 Python' if supported else '宿主系统或 Python 条件待核对')
    else:
        check('host', '宿主采集条件', 'unknown', '当前采集器未报告环境检查信息')
    discovery = usb.get('discovery')
    if discovery == 'replay' or view.get('source') == 'replay':
        check('usb', 'USB 设备识别', 'warning', '正在使用演示回放，不能验证真实 USB 连接')
    elif discovery == 'found' or (not discovery and mapping(view.get('device'))):
        check('usb', 'USB 设备识别', 'ok', '已发现目标 US3000 USB 设备')
    elif discovery in ('ambiguous', 'unavailable'):
        check('usb', 'USB 设备识别', 'warning', '多设备未明确选择' if discovery == 'ambiguous' else '无法完成设备枚举')
    else:
        check('usb', 'USB 设备识别', 'waiting' if discovery == 'not_found' else 'unknown',
              '未发现目标设备' if discovery == 'not_found' else '当前快照未提供设备识别结果')
    readable = usb.get('usbmon_readable')
    if type(readable) is bool:
        check('usbmon', '被动采集入口', 'ok' if readable else 'warning',
              'usbmon 节点可读' if readable else 'usbmon 节点缺失或当前采集器无法读取')
    else:
        check('usbmon', '被动采集入口', 'unknown', '当前采集器未报告 usbmon 检查结果')
    private_details = {
        'no_target_activity': '观察窗口内没有目标设备活动',
        'no_interrupt_in': '有设备活动，但没有所需的中断输入报告',
        'no_complete_report': '有中断活动，但没有完整的私有报告',
        'invalid_reports': '报告未通过字段或时效检查',
        'unavailable': '采集入口暂不可用',
        'stale': '之前收到的私有报告已过期',
    }
    if fresh and view.get('source') == 'usbmon':
        check('private_report', '私有报文接收', 'ok', '已收到新鲜、完整的 0x71 报告')
    elif view.get('source') == 'replay':
        check('private_report', '私有报文接收', 'warning', '当前为演示快照，非设备实测')
    else:
        detail = private_details.get(text(capture.get('state')), '等待新鲜、完整的 0x71 私有报告')
        if pollonly:
            detail += '；系统报告启用 pollonly，可能跳过所需中断读取'
        check('private_report', '私有报文接收', 'waiting', detail)
    if system['fresh']:
        check('nut', '系统 UPS 查询', 'ok', 'NUT 查询成功；这不代表所有私有字段已可用')
    else:
        check('nut', '系统 UPS 查询', 'warning', '查询已过期' if system['available'] else '查询不可用；独立的私有采集仍可工作')
    check('association', '两路设备关联', 'ok' if association == 'matched' else 'warning' if association == 'different' else 'unknown',
          '已核对为同一设备' if association == 'matched' else '系统查询目标与采集设备不一致' if association == 'different' else '尚未确认 NUT 查询与私有采集指向同一设备')
    check('storage', '历史写入', 'warning' if storage_error else 'ok' if storage_ready else 'waiting',
          storage_message(storage_error_code) if storage_error else '最近历史写入成功' if storage_ready else '等待历史存储初始化')
    safe_readiness = None
    if calibration_readiness is not None:
        raw = mapping(calibration_readiness)
        code = raw.get('code') if raw.get('code') in REASONS else None
        safe_readiness = {'ready': raw.get('ready') is True, 'can_save': raw.get('can_save') is True,
                          'code': code, 'target': raw.get('target') if raw.get('target') in
                          ('matched', 'mismatched', 'unverified', 'unavailable') else 'unverified'}
        check('calibration', '校准配置', 'warning' if code else 'ok' if safe_readiness['ready'] else 'waiting',
              readiness_message(code) if code else '采样与校准配置一致，配置文件路径已核对'
              if safe_readiness['ready'] else '等待校准配置就绪')
    raw_counters = mapping(capture.get('recent_counters'))
    counters = {key: value for key, value in raw_counters.items()
                if key in {'target_events', 'not_completion', 'not_interrupt_in', 'invalid_status',
                           'missing_payload', 'invalid_length', 'invalid_report_id', 'accepted_reports',
                           'invalid_sample', 'stale_sample'} and type(value) is int and 0 <= value <= 2147483647}
    values = system['values']
    return {
        'schema': 1, 'generated_at': now, 'capture_fresh': fresh,
        'versions': {'panel': get_build_info(), 'collector': build_view(collector.get('build')) if collector else None,
                     'nut_driver': values.get('driver.name'), 'nut_version': values.get('driver.version'),
                     'nut_subdriver': values.get('driver.version.data'), 'ups_firmware': values.get('ups.firmware'),
                     'usb_device_version': text(usb.get('bcd_device'), 32),
                     'decoder_version': number(sample.get('decoder_version'), 1, 10000)},
        'connection': {'checks': checks, 'usb_age_sec': usb_age, 'nut_query_age_sec': query_age,
                       'pollonly': pollonly, 'counters': counters, 'association': association},
        'system': system, 'observation': observation,
        'calibration_readiness': safe_readiness,
        'storage': {'ready': bool(storage_ready and not storage_error),
                    'code': storage_error_code if storage_error_code in STORAGE_MESSAGES else
                    'database_unavailable' if storage_error else None},
    }


def safe_version(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    numeric_part = re.sub(r'[-+](?:alpha|beta|rc|dev)\d{1,5}$', '', value.lstrip('vV'))
    try:
        ipaddress.ip_address(numeric_part)
        return None
    except ValueError:
        pass
    return value if re.fullmatch(r'[vV]?\d{1,5}(?:[._-]\d{1,8}){0,5}(?:[-+](?:alpha|beta|rc|dev)\d{1,5})?', value) else None


def safe_build(value):
    value = mapping(value)
    def digest(key, size):
        raw = value.get(key)
        return raw if isinstance(raw, str) and re.fullmatch(size, raw) else None
    return {'version': safe_version(value.get('version')), 'revision': digest('revision', r'[0-9a-f]{7,40}'),
            'source_sha256': digest('source_sha256', r'[0-9a-f]{64}')}


def export_point(value):
    value = mapping(value)
    point = {key: number(value.get(key), *bounds) for key, bounds in POINT_NUMBERS.items()}
    for key in ('segment', 'raw_status', 'byte_26', 'byte_27', 'byte_28', 'decoder_version', 'formula_version'):
        if type(point[key]) is not int:
            point[key] = None
    alias = value.get('device_alias')
    point['device_alias'] = alias if isinstance(alias, str) and re.fullmatch(r'device-[a-f0-9]{8,32}', alias) else None
    point['source'] = value.get('source') if value.get('source') in ('usbmon', 'replay') else None
    point['mode'] = value.get('mode') if value.get('mode') in ('online', 'charging', 'battery', 'unknown') else None
    profile = value.get('calibration_profile')
    point['calibration_profile'] = profile if profile in ('none', 'local-19v-v1', 'custom') else None
    revision = value.get('calibration_revision')
    point['calibration_revision'] = revision if isinstance(revision, str) and re.fullmatch(r'[a-f0-9]{64}', revision) else None
    return {key: point[key] for key in POINT_FIELDS}


def diagnostic_export(diagnostic):
    """No raw payloads, target identifiers or arbitrary device-provided strings."""
    versions = diagnostic['versions']
    safe_versions = {'panel': safe_build(versions['panel']),
                     'collector': safe_build(versions['collector']) if versions['collector'] else None,
                     'nut_version': safe_version(versions['nut_version']),
                     'ups_firmware': safe_version(versions['ups_firmware']),
                     'usb_device_version': safe_version(versions['usb_device_version']),
                     'decoder_version': versions['decoder_version']}
    driver = versions['nut_driver']
    safe_versions['nut_driver'] = driver if driver in ('usbhid-ups', 'dummy-ups', 'snmp-ups', 'apcupsd-ups', 'nutdrv_qx') else None
    subdriver = versions['nut_subdriver']
    safe_versions['nut_subdriver'] = subdriver if isinstance(subdriver, str) and re.fullmatch(r'(?:TOP|Arduino) HID \d{1,3}\.\d{1,3}', subdriver) else None
    system = diagnostic['system']
    values = {}
    for key, bounds in NUMERIC_NUT_FIELDS.items():
        raw = system['values'].get(key)
        if numeric_text(raw, *bounds) is not None:
            values[key] = raw
    values['ups.status'] = ' '.join(system['status_tokens'])
    observation = diagnostic['observation']
    safe_observation = {key: observation.get(key) for key in (
        'schema', 'window_sec', 'max_samples', 'count', 'started_at', 'first_timestamp',
        'last_timestamp', 'truncated', 'gap_count', 'conflict_count', 'rejected_count')}
    safe_observation['points'] = [export_point(point) for point in observation.get('points', [])]
    raw_runtime = system['runtime']['raw']
    return {
        'schema': 1, 'format': 'us3000-diagnostics', 'generated_at': diagnostic['generated_at'],
        'privacy': {'mode': 'allowlist', 'omitted': ['serial', 'addresses', 'usb_paths', 'nut_target',
                                                    'free_text_alarms', 'full_raw_frames']},
        'versions': safe_versions, 'connection': diagnostic['connection'],
        'calibration_readiness': diagnostic.get('calibration_readiness'),
        'storage': diagnostic.get('storage'),
        'system': {'available': system['available'], 'fresh': system['fresh'],
                   'status_tokens': system['status_tokens'], 'values': values,
                   'notices': system['notices'], 'thresholds': system['thresholds'],
                   'runtime': {**system['runtime'], 'raw': raw_runtime if numeric_text(raw_runtime, -1, 4294967295) is not None else None},
                   'free_text_alarm_present': bool(system['alarm_text'])},
        'observation': safe_observation,
    }


def observation_csv(diagnostic):
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=POINT_FIELDS)
    writer.writeheader()
    for point in diagnostic['observation'].get('points', []):
        writer.writerow(export_point(point))
    return output.getvalue()
