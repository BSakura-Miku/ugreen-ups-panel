from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import math
import re

import pytest

from ups_panel.raw_observation import POINT_FIELDS, RawObservationMonitor


def view(timestamp, *, fresh=True, source='usbmon', device=None, mode='online',
         byte26=41, byte27=48, decoder=4, formula=2):
    return {
        'fresh': fresh, 'source': source,
        'device': {'bus': 3, 'device': 2, 'serial': 'PRIVATE-SERIAL',
                   'path': '/PRIVATE-USB-PATH'} if device is None else device,
        'sample': {
            'timestamp': timestamp, 'mode': mode,
            'raw_status': {'online': 0x26, 'charging': 0x36, 'battery': 0x21}.get(mode, 0xff),
            'raw_fields': {'byte_26': byte26, 'byte_27': byte27, 'byte_28': 47,
                           'be_u16': {'26': 10544}, 'frame_hex': 'PRIVATE-RAW-FRAME'},
            'soc': 100, 'battery_voltage': 16.37, 'adapter_input_voltage_v': 18.91,
            'ups_output_voltage_v': 18.90, 'current': 1.9,
            'battery_charge_current_candidate_a': None,
            'battery_discharge_current_candidate_a': None,
            'decoder_version': decoder, 'formula_version': formula,
            'calibration_profile': 'none', 'calibration_revision': 'a' * 64,
        },
    }


def accept(monitor, timestamp, **kwargs):
    current = view(timestamp, **kwargs)
    assert monitor.ingest(current, now=timestamp)
    return current


def test_initial_contract_and_reading_unobserved_view_do_not_start_sampling():
    monitor = RawObservationMonitor()
    empty = monitor.snapshot(now=100)
    assert set(empty) == {'schema', 'window_sec', 'max_samples', 'count', 'points',
                         'started_at', 'first_timestamp', 'last_timestamp', 'truncated',
                         'gap_count', 'conflict_count', 'rejected_count', 'latest'}
    assert empty['schema'] == 1 and empty['window_sec'] == 3600
    assert empty['max_samples'] == 4096
    assert empty['count'] == 0 and empty['points'] == []
    assert empty['started_at'] is empty['first_timestamp'] is empty['last_timestamp'] is None
    assert not empty['truncated']
    assert empty['gap_count'] == empty['conflict_count'] == empty['rejected_count'] == 0
    assert empty['latest'] == {
        'fresh': False, 'observed': False, 'timestamp': None,
        'byte_26': None, 'byte_27': None, 'byte_28': None,
        'source': None, 'mode': None, 'device_alias': None,
    }
    for _ in range(3):
        result = monitor.snapshot(view(100), now=100)
        assert result['count'] == 0 and result['started_at'] is None
        assert result['latest']['fresh'] and not result['latest']['observed']
        assert result['latest']['device_alias'] is None
        assert result['latest']['byte_26'] == 41
    assert monitor.snapshot(now=100) == empty


@pytest.mark.parametrize('params', [
    {'window_sec': 0}, {'window_sec': -1}, {'window_sec': 3601},
    {'window_sec': True}, {'window_sec': math.nan}, {'window_sec': math.inf},
    {'max_samples': 0}, {'max_samples': 4097}, {'max_samples': True},
    {'max_samples': 2.5}, {'max_samples': '4'},
])
def test_constructor_has_hard_window_and_memory_limits(params):
    with pytest.raises(ValueError):
        RawObservationMonitor(**params)


@pytest.mark.parametrize('minutes', [0, -1, 61, True, '15', math.nan, math.inf])
def test_invalid_query_window_cannot_mutate_observer(minutes):
    monitor = RawObservationMonitor()
    accept(monitor, 100)
    expected = monitor.snapshot(now=100)
    with pytest.raises(ValueError):
        monitor.snapshot(now=100, minutes=minutes)
    assert monitor.snapshot(now=100) == expected


@pytest.mark.parametrize('now', [-1, True, '100', math.nan, math.inf, 10 ** 500])
def test_invalid_now_is_a_caller_error_and_does_not_count_or_add(now):
    monitor = RawObservationMonitor()
    with pytest.raises(ValueError):
        monitor.ingest(view(100), now=now)
    with pytest.raises(ValueError):
        monitor.snapshot(now=now)
    assert monitor.snapshot(now=100)['count'] == 0


def test_default_now_uses_server_clock(monkeypatch):
    monkeypatch.setattr('ups_panel.raw_observation.time.time', lambda: 100)
    monitor = RawObservationMonitor()
    assert monitor.ingest(view(100))
    assert monitor.snapshot()['latest']['fresh']


def test_same_value_at_new_timestamp_is_valid_but_polling_one_frame_is_not():
    monitor = RawObservationMonitor()
    original = accept(monitor, 100)
    expected = monitor.snapshot(original, now=100)
    for now in (100, 100.5, 101, 102):
        duplicate = deepcopy(original)
        duplicate.update(server_time=now, heartbeat=now, arbitrary='PRIVATE-NOISE')
        duplicate['sample']['raw_fields']['frame_hex'] = 'OTHER-IGNORED-FRAME'
        assert not monitor.ingest(duplicate, now=now)
        assert monitor.snapshot(duplicate, now=now) == expected
    newer = accept(monitor, 102)
    result = monitor.snapshot(newer, now=102)
    assert result['count'] == 2
    assert [point['timestamp'] for point in result['points']] == [100, 102]
    assert [point['segment'] for point in result['points']] == [1, 1]
    assert result['latest']['fresh'] and result['latest']['observed']
    assert result['conflict_count'] == result['rejected_count'] == 0


def test_mode_changes_start_segments_and_keep_all_observed_points():
    monitor = RawObservationMonitor()
    for timestamp, mode in ((100, 'online'), (102, 'charging'), (104, 'battery'),
                            (106, 'online'), (108, 'unknown')):
        accept(monitor, timestamp, mode=mode)
    result = monitor.snapshot(now=108)
    assert result['count'] == 5
    assert [p['mode'] for p in result['points']] == ['online', 'charging', 'battery', 'online', 'unknown']
    assert [p['segment'] for p in result['points']] == [1, 2, 3, 4, 5]
    assert result['gap_count'] == 0
    assert len({p['device_alias'] for p in result['points']}) == 1


@pytest.mark.parametrize('change', [
    {'device': {'serial': 'OTHER-PRIVATE-SERIAL'}}, {'source': 'replay'},
    {'decoder': 5}, {'formula': 3},
])
def test_same_timestamp_in_new_context_is_new_point_not_conflict(change):
    monitor = RawObservationMonitor()
    accept(monitor, 100)
    changed = view(100, **change)
    assert monitor.ingest(changed, now=100)
    result = monitor.snapshot(changed, now=100)
    assert result['count'] == 2
    assert [p['segment'] for p in result['points']] == [1, 2]
    assert result['conflict_count'] == result['gap_count'] == 0
    assert result['latest']['observed']
    if 'device' in change:
        assert result['points'][0]['device_alias'] != result['points'][1]['device_alias']
    else:
        assert result['points'][0]['device_alias'] == result['points'][1]['device_alias']


def test_same_context_conflicts_do_not_replace_points_or_count_repeated_polls():
    monitor = RawObservationMonitor()
    original = accept(monitor, 100)
    for byte26 in (42, 42, 43, 43):
        assert not monitor.ingest(view(100, byte26=byte26), now=100)
    result = monitor.snapshot(now=100)
    assert result['count'] == 1 and result['conflict_count'] == 1
    assert result['points'][0]['byte_26'] == 41
    assert not result['latest']['fresh']
    current = monitor.snapshot(view(100, byte26=42), now=100)
    assert current['latest']['fresh'] and not current['latest']['observed']
    assert monitor.snapshot(original, now=100)['latest']['observed']
    accept(monitor, 102)
    result = monitor.snapshot(now=102)
    assert [p['segment'] for p in result['points']] == [1, 2]
    assert result['gap_count'] == 1


def test_revisiting_an_older_device_context_duplicate_interrupts_the_next_segment():
    monitor = RawObservationMonitor()
    original = accept(monitor, 100)
    other = {'serial': 'OTHER-PRIVATE-DEVICE'}
    accept(monitor, 100, device=other)
    assert not monitor.ingest(original, now=100)
    accept(monitor, 102, device=other)
    result = monitor.snapshot(now=102)
    assert result['count'] == 3 and result['conflict_count'] == 0
    assert [p['segment'] for p in result['points']] == [1, 2, 3]
    assert result['gap_count'] == 1


@pytest.mark.parametrize('changes', [
    {'calibration_profile': 'custom'}, {'calibration_revision': 'b' * 64},
    {'calibration_profile': 'local-19v-v1', 'calibration_revision': 'c' * 64},
])
def test_new_timestamp_calibration_changes_start_segments_without_resetting_history(changes):
    monitor = RawObservationMonitor()
    accept(monitor, 100)
    changed = view(102)
    changed['sample'].update(changes)
    assert monitor.ingest(changed, now=102)
    continuing = deepcopy(changed)
    continuing['sample']['timestamp'] = 104
    assert monitor.ingest(continuing, now=104)
    result = monitor.snapshot(now=104)
    assert [point['segment'] for point in result['points']] == [1, 2, 2]
    assert result['count'] == 3 and result['started_at'] == 100
    assert result['gap_count'] == result['conflict_count'] == 0
    assert len({point['device_alias'] for point in result['points']}) == 1
    for key, value in changes.items():
        assert result['points'][1][key] == result['points'][2][key] == value


@pytest.mark.parametrize('changes', [
    {'calibration_profile': 'custom'}, {'calibration_revision': 'b' * 64},
])
def test_same_timestamp_calibration_change_remains_a_conflict_not_a_new_context(changes):
    monitor = RawObservationMonitor()
    accept(monitor, 100)
    changed = view(100)
    changed['sample'].update(changes)
    assert not monitor.ingest(changed, now=100)
    result = monitor.snapshot(changed, now=100)
    assert result['count'] == 1 and result['conflict_count'] == 1
    assert not result['latest']['observed']
    assert result['points'][0]['calibration_profile'] == 'none'
    assert result['points'][0]['calibration_revision'] == 'a' * 64
    changed['sample']['timestamp'] = 102
    assert monitor.ingest(changed, now=102)
    result = monitor.snapshot(changed, now=102)
    assert result['count'] == 2 and result['conflict_count'] == 1
    assert [point['segment'] for point in result['points']] == [1, 2]
    assert result['gap_count'] == 1 and result['latest']['observed']


@pytest.mark.parametrize('conflict', [False, True])
def test_clock_rollback_collision_interrupts_next_segment_without_erasing_old_point(conflict):
    monitor = RawObservationMonitor()
    accept(monitor, 100)
    accept(monitor, 104)
    assert not monitor.ingest(view(100, byte26=42 if conflict else 41), now=100)
    accept(monitor, 102)
    result = monitor.snapshot(now=102)
    assert [p['timestamp'] for p in result['points']] == [100, 104, 102]
    assert [p['segment'] for p in result['points']] == [1, 1, 2]
    assert result['points'][0]['byte_26'] == 41
    assert result['conflict_count'] == int(conflict)
    assert result['gap_count'] == 1


def test_clock_rollback_without_collision_keeps_old_history_and_ages_on_elapsed_clock():
    monitor = RawObservationMonitor(window_sec=10)
    accept(monitor, 100)
    accept(monitor, 104)
    accept(monitor, 90)
    result = monitor.snapshot(now=90)
    assert [p['timestamp'] for p in result['points']] == [100, 104, 90]
    assert [p['segment'] for p in result['points']] == [1, 1, 2]
    assert result['first_timestamp'] == 100 and result['last_timestamp'] == 90
    assert result['gap_count'] == 1
    assert [p['timestamp'] for p in monitor.snapshot(now=99)['points']] == [104, 90]
    assert monitor.snapshot(now=105)['count'] == 0


def test_five_second_gap_is_allowed_and_larger_gap_starts_one_segment():
    monitor = RawObservationMonitor()
    accept(monitor, 100)
    accept(monitor, 105)
    accept(monitor, 110.001)
    accept(monitor, 112)
    result = monitor.snapshot(now=112)
    assert [p['segment'] for p in result['points']] == [1, 1, 2, 2]
    assert result['gap_count'] == 1


@pytest.mark.parametrize('interruption', [
    None, {}, {'fresh': False, 'sample': None}, view(100, fresh=False),
    view(80), view(104),  # Old/future timestamps despite a fresh flag.
])
def test_staleness_keeps_history_does_not_reject_and_breaks_resumed_continuity(interruption):
    monitor = RawObservationMonitor()
    accept(monitor, 100)
    for _ in range(5):
        assert not monitor.ingest(interruption, now=101)
    result = monitor.snapshot(now=101)
    assert result['count'] == 1 and result['rejected_count'] == 0
    assert not result['latest']['fresh']
    accept(monitor, 102)
    result = monitor.snapshot(now=102)
    assert [p['segment'] for p in result['points']] == [1, 2]
    assert result['gap_count'] == 1


@pytest.mark.parametrize('byte', [None, True, False, -1, 256, 41.0, '41', math.nan, math.inf, {}, []])
@pytest.mark.parametrize('field', ['byte_26', 'byte_27'])
def test_invalid_required_byte_is_rejected_once_per_identical_poll_and_never_zero_filled(byte, field):
    monitor = RawObservationMonitor()
    accept(monitor, 100)
    bad = view(102)
    bad['sample']['raw_fields'][field] = byte
    for _ in range(3):
        assert not monitor.ingest(bad, now=102)
    result = monitor.snapshot(bad, now=102)
    assert result['count'] == 1 and result['rejected_count'] == 1
    assert not result['latest']['fresh'] and result['latest'][field] is None
    assert result['points'][0][field] in (41, 48)
    accept(monitor, 104)
    result = monitor.snapshot(now=104)
    assert result['count'] == 2 and result['gap_count'] == 1
    assert [p['segment'] for p in result['points']] == [1, 2]


@pytest.mark.parametrize('timestamp', [None, True, -1, math.nan, math.inf, '100', 10 ** 500])
def test_malformed_sample_timestamp_is_rejected(timestamp):
    monitor = RawObservationMonitor()
    assert not monitor.ingest(view(timestamp), now=100)
    result = monitor.snapshot(now=100)
    assert result['count'] == 0 and result['rejected_count'] == 1


@pytest.mark.parametrize('source', ['unavailable', 'network', 'PRIVATE-SOURCE', None, {}, []])
def test_only_allowed_passive_sources_are_admitted(source):
    monitor = RawObservationMonitor()
    assert not monitor.ingest(view(100, source=source), now=100)
    result = monitor.snapshot(now=100)
    assert result['count'] == 0 and result['rejected_count'] == 1
    assert 'PRIVATE' not in json.dumps(result)


def test_optional_fields_are_sanitized_without_losing_valid_raw_observation():
    monitor = RawObservationMonitor()
    current = view(100, mode='unknown')
    current['sample'].update(mode={'PRIVATE-MODE': 1}, raw_status=True, soc=math.nan,
                             battery_voltage='PRIVATE-VOLTAGE', adapter_input_voltage_v=1000,
                             ups_output_voltage_v=-1, current=False,
                             battery_charge_current_candidate_a=math.inf,
                             battery_discharge_current_candidate_a={'PRIVATE-CURRENT': 1},
                             decoder_version=True, formula_version=-1,
                             calibration_profile='PRIVATE-PROFILE', calibration_revision='PRIVATE-REVISION')
    current['sample']['raw_fields']['byte_28'] = 'PRIVATE-BYTE28'
    assert monitor.ingest(current, now=100)
    result = monitor.snapshot(current, now=100)
    point = result['points'][0]
    assert point['mode'] == 'unknown'
    optional = set(POINT_FIELDS) - {'timestamp', 'segment', 'device_alias', 'source',
                                   'mode', 'byte_26', 'byte_27'}
    assert all(point[key] is None for key in optional)
    assert result['rejected_count'] == 0
    assert 'PRIVATE' not in json.dumps(result, allow_nan=False)


def test_zero_raw_bytes_and_optional_numeric_zero_are_valid():
    monitor = RawObservationMonitor()
    current = view(100, byte26=0, byte27=255)
    current['sample'].update(soc=0, battery_voltage=0, adapter_input_voltage_v=0,
                             ups_output_voltage_v=0, current=0,
                             battery_charge_current_candidate_a=0,
                             battery_discharge_current_candidate_a=0)
    current['sample']['raw_fields']['byte_28'] = 0
    assert monitor.ingest(current, now=100)
    point = monitor.snapshot(now=100)['points'][0]
    assert point['byte_26'] == point['byte_28'] == point['soc'] == point['current'] == 0
    assert point['byte_27'] == 255


@pytest.mark.parametrize('profile', ['none', 'local-19v-v1', 'custom'])
def test_only_known_profile_and_hash_revision_survive(profile):
    monitor = RawObservationMonitor()
    current = view(100)
    current['sample'].update(calibration_profile=profile, calibration_revision='0123456789abcdef' * 4)
    assert monitor.ingest(current, now=100)
    point = monitor.snapshot(now=100)['points'][0]
    assert point['calibration_profile'] == profile
    assert point['calibration_revision'] == '0123456789abcdef' * 4


@pytest.mark.parametrize('revision', ['a' * 63, 'a' * 65, 'g' * 64, 'A' * 64, '\n' + 'a' * 64, ['a']])
def test_revision_cannot_export_arbitrary_or_malformed_text(revision):
    monitor = RawObservationMonitor()
    current = view(100)
    current['sample']['calibration_revision'] = revision
    assert monitor.ingest(current, now=100)
    assert monitor.snapshot(now=100)['points'][0]['calibration_revision'] is None


def test_expiration_is_projected_during_gets_without_advancing_ingestion_state():
    monitor = RawObservationMonitor(window_sec=10)
    current = accept(monitor, 100)
    assert monitor.snapshot(current, now=110)['count'] == 1
    expired = monitor.snapshot(current, now=110.001)
    assert expired['count'] == 0
    assert expired['first_timestamp'] is expired['last_timestamp'] is None
    assert expired['started_at'] == 100
    assert expired['gap_count'] == expired['rejected_count'] == expired['conflict_count'] == 0
    assert not expired['latest']['fresh']
    # A read at a later supplied clock cannot become a background observation.
    accept(monitor, 102)
    result = monitor.snapshot(now=102)
    assert [p['timestamp'] for p in result['points']] == [100, 102]
    assert [p['segment'] for p in result['points']] == [1, 1]
    assert result['gap_count'] == 0


def test_latest_age_has_same_ten_second_freshness_boundary_as_live_snapshot():
    monitor = RawObservationMonitor()
    current = accept(monitor, 100)
    assert monitor.snapshot(now=110)['latest']['fresh']
    assert monitor.snapshot(current, now=110)['latest']['fresh']
    assert not monitor.snapshot(now=110.001)['latest']['fresh']
    assert not monitor.snapshot(current, now=110.001)['latest']['fresh']
    assert monitor.snapshot(now=110.001)['count'] == 1


def test_query_window_is_applied_to_existing_history_without_erasing_longer_window():
    monitor = RawObservationMonitor()
    for timestamp in range(0, 3601, 2):
        accept(monitor, timestamp)
    short = monitor.snapshot(now=3600, minutes=15)
    long = monitor.snapshot(now=3600, minutes=60)
    assert short['window_sec'] == 900 and short['count'] == 451
    assert short['first_timestamp'] == 2700
    assert long['window_sec'] == 3600 and long['count'] == 1801
    assert long['first_timestamp'] == long['started_at'] == 0
    assert long['last_timestamp'] == 3600
    assert not short['truncated'] and not long['truncated']


def test_capacity_discards_oldest_points_and_truncated_only_describes_affected_window():
    monitor = RawObservationMonitor(max_samples=3)
    for timestamp in range(4):
        accept(monitor, timestamp)
    result = monitor.snapshot(now=3)
    assert result['count'] == result['max_samples'] == 3
    assert [p['timestamp'] for p in result['points']] == [1, 2, 3]
    assert result['started_at'] == 0 and result['truncated']
    assert result['gap_count'] == 0
    # The discarded timestamp 0 is outside this 60-second requested window.
    short = monitor.snapshot(now=61, minutes=1)
    assert short['count'] == 3 and not short['truncated']
    assert monitor.snapshot(now=61, minutes=60)['truncated']


def test_ingestion_prunes_expired_points_even_when_input_is_stale():
    monitor = RawObservationMonitor(window_sec=10)
    accept(monitor, 100)
    assert not monitor.ingest({'fresh': False}, now=111)
    assert not monitor._records and not monitor._seen
    assert monitor.snapshot(now=111)['count'] == 0
    accept(monitor, 112)
    result = monitor.snapshot(now=112)
    assert result['count'] == 1 and result['started_at'] == 100
    assert result['points'][0]['segment'] == 2 and result['gap_count'] == 1


def test_repeated_device_churn_keeps_all_internal_indexes_bounded():
    monitor = RawObservationMonitor(window_sec=10, max_samples=3)
    for timestamp in range(100):
        accept(monitor, timestamp, device={'serial': f'PRIVATE-{timestamp}'})
    assert len(monitor._records) == len(monitor._seen) == len(monitor._aliases) == 3
    assert len(monitor._conflicted) <= 3
    result = monitor.snapshot(now=99)
    assert result['count'] == 3 and 'PRIVATE' not in json.dumps(result)


def test_expired_last_point_conflicts_cannot_grow_conflict_index_forever():
    monitor = RawObservationMonitor(window_sec=1, max_samples=3)
    for timestamp in range(100, 150, 3):
        accept(monitor, timestamp)
        monitor.ingest({'fresh': False}, now=timestamp + 2)
        monitor.ingest(view(timestamp, byte26=42), now=timestamp + 2)
        assert len(monitor._conflicted) <= 1
    assert monitor.snapshot(now=150)['conflict_count'] > 0


def test_safe_fixed_point_contract_cannot_export_private_nested_metadata():
    monitor = RawObservationMonitor()
    current = view(100)
    current.update(errors=['PRIVATE-ERROR'], nut={'target': 'PRIVATE-NUT-TARGET'},
                   device_alias='PRIVATE-FAKE-ALIAS', source_details={'address': 'PRIVATE-HOST'})
    current['sample'].update(serial='PRIVATE-SAMPLE-SERIAL', frame_hex='PRIVATE-HEX',
                             error='PRIVATE-FREE-TEXT', device_alias='PRIVATE-SAMPLE-ALIAS',
                             calibration_coefficients={'private': 'PRIVATE-COEFFICIENT'},
                             temperature=99, unit='°C')
    assert monitor.ingest(current, now=100)
    result = monitor.snapshot(current, now=100)
    assert tuple(result['points'][0]) == POINT_FIELDS
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    assert 'PRIVATE' not in encoded
    assert all(forbidden not in encoded for forbidden in ('serial', 'frame_hex', 'path', 'errors', 'temperature', '°C'))
    alias = result['points'][0]['device_alias']
    assert re.fullmatch(r'device-[0-9a-f]{16}', alias)
    assert result['latest']['device_alias'] == alias
    # A separate process/observer does not expose a stable hash of the serial.
    other = RawObservationMonitor()
    assert other.ingest(current, now=100)
    assert other.snapshot(now=100)['points'][0]['device_alias'] != alias


def test_malformed_or_oversized_device_identity_is_rejected_without_exception_or_leak():
    for device in ({'serial': 'PRIVATE' * 2000}, {'serial': math.nan}, {'weird': object()}):
        monitor = RawObservationMonitor()
        assert not monitor.ingest(view(100, device=device), now=100)
        result = monitor.snapshot(now=100)
        assert result['rejected_count'] == 1
        assert 'PRIVATE' not in json.dumps(result)
    cyclic = {}
    cyclic['self'] = cyclic
    monitor = RawObservationMonitor()
    assert not monitor.ingest(view(100, device=cyclic), now=100)


def test_no_raw_or_identity_reference_can_be_mutated_through_inputs_or_results():
    monitor = RawObservationMonitor()
    current = accept(monitor, 100)
    saved = monitor.snapshot(now=100)
    current['sample']['raw_fields']['byte_26'] = 255
    current['device']['serial'] = 'PRIVATE-CHANGED'
    output = monitor.snapshot(now=100)
    output['points'][0]['byte_26'] = 0
    output['points'].append({'private': 'PRIVATE-RESULT-MUTATION'})
    output['latest']['device_alias'] = 'PRIVATE-ALIAS-MUTATION'
    assert monitor.snapshot(now=100) == saved


def test_concurrent_duplicate_ingestion_and_reads_preserve_one_point_and_alias():
    monitor = RawObservationMonitor()
    current = view(100)
    with ThreadPoolExecutor(max_workers=8) as pool:
        added = list(pool.map(lambda _: monitor.ingest(current, now=100), range(80)))
        outputs = list(pool.map(lambda _: monitor.snapshot(current, now=100), range(40)))
    assert sum(added) == 1
    assert all(output == outputs[0] for output in outputs)
    assert outputs[0]['count'] == 1
    assert outputs[0]['conflict_count'] == outputs[0]['rejected_count'] == 0
