import copy
from datetime import datetime
import json
import sqlite3

import pytest

from ups_panel.calibration import default_config, normalize_config
import ups_panel.energy_usage as usage_module
from ups_panel.energy_usage import EnergyUsage, TZ


def timestamp(local='2026-09-09T12:00:00'):
    return datetime.fromisoformat(local).replace(tzinfo=TZ).timestamp()


def configuration(base=1, charge=None, battery=None):
    return normalize_config({'schema': 2, 'profile': 'custom', 'ac_voltage_nominal_v': 12,
                             'coefficients': {'base_gain': base, 'charge_gain': charge, 'battery_gain': battery}})


def view(ts, power=100, *, mode='online', config=None, source='synthetic-energy-usage'):
    config = default_config('local-19v-v1') if config is None else config
    profile, revision = config['profile'], config['revision']
    model = (f'us3000_custom_v2_{revision}' if config['schema'] == 2 else
             f'us3000_19v_custom_{revision}' if profile == 'custom' else
             'us3000_19v_v1' if profile != 'none' else None)
    sample = {'timestamp': ts, 'mode': mode, 'calibration_profile': profile,
              'calibration_revision': revision, 'calibration_coefficients': config['coefficients'],
              'ac_estimate_model': model, 'ac_estimate_quality': 'custom_unverified' if profile == 'custom' else 'extrapolated',
              'ac_input_estimate_w': power, 'decoder_version': 4, 'formula_version': 2,
              'battery_energy_estimate_w': 999, 'battery_discharge_power_candidate_w': 888}
    if config['schema'] == 2:
        sample.update(calibration_schema=2, ac_voltage_nominal_v=config['ac_voltage_nominal_v'])
    return {'fresh': True, 'server_time': ts, 'sample': sample, 'source': source,
            'device': {'serial': 'SYNTHETIC-AC-USAGE', 'vendor': '1234', 'product': '5678',
                       'bus': 1, 'device': 2, 'path': '1-1'}}


@pytest.fixture
def db():
    connection = sqlite3.connect(':memory:')
    connection.execute('CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT)')
    connection.execute('PRAGMA user_version=2')
    yield connection
    connection.close()


def commit(model, db, *, mirror_cursor=False):
    with db:
        model.write(db)
        if mirror_cursor:
            db.execute("INSERT OR REPLACE INTO meta VALUES('last_ts',?)", (str(model.state['last_ts']),))
    model.committed()


def feed(model, start, duration, *, power=100, step=5, mode='online', config=None):
    for offset in range(0, duration + 1, step):
        model.ingest(view(start + offset, power, mode=mode, config=config))


def summary(model, db, now, date='2026-09-09'):
    return model.day(db, date, now)['day']


def test_constant_power_real_hour_energy_and_same_day_month_ledger(db):
    model = EnergyUsage(db)
    start = timestamp()
    feed(model, start, 3600, power=100)
    day = model.day(db, '2026-09-09', start + 3600)
    month = model.month(db, '2026-09', start + 3600)
    assert day['day']['estimate_kwh'] == pytest.approx(.1)
    assert day['day']['covered_sec'] == 3600
    assert day['day']['average_power_w'] == pytest.approx(100)
    assert day['day']['status'] == 'today'
    assert day['hours'][12]['estimate_kwh'] == pytest.approx(.1)
    assert day['hours'][12]['coverage_ratio'] == 1
    assert day['hours'][13]['estimate_kwh'] is None
    assert sum(row['estimate_kwh'] or 0 for row in day['hours']) == pytest.approx(day['day']['estimate_kwh'])
    assert sum(row['estimate_kwh'] for row in day['bases']) == pytest.approx(.1)
    assert month['summary']['estimate_kwh'] == pytest.approx(.1)
    assert month['summary']['expected_sec'] == 8 * 86400 + 13 * 3600
    assert month['today'] == day['day'] and month['days'][8] == day['day']
    assert month['summary']['complete_day_average_kwh'] is None
    assert month['summary']['recorded_days'] == 1 and month['summary']['basis_count'] == 1
    assert month['tracking_started_at'] == start and month['first_month'] == '2026-09'
    assert month['last_recorded_at'] == start + 3600
    commit(model, db, mirror_cursor=True)
    restored = EnergyUsage(db)
    assert restored.month(db, None, start + 3600) == month


def test_midnight_ramp_is_split_by_interpolated_power_not_equal_duration_share(db):
    model = EnergyUsage(db)
    boundary = timestamp('2026-09-10T00:00:00')
    model.ingest(view(boundary - 2, 0))
    model.ingest(view(boundary + 2, 100))
    before = model.day(db, '2026-09-09', boundary + 2)
    after = model.day(db, '2026-09-10', boundary + 2)
    assert before['day']['estimate_kwh'] == pytest.approx(50 / 3600000)
    assert after['day']['estimate_kwh'] == pytest.approx(150 / 3600000)
    assert before['hours'][23]['covered_sec'] == after['hours'][0]['covered_sec'] == 2
    assert before['day']['status'] == 'partial' and after['day']['status'] == 'today'
    assert model.month(db, '2026-09', boundary + 2)['summary']['estimate_kwh'] == pytest.approx(200 / 3600000)


def test_hour_and_year_boundaries_preserve_each_side_of_a_power_ramp(db):
    model = EnergyUsage(db)
    boundary = timestamp('2027-01-01T00:00:00')
    model.ingest(view(boundary - 2, 10))
    model.ingest(view(boundary + 3, 110))
    old = model.month(db, '2026-12', boundary + 3)
    new = model.month(db, '2027-01', boundary + 3)
    assert old['summary']['estimate_kwh'] == pytest.approx(60 / 3600000)
    assert new['summary']['estimate_kwh'] == pytest.approx(240 / 3600000)
    assert old['today'] == new['today']
    assert old['days'][30]['date'] == '2026-12-31'
    assert len(new['days']) == 31


def test_irregular_trapezoids_use_existing_ac_estimate_without_rescaling(db):
    model = EnergyUsage(db)
    start = timestamp()
    config = configuration(base=9)
    for offset, power in ((0, 100), (2, 200), (5, 0), (10, 100)):
        model.ingest(view(start + offset, power, config=config))
    # 150*2 + 100*3 + 50*5 = 850 watt-seconds, despite base_gain=9.
    assert summary(model, db, start + 10)['estimate_kwh'] == pytest.approx(850 / 3600000)
    assert summary(model, db, start + 10)['covered_sec'] == 10


def test_five_second_boundary_and_missing_time_are_not_interpolated(db):
    model = EnergyUsage(db)
    start = timestamp()
    for offset in (0, 5, 10.001, 15.001):
        model.ingest(view(start + offset))
    value = summary(model, db, start + 15.001)
    assert value['estimate_kwh'] == pytest.approx(1000 / 3600000)
    assert value['covered_sec'] == pytest.approx(10)


def test_mode_changes_break_bridges_and_battery_counts_zero_with_coverage(db):
    model = EnergyUsage(db)
    start = timestamp()
    for offset, mode, power in ((0, 'online', 100), (2, 'online', 100), (4, 'charging', 300),
                                (6, 'charging', 300), (8, 'battery', 999), (10, 'battery', 999),
                                (12, 'online', 100), (14, 'online', 100)):
        model.ingest(view(start + offset, power, mode=mode))
    value = model.day(db, '2026-09-09', start + 14)
    assert value['day']['estimate_kwh'] == pytest.approx(1000 / 3600000)
    assert value['day']['covered_sec'] == 8 and value['day']['basis_count'] == 3
    assert len(value['bases']) == 3
    assert sorted(row['estimate_kwh'] for row in value['bases'])[0] == 0


def test_unknown_mode_and_invalid_readings_are_gaps_even_if_numeric_power_is_present(db):
    model = EnergyUsage(db)
    start = timestamp()
    model.ingest(view(start))
    model.ingest(view(start + 2, mode='unknown'))
    model.ingest(view(start + 4))
    model.ingest(view(start + 6))
    assert summary(model, db, start + 6)['covered_sec'] == 2
    assert summary(model, db, start + 6)['estimate_kwh'] == pytest.approx(200 / 3600000)


@pytest.mark.parametrize('bad', [None, True, '100', -1, float('nan'), float('inf'), 10 ** 400])
def test_invalid_power_cannot_enter_ledger_or_bridge_adjacent_samples(db, bad):
    model = EnergyUsage(db)
    start = timestamp()
    for offset, power in ((0, 100), (2, bad), (4, 100), (6, 100)):
        model.ingest(view(start + offset, power))
    assert summary(model, db, start + 6)['covered_sec'] == 2
    commit(model, db)
    assert 'NaN' not in db.execute('SELECT payload FROM energy_usage_state').fetchone()[0]


def test_large_finite_power_avoids_intermediate_overflow_and_underflow_is_not_zero_usage(db):
    model = EnergyUsage(db)
    start = timestamp()
    model.ingest(view(start, 1e308))
    model.ingest(view(start + 5, 1e308))
    value = summary(model, db, start + 5)
    assert value['estimate_kwh'] == pytest.approx(1e308 * (5 / 3600000))
    assert value['average_power_w'] == pytest.approx(1e308)
    commit(model, db)
    model.ingest(view(start + 7, 1e-323))
    model.ingest(view(start + 9, 1e-323))
    assert model.state['reason'] == 'invalid_energy' and model.state['anchor'] is None


def test_online_zero_is_valid_but_unconfigured_online_is_not_a_zero_reading(db):
    model = EnergyUsage(db)
    start = timestamp()
    feed(model, start, 10, power=0)
    value = summary(model, db, start + 10)
    assert value['estimate_kwh'] == 0 and value['covered_sec'] == 10
    assert value['average_power_w'] == 0
    model = EnergyUsage(db)
    feed(model, start, 10, power=100, config=default_config())
    value = summary(model, db, start + 10)
    assert value['estimate_kwh'] is None and value['covered_sec'] == 0
    assert model.state['reason'] == 'not_configured'


@pytest.mark.parametrize('config', [default_config(), configuration()])
def test_continuous_battery_mode_is_known_zero_without_a_battery_or_ac_gain(db, config):
    model = EnergyUsage(db)
    start = timestamp()
    feed(model, start, 10, power=None, mode='battery', config=config)
    value = summary(model, db, start + 10)
    assert value['estimate_kwh'] == 0 and value['covered_sec'] == 10
    assert value['status'] == 'today'


def test_partial_ac_configuration_supports_online_but_not_unconfigured_charging(db):
    model = EnergyUsage(db)
    start = timestamp()
    config = configuration()
    feed(model, start, 10, config=config)
    assert summary(model, db, start + 10)['covered_sec'] == 10
    model.ingest(view(start + 12, mode='charging', config=config))
    model.ingest(view(start + 14, mode='charging', config=config))
    assert model.state['reason'] == 'charge_not_configured'
    assert summary(model, db, start + 14)['covered_sec'] == 10


def test_model_quality_range_change_is_not_a_new_basis_or_a_gap(db):
    model = EnergyUsage(db)
    start = timestamp()
    first, second = view(start, 60), view(start + 5, 100)
    first['sample']['ac_estimate_quality'] = 'calibrated_range'
    model.ingest(first)
    model.ingest(second)
    value = summary(model, db, start + 5)
    assert value['covered_sec'] == 5 and value['basis_count'] == 1
    assert value['estimate_kwh'] == pytest.approx(400 / 3600000)


@pytest.mark.parametrize('mutation', ['revision', 'model', 'coefficient', 'missing_preset_coefficients',
                                    'warming_up', 'unsupported_voltage'])
def test_forged_or_unavailable_model_cannot_turn_a_positive_number_into_accounted_energy(db, mutation):
    model = EnergyUsage(db)
    start = timestamp()
    model.ingest(view(start))
    bad = view(start + 2)
    if mutation == 'revision':
        bad['sample']['calibration_revision'] = 'forged'
    elif mutation == 'model':
        bad['sample']['ac_estimate_model'] = 'different-formula'
    elif mutation == 'coefficient':
        bad['sample']['calibration_coefficients']['base_gain'] = 2
    elif mutation == 'missing_preset_coefficients':
        bad['sample']['calibration_coefficients'] = None
    else:
        bad['sample']['ac_estimate_quality'] = mutation
    model.ingest(bad)
    assert model.state['anchor'] is None
    assert summary(model, db, start + 2)['estimate_kwh'] is None


@pytest.mark.parametrize('field', ['source', 'device', 'decoder_version', 'formula_version',
                                  'calibration_revision', 'calibration_coefficients', 'ac_estimate_model'])
def test_missing_basis_is_not_borrowed_from_old_sample_even_for_battery_zero(db, field):
    model = EnergyUsage(db)
    start = timestamp()
    model.ingest(view(start, mode='battery'))
    bad = view(start + 2, mode='battery')
    del (bad if field in ('source', 'device') else bad['sample'])[field]
    model.ingest(bad)
    assert model.state['reason'] == 'invalid_basis' and model.state['anchor'] is None
    assert summary(model, db, start + 2)['estimate_kwh'] is None


@pytest.mark.parametrize('change', ['config', 'source', 'device', 'decoder', 'formula'])
def test_changed_basis_keeps_old_energy_and_separate_new_identity(db, change):
    model = EnergyUsage(db)
    start = timestamp()
    model.ingest(view(start))
    model.ingest(view(start + 2))
    changed = view(start + 4)
    if change == 'config':
        changed = view(start + 4, 200, config=configuration(base=2))
    elif change == 'source':
        changed['source'] = 'another-source'
    elif change == 'device':
        changed['device']['serial'] = 'another-ups'
    else:
        changed['sample'][change + '_version'] += 1
    model.ingest(changed)
    assert model.state['reason'] == 'context_changed'
    following = copy.deepcopy(changed)
    following['sample']['timestamp'] = following['server_time'] = start + 6
    model.ingest(following)
    value = model.day(db, '2026-09-09', start + 6)
    assert value['day']['covered_sec'] == 4 and value['day']['basis_count'] == 2
    assert value['day']['estimate_kwh'] == pytest.approx((600 if change == 'config' else 400) / 3600000)


def test_serial_identity_ignores_transient_usb_addresses(db):
    model = EnergyUsage(db)
    start = timestamp()
    model.ingest(view(start))
    reenumerated = view(start + 2)
    reenumerated['device'].update(bus=3, device=8, path='3-2')
    model.ingest(reenumerated)
    assert summary(model, db, start + 2)['covered_sec'] == 2


def test_duplicates_do_not_mutate_and_rollback_breaks_without_rewinding_watermark(db):
    model = EnergyUsage(db)
    start = timestamp()
    model.ingest(view(start))
    model.ingest(view(start + 2))
    before = copy.deepcopy(model.__dict__)
    model.ingest(view(start + 2, 999))
    assert model.__dict__ == before
    model.ingest(view(start + 1, 999))
    assert model.state['last_ts'] == start + 2 and model.state['anchor'] is None
    model.ingest(view(start + 4))
    model.ingest(view(start + 6))
    assert summary(model, db, start + 6)['covered_sec'] == 4
    assert summary(model, db, start + 6)['estimate_kwh'] == pytest.approx(400 / 3600000)


def test_restart_resumes_only_own_committed_and_matching_legacy_cursor(db):
    model = EnergyUsage(db)
    start = timestamp()
    feed(model, start, 10)
    commit(model, db, mirror_cursor=True)
    resumed = EnergyUsage(db)
    resumed.ingest(view(start + 15))
    assert summary(resumed, db, start + 15)['covered_sec'] == 15
    commit(resumed, db, mirror_cursor=True)
    db.execute("UPDATE meta SET value=? WHERE key='last_ts'", (str(start + 17),))
    db.commit()
    downgraded = EnergyUsage(db)
    assert downgraded.state['reason'] == 'continuity_lost' and downgraded.state['anchor'] is None
    downgraded.ingest(view(start + 19))
    downgraded.ingest(view(start + 21))
    assert summary(downgraded, db, start + 21)['covered_sec'] == 17


def test_no_energy_break_is_persistent_and_long_restart_does_not_fill_gap(db):
    model = EnergyUsage(db)
    start = timestamp()
    feed(model, start, 10)
    commit(model, db)
    model.ingest({'fresh': False, 'sample': None})
    commit(model, db)
    resumed = EnergyUsage(db)
    resumed.ingest(view(start + 12))
    resumed.ingest(view(start + 14))
    assert summary(resumed, db, start + 14)['covered_sec'] == 12
    commit(resumed, db)
    resumed = EnergyUsage(db)
    resumed.ingest(view(start + 30))
    assert resumed.state['reason'] == 'sample_gap'
    resumed.ingest(view(start + 32))
    assert summary(resumed, db, start + 32)['covered_sec'] == 14


def test_failed_flush_new_samples_and_single_retry_commit_latest_cursor_with_all_energy(db):
    model = EnergyUsage(db)
    start = timestamp()
    feed(model, start, 10)
    with pytest.raises(sqlite3.OperationalError):
        with db:
            model.write(db)
            raise sqlite3.OperationalError('synthetic failure after ledger write')
    assert db.execute('SELECT COUNT(*) FROM energy_usage_hours').fetchone()[0] == 0
    model.ingest(view(start + 15))
    assert summary(model, db, start + 15)['covered_sec'] == 15
    commit(model, db, mirror_cursor=True)
    assert not model.pending and not model.inflight and not model.dirty
    resumed = EnergyUsage(db)
    assert resumed.state['last_ts'] == start + 15
    assert resumed.state['anchor']['timestamp'] == start + 15
    assert resumed.state['reason'] is None
    assert summary(resumed, db, start + 15)['estimate_kwh'] == pytest.approx(1500 / 3600000)


def test_committed_write_without_acknowledgement_is_idempotent_with_new_samples(db):
    model = EnergyUsage(db)
    start = timestamp()
    feed(model, start, 10)
    with db:
        model.write(db)
    assert summary(model, db, start + 10)['covered_sec'] == 10
    model.ingest(view(start + 15))
    assert summary(model, db, start + 15)['covered_sec'] == 15
    with db:
        model.write(db)
        model.write(db)
    model.committed()
    assert not model.pending and not model.inflight
    assert summary(model, db, start + 15)['covered_sec'] == 15
    assert db.execute('SELECT covered_sec FROM energy_usage_hours').fetchone()[0] == 15


def test_two_unacknowledged_writes_rolled_back_together_keep_all_deltas_for_retry(db):
    model = EnergyUsage(db)
    start = timestamp()
    feed(model, start, 10)
    with pytest.raises(sqlite3.OperationalError):
        with db:
            model.write(db)
            model.ingest(view(start + 15))
            model.write(db)
            raise sqlite3.OperationalError('rollback two writes')
    assert summary(model, db, start + 15)['covered_sec'] == 15
    model.ingest(view(start + 20))
    commit(model, db)
    assert summary(model, db, start + 20)['covered_sec'] == 20
    assert db.execute('SELECT covered_sec FROM energy_usage_hours').fetchone()[0] == 20


def test_pending_is_added_to_existing_hour_not_used_to_replace_it(db):
    model = EnergyUsage(db)
    start = timestamp()
    feed(model, start, 10)
    commit(model, db)
    model.ingest(view(start + 15))
    report = model.month(db, '2026-09', start + 15)
    assert report['summary']['estimate_kwh'] == pytest.approx(1500 / 3600000)
    assert report['today']['covered_sec'] == 15
    assert db.execute('SELECT covered_sec FROM energy_usage_hours').fetchone()[0] == 10


def test_pending_bound_rejects_new_buckets_without_evicting_already_accumulated_energy(db, monkeypatch):
    monkeypatch.setattr(usage_module, 'MAX_PENDING_BUCKETS', 2)
    model = EnergyUsage(db)
    start = timestamp()
    for hour in range(3):
        model.ingest(view(start + hour * 3600))
        model.ingest(view(start + hour * 3600 + 2))
    assert len(model.pending) == 2 and model.state['dropped_intervals'] == 1
    assert model.state['reason'] == 'pending_limit'
    report = model.day(db, '2026-09-09', start + 7202)
    assert report['day']['covered_sec'] == 4 and report['dropped_intervals'] == 1
    assert report['hours'][14]['estimate_kwh'] is None
    commit(model, db)
    model.ingest(view(start + 7204))
    assert summary(model, db, start + 7204)['covered_sec'] == 6
    assert model.state['dropped_intervals'] == 1


def test_buffer_limit_rejects_a_cross_midnight_interval_atomically(db, monkeypatch):
    monkeypatch.setattr(usage_module, 'MAX_PENDING_BUCKETS', 1)
    model = EnergyUsage(db)
    boundary = timestamp('2026-09-10T00:00:00')
    for offset in (-4, -2, 2):
        model.ingest(view(boundary + offset))
    assert model.state['dropped_intervals'] == 1
    assert model.day(db, '2026-09-09', boundary + 2)['day']['covered_sec'] == 2
    assert model.day(db, '2026-09-10', boundary + 2)['day']['estimate_kwh'] is None
    commit(model, db)
    assert db.execute('SELECT covered_sec FROM energy_usage_hours').fetchone()[0] == 2


def test_complete_days_only_average_real_finished_days_with_five_second_tolerance(db):
    model = EnergyUsage(db)
    start = timestamp('2026-09-06T00:00:00')
    feed(model, start + 5, 86400 - 5, power=100)
    feed(model, start + 86400 + 10, 86400 - 10, power=200)
    feed(model, start + 2 * 86400, 3600, power=300)
    now = start + 2 * 86400 + 3600
    report = model.month(db, '2026-09', now)
    assert report['days'][5]['status'] == 'complete'
    assert report['days'][6]['status'] == 'partial'
    assert report['days'][7]['status'] == 'today'
    assert report['summary']['complete_days'] == 1 and report['summary']['recorded_days'] == 3
    assert report['summary']['complete_day_average_kwh'] == pytest.approx(100 * (86400 - 5) / 3600000)
    assert report['summary']['expected_sec'] == 7 * 86400 + 3600


def test_a_fully_observed_battery_day_is_zero_and_counts_in_complete_day_average(db):
    model = EnergyUsage(db)
    start = timestamp('2026-09-08T00:00:00')
    feed(model, start, 86400, power=None, mode='battery', config=default_config())
    report = model.month(db, '2026-09', start + 86400)
    assert report['days'][7]['status'] == 'complete'
    assert report['days'][7]['estimate_kwh'] == 0 and report['days'][7]['coverage_ratio'] == 1
    assert report['summary']['complete_days'] == 1
    assert report['summary']['complete_day_average_kwh'] == 0
    assert report['today']['estimate_kwh'] is None


def test_future_month_empty_days_and_beijing_zero_boundary_never_fabricate_zero(db):
    model = EnergyUsage(db)
    now = timestamp('2026-09-09T00:00:00')
    today = model.month(db, None, now)
    assert today['month'] == '2026-09' and today['current_date'] == '2026-09-09'
    assert today['today']['status'] == 'no_data' and today['today']['expected_sec'] == 0
    assert today['today']['estimate_kwh'] is None and today['today']['coverage_ratio'] is None
    assert today['tracking_started_at'] is None and today['first_month'] is None
    future = model.month(db, '2028-02', now)
    assert len(future['days']) == 29 and all(day['status'] == 'future' for day in future['days'])
    assert future['summary']['expected_sec'] == 0 and future['summary']['estimate_kwh'] is None
    assert future['today'] == today['today']
    day = model.day(db, '2026-09-10', now)
    assert day['day']['status'] == 'future' and len(day['hours']) == 24
    assert [hour['hour'] for hour in day['hours']] == list(range(24))
    assert all(hour['estimate_kwh'] is None and hour['expected_sec'] == 0 for hour in day['hours'])


def test_future_or_stale_samples_cannot_move_watermark_or_become_coverage(db):
    model = EnergyUsage(db)
    start = timestamp()
    model.ingest(view(start))
    future = view(start + 2)
    future['server_time'] = start + 1
    model.ingest(future)
    assert model.state['reason'] == 'future_sample' and model.state['last_ts'] == start
    stale = view(start + 2)
    stale['server_time'] = start + 12.001
    model.ingest(stale)
    assert model.state['reason'] == 'stale' and model.state['last_ts'] == start
    model.ingest(view(start + 4))
    model.ingest(view(start + 6))
    assert summary(model, db, start + 6)['covered_sec'] == 2


def test_query_cutoff_inside_aggregated_span_is_excluded_not_prorated(db):
    model = EnergyUsage(db)
    start = timestamp()
    feed(model, start, 10)
    commit(model, db)
    day = model.day(db, '2026-09-09', start + 5)
    month = model.month(db, '2026-09', start + 5)
    assert day['reason'] == month['reason'] == 'query_cutoff_overlap'
    assert day['day']['estimate_kwh'] is None and day['day']['covered_sec'] == 0
    assert day['bases'] == [] and month['summary']['estimate_kwh'] is None
    assert day['hours'][12]['expected_sec'] == 5
    assert summary(model, db, start + 10)['covered_sec'] == 10


@pytest.mark.parametrize('invalid', ['2026-9', '2026-00', '2026-13', '1969-12', '9999-01', '', [], True])
def test_month_rejects_noncanonical_or_unsupported_dates(db, invalid):
    with pytest.raises(ValueError, match='invalid_month'):
        EnergyUsage(db).month(db, invalid, timestamp())


@pytest.mark.parametrize('invalid', ['2026-09-9', '2026-02-29', '2026-09-31', '1969-12-31',
                                    '9999-01-01', '2026-09-09T00:00:00', None, []])
def test_day_rejects_noncanonical_or_unsupported_dates(db, invalid):
    with pytest.raises(ValueError, match='invalid_date'):
        EnergyUsage(db).day(db, invalid, timestamp())


def test_queries_are_detached_read_only_and_do_not_integrate_to_now(db):
    model = EnergyUsage(db)
    start = timestamp()
    feed(model, start, 10)
    commit(model, db)
    model.ingest(view(start + 15))
    memory = copy.deepcopy(model.__dict__)
    changes = db.total_changes
    report = model.month(db, None, start + 60)
    assert report['today']['covered_sec'] == 15
    report['today']['covered_sec'] = 999
    detail = model.day(db, '2026-09-09', start + 60)
    detail['bases'][0]['source'] = 'client-mutated'
    assert db.total_changes == changes and model.__dict__ == memory


def test_additive_tables_do_not_backfill_or_change_legacy_schema_and_permanent_hours_survive(db):
    db.execute('CREATE TABLE samples (fake TEXT)')
    db.execute("INSERT INTO samples VALUES('legacy averages are not interval energy')")
    model = EnergyUsage(db)
    start = timestamp()
    assert model.month(db, None, start)['summary']['estimate_kwh'] is None
    feed(model, start, 10)
    commit(model, db)
    db.execute('DELETE FROM samples')
    db.commit()
    restored = EnergyUsage(db)
    assert restored.month(db, '2026-09', timestamp('2030-01-01T00:00:00'))['summary']['estimate_kwh'] == pytest.approx(1000 / 3600000)
    assert db.execute('PRAGMA user_version').fetchone()[0] == 2
    assert [row[1] for row in db.execute('PRAGMA table_info(samples)')] == ['fake']


def test_two_instances_cannot_overwrite_each_others_committed_ledger(db):
    first, second = EnergyUsage(db), EnergyUsage(db)
    start = timestamp()
    feed(first, start, 10)
    feed(second, start, 10)
    commit(first, db)
    with pytest.raises(ValueError, match='energy_ledger_changed'):
        with db:
            second.write(db)
    assert db.execute('SELECT covered_sec FROM energy_usage_hours').fetchone()[0] == 10
