import copy
import json
import math
import sqlite3

import pytest

from ups_panel.battery_capacity import BatteryCapacity, CRITERIA, MAX_RECENT
from ups_panel.calibration import default_config, normalize_config


def configuration(gain=2, base=1):
    return normalize_config({'schema': 2, 'profile': 'custom', 'ac_voltage_nominal_v': 12,
                             'coefficients': {'base_gain': base, 'charge_gain': None, 'battery_gain': gain}})


def view(ts, power=10, *, soc=91, mode='battery', config=None, device=None):
    config = configuration() if config is None else config
    profile = config['profile']
    sample = {'timestamp': ts, 'mode': mode, 'soc': soc, 'battery_voltage': 15,
              'cells': [3.74, 3.75, 3.75, 3.76], 'battery_discharge_power_candidate_w': power,
              'battery_energy_estimate_w': 999, 'calibration_profile': profile,
              'calibration_revision': config['revision'], 'calibration_coefficients': config['coefficients'],
              'calibration_schema': config['schema'], 'decoder_version': 4, 'formula_version': 2,
              'battery_estimate_basis': ('us3000_battery_custom_' + config['revision'] if profile == 'custom' else
                                         'nominal_43_2wh_soc_v1' if profile == 'local-19v-v1' else None)}
    if config['schema'] == 2:
        sample['ac_voltage_nominal_v'] = config['ac_voltage_nominal_v']
    return {'fresh': True, 'source': 'usbmon', 'sample': sample,
            'device': {'serial': 'CAPACITY-TEST', 'vendor': '1234', 'product': '5678',
                       'bus': 1, 'device': 2, 'path': '1-1'} if device is None else device}


@pytest.fixture
def db():
    connection = sqlite3.connect(':memory:')
    connection.execute('CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT)')
    connection.execute('PRAGMA user_version=2')
    yield connection
    connection.close()


def commit(model, db):
    with db:
        model.write(db)
    model.committed()


def window(model, start=100, *, seconds_per_pp=10, power=10, external=True, config=None):
    if external:
        model.ingest(view(start - 4, mode='online', config=config))
    model.ingest(view(start - 2, config=config))
    last = None
    for elapsed in range(0, seconds_per_pp * 10 + 1, 2):
        value = power(elapsed) if callable(power) else power
        last = view(start + elapsed, value, soc=90 - elapsed // seconds_per_pp, config=config)
        model.ingest(last)
    return last


def result(model, last):
    return model.snapshot(last, now=last['sample']['timestamp'])


@pytest.mark.parametrize('duplicate', [False, True])
@pytest.mark.parametrize('restart', [False, True])
def test_explicit_calibration_rejection_abandons_current_window_not_reference(db, duplicate, restart):
    model = BatteryCapacity(db)
    window(model)
    baseline = copy.deepcopy(model.state['baseline'])
    for item in (view(396, mode='online'), view(398), view(400, soc=90), view(402, soc=90)):
        model.ingest(item)
    assert model.active and model.active['duration_sec'] == 2
    rejected = view(402 if duplicate else 403, soc=90)
    rejected['calibration_validation'] = {'valid': False, 'reason': 'config_mismatch'}
    model.ingest(rejected)
    assert model.active is None and model.previous is None and model.blocked
    assert model.state['baseline'] == baseline and model.state['reason'] == 'invalid_basis'
    if restart:
        commit(model, db)
        model = BatteryCapacity(db)
    model.ingest(view(404, soc=90))
    assert model.active is None and model.previous is None and model.blocked
    assert model.state['baseline'] == baseline
    # A new observed external-power-separated cycle is still eligible.
    window(model, start=500)
    assert model.state['baseline'] == baseline and model.state['recent'][-1]['accepted']


def test_stage_is_fixed_before_first_real_window_and_never_uses_nominal_capacity(db):
    model = BatteryCapacity(db)
    initial = view(10, mode='online')
    model.ingest(initial)
    before = result(model, initial)
    assert before['status'] == 'waiting_reference' and before['baseline'] is None
    assert before['epoch']['activated_at'] == 10
    last = window(model)
    snapshot = result(model, last)
    baseline = snapshot['baseline']
    assert snapshot['epoch'] == before['epoch']
    assert baseline['start_ts'] == 100 and baseline['end_ts'] == 200
    assert baseline['estimate_wh'] == pytest.approx(20 * 100 / 3600)
    assert baseline['index_pct'] == 100 and baseline['change_pct'] == 0
    assert baseline['start_soc'] == 90 and baseline['end_soc'] == 80
    assert baseline['duration_sec'] == 100 and baseline['coverage_ratio'] == 1
    assert baseline['avg_power_w'] == 20 and baseline['power_cv'] == 0
    assert baseline['max_cell_delta_mv'] == pytest.approx(20)
    assert baseline['min_battery_voltage_v'] == baseline['max_battery_voltage_v'] == 15
    assert snapshot['status'] == 'reference_ready' and snapshot['comparison'] is None
    assert snapshot['recent'] == [] and snapshot['temperature_known'] is False
    assert snapshot['criteria'] == CRITERIA


def test_irregular_time_trapezoids_and_time_weighted_linear_power_variance(db):
    model = BatteryCapacity(db)
    model.ingest(view(98))
    model.ingest(view(100, soc=90))
    elapsed = 0
    intervals = 0
    while elapsed < 100:
        elapsed += (1, 4, 2, 3)[intervals % 4]
        last = view(100 + elapsed, 10 + elapsed / 50, soc=90 - elapsed // 10)
        last['sample']['battery_voltage'] = 15 - elapsed / 100
        model.ingest(last)
        intervals += 1
    baseline = result(model, last)['baseline']
    assert baseline['estimate_wh'] == pytest.approx(22 * 100 / 3600)
    assert baseline['avg_power_w'] == pytest.approx(22)
    assert baseline['power_cv'] == pytest.approx((4 / math.sqrt(12)) / 22)
    assert baseline['min_power_w'] == 20 and baseline['max_power_w'] == 24
    assert baseline['interval_count'] == intervals and baseline['sample_count'] == intervals + 1
    assert baseline['min_battery_voltage_v'] == 14 and baseline['max_battery_voltage_v'] == 15


def test_reference_never_rolls_and_three_independent_matches_use_median(db):
    model = BatteryCapacity(db)
    window(model)
    baseline = copy.deepcopy(model.state['baseline'])
    for index, duration in enumerate((8, 12, 14)):
        last = window(model, 300 + index * 200, seconds_per_pp=duration)
        snapshot = result(model, last)
        assert snapshot['status'] == ('trend' if index == 2 else 'preliminary')
        assert snapshot['comparison']['sample_count'] == index + 1
        assert snapshot['baseline'] == baseline
    assert snapshot['comparison']['index_pct'] == pytest.approx(120)
    assert snapshot['comparison']['change_pct'] == pytest.approx(20)
    assert snapshot['comparison']['latest_end_ts'] == 840
    assert [item['index_pct'] for item in snapshot['recent']] == pytest.approx([140, 120, 80])
    last = window(model, 1000, power=12)
    snapshot = result(model, last)
    assert snapshot['recent'][0]['accepted'] is False
    assert snapshot['recent'][0]['reason'] == 'load_mismatch'
    assert snapshot['comparison']['index_pct'] == pytest.approx(120)


def test_matching_power_boundary_and_time_weighted_cv_filter(db):
    model = BatteryCapacity(db)
    window(model)
    last = window(model, 300, power=11)
    assert result(model, last)['recent'][0]['accepted'] is True
    last = window(model, 500, power=lambda elapsed: 5 if elapsed < 50 else 15)
    candidate = result(model, last)['recent'][0]
    assert candidate['reason'] == 'unstable_load' and not candidate['accepted']
    assert candidate['power_cv'] > 0.15
    assert result(model, last)['comparison']['sample_count'] == 1


def test_short_or_unstable_first_window_cannot_establish_a_reference(db):
    model = BatteryCapacity(db)
    last = window(model, seconds_per_pp=2)
    snapshot = result(model, last)
    assert snapshot['baseline'] is None
    assert snapshot['recent'][0]['reason'] == 'short_window'
    last = window(model, 300, power=lambda elapsed: 5 if elapsed < 50 else 15)
    assert result(model, last)['baseline'] is None
    assert result(model, last)['recent'][0]['reason'] == 'unstable_load'
    last = window(model, 500, seconds_per_pp=6)
    assert result(model, last)['baseline']['duration_sec'] == 60


def test_starting_during_battery_is_allowed_only_with_observed_start_crossing(db):
    model = BatteryCapacity(db)
    last = window(model, external=False)
    assert result(model, last)['baseline'] is not None
    model = BatteryCapacity(db)
    model.ingest(view(10, soc=90))
    assert result(model, view(10, soc=90))['reason'] == 'missed_start'
    last = window(model, 100, external=False)
    assert result(model, last)['baseline'] is None
    last = window(model, 300)
    assert result(model, last)['baseline']['start_ts'] == 300


@pytest.mark.parametrize('kind,expected', [('gap', 'sample_gap'), ('stale', 'stale'),
                                         ('rollback', 'timestamp_rollback'), ('rebound', 'soc_rebound'),
                                         ('soc_jump', 'soc_jump'), ('missed_end', 'missed_end'),
                                         ('invalid_soc', 'invalid_soc'), ('unknown', 'unknown_mode'),
                                         ('power', 'invalid_power'), ('external', 'external_before_end')])
def test_an_interrupted_window_is_discarded_and_never_reconstructed(db, kind, expected):
    model = BatteryCapacity(db)
    for item in (view(98), view(100, soc=90), view(102, soc=89)):
        model.ingest(item)
    bad = view(104, soc=88)
    if kind == 'gap':
        bad['sample']['timestamp'] = 108
    elif kind == 'stale':
        bad['fresh'] = False
    elif kind == 'rollback':
        bad['sample']['timestamp'] = 101
    elif kind in ('rebound', 'soc_jump', 'missed_end', 'invalid_soc'):
        bad['sample']['soc'] = {'rebound': 90, 'soc_jump': 87, 'missed_end': 79, 'invalid_soc': None}[kind]
    elif kind == 'unknown':
        bad['sample']['mode'] = 'unknown'
    elif kind == 'power':
        bad['sample']['battery_discharge_power_candidate_w'] = None
    else:
        bad['sample']['mode'] = 'online'
    model.ingest(bad)
    assert model.active is None and model.state['reason'] == expected
    assert model.state['baseline'] is None and model.state['recent'] == []
    for ts, soc in ((110, 82), (112, 81), (114, 80)):
        model.ingest(view(ts, soc=soc))
    assert model.state['baseline'] is None
    last = window(model, 200)
    assert result(model, last)['baseline']['start_ts'] == 200


def test_skipped_start_and_repeated_cycle_without_external_cannot_make_observations(db):
    model = BatteryCapacity(db)
    model.ingest(view(98, soc=91))
    model.ingest(view(100, soc=89))
    assert model.state['reason'] == 'missed_start'
    window(model, 200)
    baseline = copy.deepcopy(model.state['baseline'])
    window(model, 302, external=False)
    assert model.state['recent'] == [] and model.state['baseline'] == baseline


def test_jump_landing_exactly_on_start_threshold_is_not_a_valid_crossing(db):
    model = BatteryCapacity(db)
    model.ingest(view(98, soc=93))
    model.ingest(view(100, soc=90))
    assert model.active is None and model.state['reason'] == 'soc_jump'
    for elapsed in range(2, 102, 2):
        model.ingest(view(100 + elapsed, soc=90 - elapsed // 10))
    assert model.state['baseline'] is None and model.state['recent'] == []
    last = window(model, 300)
    assert result(model, last)['baseline']['start_ts'] == 300


@pytest.mark.parametrize('bad', [None, True, '20', 0, -1, float('nan'), float('inf'), 10 ** 400, 1e308])
def test_invalid_or_overflowing_power_never_reaches_persistent_numbers(db, bad):
    model = BatteryCapacity(db)
    model.ingest(view(98))
    model.ingest(view(100, soc=90))
    model.ingest(view(102, bad, soc=89))
    assert model.state['reason'] in ('invalid_power', 'invalid_energy')
    assert model.active is None and model.state['baseline'] is None
    commit(model, db)
    payload = db.execute('SELECT payload FROM battery_capacity_state').fetchone()[0]
    assert 'NaN' not in payload and 'Infinity' not in payload


@pytest.mark.parametrize('config', [default_config(), configuration(None)])
def test_unconfigured_profiles_never_create_a_stage_or_fallback_gain(db, config):
    model = BatteryCapacity(db)
    last = window(model, config=config)
    snapshot = result(model, last)
    assert snapshot['status'] == 'not_configured' and snapshot['epoch'] is None
    assert snapshot['baseline'] is None and snapshot['comparison'] is None


@pytest.mark.parametrize('field', ['calibration_revision', 'calibration_coefficients', 'battery_estimate_basis',
                                  'decoder_version', 'formula_version', 'device', 'source'])
def test_missing_basis_invalidates_window_and_does_not_borrow_previous_metadata(db, field):
    model = BatteryCapacity(db)
    model.ingest(view(98))
    model.ingest(view(100, soc=90))
    bad = view(102, soc=89)
    del (bad if field in ('device', 'source') else bad['sample'])[field]
    model.ingest(bad)
    assert model.state['reason'] == 'invalid_basis' and model.active is None


def test_same_serial_survives_usb_reenumeration_but_other_basis_needs_explicit_reset(db):
    model = BatteryCapacity(db)
    model.ingest(view(98))
    model.ingest(view(100, soc=90))
    same = view(102, soc=90)
    same['device'].update(bus=3, device=99, path='3-4')
    model.ingest(same)
    assert model.active is not None
    different = view(104, soc=89, config=configuration(3))
    model.ingest(different)
    snapshot = result(model, different)
    assert snapshot['status'] == 'incompatible' and snapshot['progress'] is None
    assert snapshot['epoch']['basis']['battery_gain'] == 2
    assert snapshot['current_basis']['battery_gain'] == 3


def test_without_serial_device_path_and_bus_are_conservative_context_boundaries(db):
    model = BatteryCapacity(db)
    device = {'path': '1-1', 'bus': 1, 'device': 2}
    model.ingest(view(98, device=device))
    model.ingest(view(100, soc=90, device=device))
    model.ingest(view(102, soc=89, device=dict(device, path='1-2')))
    assert model.state['reason'] == 'context_changed' and model.active is None


def test_duplicate_samples_do_not_mutate_counts_anchors_or_candidate_state(db):
    model = BatteryCapacity(db)
    model.ingest(view(98))
    model.ingest(view(100, soc=90))
    model.ingest(view(102, soc=90))
    before = copy.deepcopy(model.__dict__)
    model.ingest(view(102, 999, soc=80, config=configuration(3)))
    assert model.__dict__ == before


def test_persistence_retains_reference_but_restart_never_resumes_a_partial_window(db):
    model = BatteryCapacity(db)
    window(model)
    model.ingest(view(296, mode='online'))
    model.ingest(view(298))
    model.ingest(view(300, soc=90))
    model.ingest(view(302, soc=89))
    commit(model, db)
    baseline = copy.deepcopy(model.state['baseline'])
    resumed = BatteryCapacity(db)
    assert resumed.state['baseline'] == baseline and resumed.active is None
    assert resumed.state['reason'] == 'restart'
    for index in range(9):
        resumed.ingest(view(304 + index * 2, soc=88 - index))
    assert resumed.state['recent'] == []
    last = window(resumed, 400)
    assert result(resumed, last)['comparison']['sample_count'] == 1
    assert resumed.state['baseline'] == baseline


def test_current_and_archived_epochs_commit_atomically_and_failed_write_can_retry(db):
    model = BatteryCapacity(db)
    window(model)
    commit(model, db)
    old_state = db.execute('SELECT payload FROM battery_capacity_state').fetchone()[0]
    old_id = model.state['epoch']['id']
    changed = view(300, mode='online', config=configuration(3))
    new = model.reset(changed, expected_epoch_id=old_id)
    assert new['epoch']['id'] != old_id and new['baseline'] is None
    with pytest.raises(sqlite3.OperationalError):
        with db:
            model.write(db)
            raise sqlite3.OperationalError('rollback')
    assert db.execute('SELECT payload FROM battery_capacity_state').fetchone()[0] == old_state
    assert db.execute('SELECT COUNT(*) FROM battery_capacity_epochs').fetchone()[0] == 0
    assert model.dirty and old_id in model.pending_archives
    commit(model, db)
    commit(model, db)
    archive = json.loads(db.execute('SELECT payload FROM battery_capacity_epochs').fetchone()[0])
    assert archive['epoch']['id'] == old_id and archive['baseline']['index_pct'] == 100
    restored = BatteryCapacity(db)
    assert restored.state['epoch'] == new['epoch'] and restored.state['baseline'] is None
    assert restored.pending_archives == {} and model.pending_archives == {}


@pytest.mark.parametrize('kind,reason', [('cas', 'reference_changed'), ('stale', 'stale'),
                                      ('timestamp', 'invalid_timestamp'), ('aged', 'stale'),
                                      ('config', 'not_configured')])
def test_reset_checks_inputs_before_mutation(db, kind, reason):
    model = BatteryCapacity(db)
    window(model)
    new = view(300, mode='online')
    expected = model.state['epoch']['id']
    if kind == 'cas':
        expected = 'old-request'
    elif kind == 'stale':
        new['fresh'] = False
    elif kind == 'timestamp':
        new['sample']['timestamp'] = None
    elif kind == 'aged':
        new['server_time'] = 311
    else:
        new = view(300, config=default_config())
    before = copy.deepcopy(model.__dict__)
    with pytest.raises(ValueError, match=reason):
        model.reset(new, expected_epoch_id=expected)
    assert model.__dict__ == before


def test_snapshot_is_detached_read_only_and_hides_unobserved_or_stale_progress(db):
    model = BatteryCapacity(db)
    last = window(model)
    model.ingest(view(296, mode='online'))
    model.ingest(view(298))
    model.ingest(view(300, soc=90))
    last = view(302, soc=89)
    model.ingest(last)
    commit(model, db)
    memory = copy.deepcopy(model.__dict__)
    rows = list(db.execute('SELECT * FROM battery_capacity_state'))
    changes = db.total_changes
    snapshot = result(model, last)
    assert snapshot['status'] == 'collecting' and snapshot['progress']['completed_pp'] == 1
    snapshot['baseline']['estimate_wh'] = 999
    snapshot['epoch']['basis']['device']['serial'] = 'client-edit'
    snapshot['criteria']['soc_start'] = 10
    newer = model.snapshot(view(304, soc=88), now=304)
    assert newer['progress'] is None and newer['reason'] == 'not_observed'
    stale = model.snapshot(last, now=313)
    assert not stale['capture_fresh'] and stale['reason'] == 'stale' and stale['progress'] is None
    assert stale['baseline'] is not None and stale['sample_timestamp'] == 302
    assert model.__dict__ == memory and db.total_changes == changes
    assert list(db.execute('SELECT * FROM battery_capacity_state')) == rows


def test_recent_candidates_are_bounded_without_pruning_fixed_baseline_or_archives(db):
    model = BatteryCapacity(db)
    window(model)
    baseline = copy.deepcopy(model.state['baseline'])
    for index in range(MAX_RECENT + 5):
        window(model, 300 + index * 200)
    assert len(model.state['recent']) == MAX_RECENT and model.state['baseline'] == baseline
    commit(model, db)
    resumed = BatteryCapacity(db)
    assert resumed.state['baseline'] == baseline and len(resumed.state['recent']) == MAX_RECENT
    resumed.reset(view(10000, mode='online'))
    commit(resumed, db)
    archive = json.loads(db.execute('SELECT payload FROM battery_capacity_epochs').fetchone()[0])
    assert archive['baseline'] == baseline and len(archive['recent']) == MAX_RECENT


@pytest.mark.parametrize('matching_count', [1, 2, 3])
def test_rejected_candidates_never_evict_latest_matches_or_reset_comparison_to_baseline(db, matching_count):
    model = BatteryCapacity(db)
    window(model)
    for index, duration in enumerate((8, 14, 16)[:matching_count]):
        last = window(model, 300 + index * 200, seconds_per_pp=duration)
    before = result(model, last)
    expected_matches = [item['id'] for item in before['recent'] if item['accepted']]
    for index in range(30):
        power = 12 if index % 2 == 0 else lambda elapsed: 5 if elapsed < 50 else 15
        last = window(model, 1000 + index * 200, power=power)
    after = result(model, last)
    assert len(after['recent']) == MAX_RECENT
    assert after['comparison'] == before['comparison']
    assert after['comparison']['index_pct'] != pytest.approx(100)
    assert after['status'] == ('trend' if matching_count == 3 else 'preliminary')
    assert [item['id'] for item in after['recent'] if item['accepted']] == expected_matches
    assert after['recent'][0]['reason'] == 'unstable_load'
    assert [item['end_ts'] for item in after['recent']] == sorted(
        (item['end_ts'] for item in after['recent']), reverse=True)
    commit(model, db)
    restored = BatteryCapacity(db)
    reloaded = result(restored, last)
    assert reloaded['comparison'] == before['comparison']
    assert reloaded['baseline'] == before['baseline']
    assert reloaded['recent'] == after['recent']
    # A later match replaces only the oldest member of the comparison trio.
    last = window(restored, 7200, seconds_per_pp=16)
    newer = result(restored, last)
    assert newer['comparison']['sample_count'] == min(3, matching_count + 1)
    assert newer['comparison']['latest_end_ts'] == 7360
    assert len(newer['recent']) == MAX_RECENT


def test_old_history_and_downgrade_are_isolated_and_no_legacy_data_is_backfilled(db):
    db.execute("INSERT INTO meta VALUES('battery_sessions_state','legacy-value')")
    db.execute('CREATE TABLE battery_sessions (id TEXT PRIMARY KEY, payload TEXT)')
    db.execute("INSERT INTO battery_sessions VALUES('old','old-wh-and-soc')")
    model = BatteryCapacity(db)
    assert model.state['epoch'] is None and model.state['baseline'] is None
    window(model)
    commit(model, db)
    original = db.execute('SELECT payload FROM battery_capacity_state').fetchone()[0]
    # Old versions can keep their own schemas and pruners; they have no FK to
    # this permanent reference and must not advance its absent integration.
    db.execute("UPDATE meta SET value='downgraded' WHERE key='battery_sessions_state'")
    db.execute('DELETE FROM battery_sessions')
    assert db.execute('SELECT payload FROM battery_capacity_state').fetchone()[0] == original
    restored = BatteryCapacity(db)
    assert restored.state['baseline'] == model.state['baseline']
    assert restored.blocked and restored.active is None
    assert db.execute('PRAGMA user_version').fetchone()[0] == 2
    assert db.execute("SELECT value FROM meta WHERE key='battery_sessions_state'").fetchone()[0] == 'downgraded'
