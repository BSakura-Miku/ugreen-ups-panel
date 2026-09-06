"""Local US3000 observations; electrical candidates are not calibrated measurements."""
import time

MODES = {0x26: 'online', 0x36: 'charging', 0x21: 'battery'}


def parse_frame(frame: bytes, timestamp: float | None = None, usb_status: int = 0) -> dict:
    if len(frame) != 64 or frame[0] != 0x71:
        raise ValueError('Expected a complete 64-byte report 0x71')
    if usb_status not in (0, -2):
        raise ValueError('Unsupported USB completion status')
    word = lambda i: int.from_bytes(frame[i:i + 2], 'big')
    cells = [word(i) / 1000 for i in (35, 37, 39, 41)]
    battery = word(22) / 1000
    soc = frame[43]
    if not 0 <= soc <= 100 or not 5 <= battery <= 20 or any(not 1 <= v <= 5 for v in cells):
        raise ValueError('Implausible battery fields')
    mode = MODES.get(frame[7], 'unknown')
    voltage = word(18) / 1000
    current = word(24) / 1000
    adapter = word(16) / 1000
    warnings = []
    if mode == 'unknown':
        warnings.append('此工作模式的字段尚未在本机验证')
    elif not 1 <= voltage <= 30:
        warnings.append('输出电压读数无效')
    if mode != 'unknown' and current > 30:
        warnings.append('输出电流读数无效')
    if abs(sum(cells) - battery) > .25:
        warnings.append('电芯电压之和与电池组电压差异较大')
    if adapter > 30:
        warnings.append('输入电压原始值超出范围，暂不显示')
    adapter = adapter if adapter <= 30 and mode != 'unknown' else None
    output = voltage if mode != 'unknown' and 1 <= voltage <= 30 else None
    current = current if mode != 'unknown' and current <= 30 else None
    # Local transition confirms word18 follows the 19 V / 12 V supply rail.
    # Word24 / 1000 remains a current hypothesis, NOT a verified output current.
    power = round(output * current, 3) if output is not None and current is not None else None
    charge = word(29) / 1000 if mode == 'charging' and word(29) <= 30000 else None
    discharge = word(31) / 1000 if mode == 'battery' and word(31) <= 30000 else None
    if (mode == 'charging' and charge is None) or (mode == 'battery' and discharge is None):
        warnings.append('电池电流候选超出范围，暂不显示')
    return {
        'timestamp': timestamp if timestamp is not None else time.time(),
        'mode': mode, 'raw_status': frame[7], 'soc': soc,
        'input_voltage': adapter, 'adapter_input_voltage_v': adapter,
        'output_voltage': output, 'ups_output_voltage_v': output,
        'decoder_version': 4,
        'current': current if mode != 'unknown' else None,
        'current_kind': 'unverified',
        'power_w': power, 'power_kind': 'unverified',
        'power_verified': False, 'current_verified': False,
        'power_source': 'rail18_times_current24_hypothesis_v2',
        'formula_version': 2, 'dc_power_estimate_w': power,
        'field_evidence': {'input_voltage': 'observed_decay_and_recovery_during_power_transition',
                           'rail_voltage': 'observed_19v_12v_transition',
                           'current': 'measurement_location_and_scale_unconfirmed',
                           'battery_voltage': 'consistent_with_sum_of_four_cells',
                           'soc': 'device_reported_not_capacity_tested',
                           'charging_mode': 'observed_0x36_with_soc_rising',
                           'charge_current': 'word29_tracks_recharge_scale_unconfirmed',
                           'discharge_current': 'word31_tracks_battery_mode_scale_unconfirmed',
                           'word20': 'not_total_input_current_recharge_counterexample'},
        'raw_fields': {'be_u16': {str(i): word(i) for i in (16,18,20,22,24,26,29,31,33)},
                       'byte_26': frame[26], 'byte_27': frame[27],
                       'byte_28': frame[28], 'frame_hex': frame.hex()},
        'battery_voltage': battery, 'cells': cells,
        'cell_delta_mv': round((max(cells) - min(cells)) * 1000, 1),
        'charge_current_ma': None,  # Legacy verified-looking field intentionally unused.
        'battery_charge_current_candidate_a': charge,
        'battery_discharge_current_candidate_a': discharge,
        'battery_charge_power_candidate_w': round(battery * charge, 3) if charge is not None else None,
        'battery_discharge_power_candidate_w': round(battery * discharge, 3) if discharge is not None else None,
        'battery_current_verified': False,
        'load_percent': None, 'load_verified': False,
        'runtime_sec': None, 'runtime_source': None,
        'usb_status': usb_status, 'warnings': warnings,
    }
