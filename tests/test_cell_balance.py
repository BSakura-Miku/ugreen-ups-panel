from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from ups_panel.cell_balance import CellBalanceMonitor


def view(timestamp, *, mode='online', cells=None, fresh=True, source='usbmon',
         device=None, decoder=1, revision='power-one'):
    return {
        'fresh': fresh, 'source': source,
        'device': {'serial': 'A', 'bus': 1, 'device': 2} if device is None else device,
        'sample': {'timestamp': timestamp, 'mode': mode,
                   'cells': [3.9, 3.91, 3.91, 3.91] if cells is None else cells,
                   'decoder_version': decoder, 'calibration_revision': revision,
                   # Deliberately wrong: the monitor must recompute from cells.
                   'cell_delta_mv': 999},
    }


def observe(monitor, start=0, end=1800, step=2, **kwargs):
    latest = None
    for timestamp in range(start, end + 1, step):
        latest = view(timestamp, **kwargs)
        monitor.ingest(latest)
    return latest


def test_contract_is_unavailable_until_fresh_valid_cells_exist():
    monitor = CellBalanceMonitor()
    result = monitor.snapshot({'fresh': False, 'sample': None})
    assert result == {
        'schema': 1, 'state': 'unavailable', 'level': None, 'delta_mv': None,
        'standby_duration_sec': 0, 'required_standby_sec': 1800, 'persistence_sec': 120,
        'candidate_level': None, 'candidate_duration_sec': 0, 'lowest_cells': [],
        'frequent_lowest_cell': None, 'recent_max_delta_mv': None,
        'recent_window_sec': 1800, 'recent_sample_count': 0,
        'sample_timestamp': None, 'observed': False, 'reason': 'no_fresh_sample',
        'thresholds': {'good_below_mv': 20, 'minor_below_mv': 50, 'elevated_below_mv': 100},
        'reference_only': True,
    }
    latest = view(0)
    assert monitor.snapshot(latest)['state'] == 'observing'
    assert monitor.snapshot(latest)['reason'] == 'not_observed'
    assert monitor.snapshot(latest)['delta_mv'] == 10


@pytest.mark.parametrize('delta,expected', [
    (0, 'good'), (19.999, 'good'), (20, 'minor'), (20.001, 'minor'),
    (49.999, 'minor'), (50, 'elevated'), (50.001, 'elevated'),
    (99.999, 'elevated'), (100, 'check'), (100.001, 'check'),
])
def test_reference_bands_use_recomputed_cells_with_exact_float_boundaries(delta, expected):
    monitor = CellBalanceMonitor()
    latest = observe(monitor, cells=[3.9, 3.9 + delta / 1000, 3.9, 3.9])
    result = monitor.snapshot(latest)
    assert result['state'] == 'assessed' and result['level'] == expected
    assert result['delta_mv'] == pytest.approx(delta, abs=1e-8)
    assert result['delta_mv'] != latest['sample']['cell_delta_mv']


def test_thirty_minutes_are_required_but_candidate_can_confirm_during_settling():
    monitor = CellBalanceMonitor()
    latest = observe(monitor, end=1798)
    result = monitor.snapshot(latest)
    assert result['state'] == 'settling' and result['level'] is None
    assert result['standby_duration_sec'] == result['candidate_duration_sec'] == 1798
    assert result['candidate_level'] == 'good'
    latest = view(1800)
    monitor.ingest(latest)
    result = monitor.snapshot(latest)
    assert result['state'] == 'assessed' and result['level'] == 'good'
    assert result['recent_sample_count'] == 901
    # A restart requires a complete new standby observation, not old history.
    restarted = CellBalanceMonitor()
    restarted.ingest(latest)
    assert restarted.snapshot(latest)['standby_duration_sec'] == 0
    assert restarted.snapshot(latest)['state'] == 'settling'


def test_duplicate_ingest_and_repeated_gets_do_not_add_time_or_samples():
    monitor = CellBalanceMonitor()
    latest = observe(monitor, end=100)
    expected = monitor.snapshot(latest)
    for server_time in range(102, 150):
        duplicate = deepcopy(latest)
        duplicate['server_time'] = server_time
        monitor.ingest(duplicate)
        assert monitor.snapshot(duplicate) == expected
    assert expected['recent_sample_count'] == 51
    assert expected['standby_duration_sec'] == 100


@pytest.mark.parametrize('changes', [
    {'mode': 'charging'}, {'mode': 'battery'}, {'mode': 'unknown'},
    {'fresh': False}, {'source': 'other'}, {'device': {'serial': 'B'}},
    {'decoder': 2}, {'cells': [3.9, 4.1, 4.1, 4.1]},
])
def test_same_timestamp_with_a_different_view_cannot_show_a_previous_grade(changes):
    monitor = CellBalanceMonitor()
    latest = observe(monitor)
    old = monitor.snapshot(latest)
    replacement = view(1800, **changes)
    result = monitor.snapshot(replacement)
    assert result['level'] is None and result['observed'] is False
    assert result['standby_duration_sec'] == 0 and result['recent_sample_count'] == 0
    # A read of another view cannot rewrite the monitor's actual observations.
    assert monitor.snapshot(latest) == old


def test_new_and_older_unobserved_samples_are_not_graded_until_background_ingest():
    monitor = CellBalanceMonitor()
    latest = observe(monitor)
    for timestamp in (1798, 1802):
        result = monitor.snapshot(view(timestamp))
        assert result['state'] == 'observing' and result['reason'] == 'not_observed'
        assert result['level'] is None and result['candidate_level'] is None
        assert result['standby_duration_sec'] == 0 and result['delta_mv'] == 10
    assert monitor.snapshot(latest)['standby_duration_sec'] == 1800
    next_view = view(1802)
    monitor.ingest(next_view)
    assert monitor.snapshot(next_view)['state'] == 'assessed'
    assert monitor.snapshot(next_view)['standby_duration_sec'] == 1802


@pytest.mark.parametrize('interruption', [
    {'fresh': False, 'sample': None}, view(102, fresh=False), view(102, mode='unknown'),
    view(102, cells=[3.9, 3.9, 3.9]), view(102, cells=[3.9, 3.9, 3.9, 6]),
    view(102, mode='charging'), view(102, mode='battery'),
])
def test_interruption_discards_continuity_candidates_and_recent_window(interruption):
    monitor = CellBalanceMonitor()
    observe(monitor, end=100)
    monitor.ingest(interruption)
    resumed = view(104)
    monitor.ingest(resumed)
    result = monitor.snapshot(resumed)
    assert result['state'] == 'settling'
    assert result['standby_duration_sec'] == result['candidate_duration_sec'] == 0
    assert result['recent_sample_count'] == 1


@pytest.mark.parametrize('changes', [
    {'source': 'other'}, {'device': {'serial': 'B'}}, {'decoder': 2},
])
def test_changed_source_device_or_decoder_restarts_online_observation(changes):
    monitor = CellBalanceMonitor()
    observe(monitor, end=100)
    latest = view(102, **changes)
    monitor.ingest(latest)
    result = monitor.snapshot(latest)
    assert result['standby_duration_sec'] == 0 and result['recent_sample_count'] == 1


def test_power_calibration_metadata_does_not_change_cell_observation_identity():
    monitor = CellBalanceMonitor()
    latest = observe(monitor)
    changed = deepcopy(latest)
    changed['sample'].update(calibration_revision='power-two', ac_voltage_nominal_v=12,
                             formula_version=99, calibration_coefficients={'base_gain': 1})
    assert monitor.snapshot(changed)['state'] == 'assessed'
    monitor.ingest(changed)
    assert monitor.snapshot(changed)['recent_sample_count'] == 901
    changed['sample']['timestamp'] = 1802
    monitor.ingest(changed)
    assert monitor.snapshot(changed)['standby_duration_sec'] == 1802


def test_five_second_gap_is_allowed_but_larger_gap_and_time_reversal_reset():
    monitor = CellBalanceMonitor()
    monitor.ingest(view(0))
    monitor.ingest(view(5))
    assert monitor.snapshot(view(5))['standby_duration_sec'] == 5
    monitor.ingest(view(10.001))
    assert monitor.snapshot(view(10.001))['standby_duration_sec'] == 0
    monitor.ingest(view(9))
    assert monitor.snapshot(view(9))['standby_duration_sec'] == 0
    assert monitor.snapshot(view(9))['recent_sample_count'] == 1


def test_conflicting_cells_at_duplicate_timestamp_reset_instead_of_reusing_candidate():
    monitor = CellBalanceMonitor()
    observe(monitor)
    changed = view(1800, cells=[3.9, 4.01, 4.01, 4.01])
    monitor.ingest(changed)
    result = monitor.snapshot(changed)
    assert result['state'] == 'settling' and result['level'] is None
    assert result['standby_duration_sec'] == result['candidate_duration_sec'] == 0
    assert result['candidate_level'] == 'check' and result['recent_sample_count'] == 1


@pytest.mark.parametrize('mode,state', [('charging', 'charging'), ('battery', 'discharging')])
def test_charging_and_battery_show_current_delta_without_standby_grade(mode, state):
    monitor = CellBalanceMonitor()
    observe(monitor)
    latest = view(1802, mode=mode, cells=[3.9, 4, 4, 4])
    monitor.ingest(latest)
    result = monitor.snapshot(latest)
    assert result['state'] == state and result['delta_mv'] == 100
    assert result['level'] is result['candidate_level'] is None
    assert result['standby_duration_sec'] == result['candidate_duration_sec'] == 0
    assert result['recent_max_delta_mv'] is result['frequent_lowest_cell'] is None
    assert result['recent_sample_count'] == 0 and result['lowest_cells'] == [1]


def test_grade_changes_require_full_persistence_and_do_not_keep_old_good_label():
    monitor = CellBalanceMonitor()
    observe(monitor)
    latest = observe(monitor, 1802, 1920, cells=[3.9, 3.93, 3.93, 3.93])
    result = monitor.snapshot(latest)
    assert result['state'] == 'observing' and result['level'] is None
    assert result['candidate_level'] == 'minor' and result['candidate_duration_sec'] == 118
    latest = view(1922, cells=[3.9, 3.93, 3.93, 3.93])
    monitor.ingest(latest)
    assert monitor.snapshot(latest)['level'] == 'minor'
    latest = view(1924)
    monitor.ingest(latest)
    result = monitor.snapshot(latest)
    assert result['state'] == 'observing' and result['level'] is None
    assert result['candidate_level'] == 'good' and result['candidate_duration_sec'] == 0


def test_one_sample_spike_is_never_published_as_confirmed_check_level():
    monitor = CellBalanceMonitor()
    observe(monitor)
    spike = view(1802, cells=[3.9, 4.01, 4.01, 4.01])
    monitor.ingest(spike)
    assert monitor.snapshot(spike)['level'] is None
    latest = observe(monitor, 1804, 1922)
    assert monitor.snapshot(latest)['state'] == 'observing'
    monitor.ingest(view(1924))
    assert monitor.snapshot(view(1924))['level'] == 'good'


def test_recent_max_and_sample_count_cover_only_last_thirty_minutes_of_standby():
    monitor = CellBalanceMonitor()
    monitor.ingest(view(0, cells=[3.9, 4.01, 4.01, 4.01]))
    latest = observe(monitor, 2, 1800)
    assert monitor.snapshot(latest)['recent_max_delta_mv'] == 110
    latest = view(1802)
    monitor.ingest(latest)
    result = monitor.snapshot(latest)
    assert result['recent_max_delta_mv'] == 10 and result['recent_sample_count'] == 901
    assert result['standby_duration_sec'] == 1802 and result['recent_window_sec'] == 1800


def test_frequent_lowest_counts_only_unique_low_cells_and_returns_null_on_ties():
    monitor = CellBalanceMonitor()
    first = view(0, cells=[3.9, 3.94, 3.94, 3.94])
    monitor.ingest(first)
    for _ in range(10):
        monitor.ingest(first)
    second = view(2, cells=[3.94, 3.9, 3.94, 3.94])
    monitor.ingest(second)
    assert monitor.snapshot(second)['frequent_lowest_cell'] is None
    tied_cells = view(4, cells=[3.9, 3.9, 3.94, 3.94])
    monitor.ingest(tied_cells)
    assert monitor.snapshot(tied_cells)['lowest_cells'] == [1, 2]
    assert monitor.snapshot(tied_cells)['frequent_lowest_cell'] is None
    all_equal = view(6, cells=[3.9] * 4)
    monitor.ingest(all_equal)
    assert monitor.snapshot(all_equal)['lowest_cells'] == []
    assert monitor.snapshot(all_equal)['frequent_lowest_cell'] is None
    final = view(8, cells=[3.94, 3.9, 3.94, 3.94])
    monitor.ingest(final)
    result = monitor.snapshot(final)
    assert result['frequent_lowest_cell'] == 2 and result['recent_sample_count'] == 5


def test_frequent_lowest_discards_votes_older_than_current_thirty_minute_window():
    monitor = CellBalanceMonitor()
    observe(monitor, 0, 900, cells=[3.9, 3.94, 3.94, 3.94])
    latest = observe(monitor, 902, 1800, cells=[3.94, 3.9, 3.94, 3.94])
    assert monitor.snapshot(latest)['frequent_lowest_cell'] == 1
    latest = view(1802, cells=[3.94, 3.9, 3.94, 3.94])
    monitor.ingest(latest)
    result = monitor.snapshot(latest)
    assert result['frequent_lowest_cell'] == 2
    assert result['recent_sample_count'] == 901


@pytest.mark.parametrize('timestamp', [True, None, '2', -1, float('nan'), float('inf')])
def test_invalid_timestamps_clear_observation(timestamp):
    monitor = CellBalanceMonitor()
    monitor.ingest(view(0))
    invalid = view(timestamp)
    monitor.ingest(invalid)
    assert monitor.snapshot(invalid)['state'] == 'unavailable'
    assert monitor.snapshot(invalid)['delta_mv'] is None
    monitor.ingest(view(2))
    assert monitor.snapshot(view(2))['standby_duration_sec'] == 0


@pytest.mark.parametrize('cells', [
    [], [4] * 3, [4] * 5, (4, 4, 4, 4), ['4', 4, 4, 4],
    [True, 4, 4, 4], [0.999, 4, 4, 4], [5.001, 4, 4, 4],
    [float('nan'), 4, 4, 4], [float('inf'), 4, 4, 4],
])
def test_invalid_four_cell_input_is_unavailable_and_breaks_standby(cells):
    monitor = CellBalanceMonitor()
    monitor.ingest(view(0))
    invalid = view(2, cells=cells)
    monitor.ingest(invalid)
    result = monitor.snapshot(invalid)
    assert result['state'] == 'unavailable' and result['reason'] == 'invalid_sample'
    assert result['delta_mv'] is None and result['lowest_cells'] == []
    monitor.ingest(view(4))
    assert monitor.snapshot(view(4))['standby_duration_sec'] == 0


def test_valid_cell_endpoints_and_nonfinite_identity_handling():
    monitor = CellBalanceMonitor()
    latest = view(0, cells=[1, 5, 5, 5])
    monitor.ingest(latest)
    assert monitor.snapshot(latest)['delta_mv'] == 4000
    invalid = view(2, device={'serial': float('nan')})
    monitor.ingest(invalid)
    assert monitor.snapshot(invalid)['state'] == 'unavailable'


def test_unexpected_dense_sampling_is_bounded_and_does_not_claim_a_partial_window():
    monitor = CellBalanceMonitor()
    for index in range(5000):
        latest = view(index / 10)
        monitor.ingest(latest)
    result = monitor.snapshot(latest)
    assert result['recent_sample_count'] <= 4096
    assert result['standby_duration_sec'] < 499.9
    assert result['level'] is None and result['state'] == 'settling'


def test_concurrent_reads_return_independent_dicts_without_mutating_monitor():
    monitor = CellBalanceMonitor()
    latest = observe(monitor)
    expected = monitor.snapshot(latest)

    def read_and_edit(_):
        result = monitor.snapshot(latest)
        assert result == expected
        result['thresholds']['good_below_mv'] = -1
        result['lowest_cells'].append(99)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(read_and_edit, range(80)))
    assert monitor.snapshot(latest) == expected
