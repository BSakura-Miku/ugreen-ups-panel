import copy
import json
import sqlite3

import pytest

from ups_panel.battery_sessions import BatterySessions, FIELDS, STATE_KEY
from ups_panel.calibration import default_config, normalize_config
from ups_panel.storage import Store


def configuration(gain=2, *, base=1):
    return normalize_config({'schema': 2, 'profile': 'custom', 'ac_voltage_nominal_v': 12,
                             'coefficients': {'base_gain': base, 'charge_gain': None, 'battery_gain': gain}})


def view(ts, power=10, *, mode='battery', soc=90, config=None, source='usbmon', device=None):
    config = configuration() if config is None else config
    profile = config['profile']
    sample = {'timestamp': ts, 'mode': mode, 'soc': soc, 'cells': [4, 4, 4, 4],
              'battery_discharge_power_candidate_w': power,
              # This deliberately disagrees with the raw channel. Energy must
              # not integrate the smoothed display series or its warm-up nulls.
              'battery_energy_estimate_w': 999,
              'calibration_profile': profile, 'calibration_revision': config['revision'],
              'calibration_coefficients': config['coefficients'],
              'battery_estimate_basis': ('us3000_battery_custom_' + config['revision'] if profile == 'custom'
                                         else 'nominal_43_2wh_soc_v1' if profile != 'none' else None),
              'decoder_version': 4, 'formula_version': 2}
    if config['schema'] == 2:
        sample.update(calibration_schema=2, ac_voltage_nominal_v=config['ac_voltage_nominal_v'])
    return {'fresh': True, 'source': source,
            'device': {'serial': 'SYNTHETIC-ENERGY', 'bus': 1, 'device': 2, 'path': '1-1'} if device is None else device,
            'sample': sample}


@pytest.fixture
def db():
    connection = sqlite3.connect(':memory:')
    connection.execute('CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT)')
    yield connection
    connection.close()


def commit(sessions, db):
    with db:
        sessions.write(db)
    sessions.committed()


def records(sessions, db, now=120):
    return sessions.history(db, 365, 500, now, True)['records']


def energy(sessions, db, now=120):
    return records(sessions, db, now)[0]['energy']


def test_trapezoids_use_unsmoothed_power_and_actual_irregular_sample_times(db):
    sessions = BatterySessions(db)
    for item in (view(98, mode='online'), view(100, 10, soc=90), view(102, 20, soc=89),
                 view(105, 40, soc=88), view(106, mode='charging', soc=92)):
        sessions.ingest(item)
    record = records(sessions, db)[0]
    result = record['energy']
    # 20 -> 40 W over 2 s, then 40 -> 80 W over 3 s: 240 W.s.
    assert result['estimate_wh'] == pytest.approx(240 / 3600)
    assert result['interval_count'] == 2
    assert result['covered_duration_sec'] == result['observed_duration_sec'] == 5
    assert result['coverage_ratio'] == 1 and result['status'] == 'available'
    assert result['reasons'] == []
    assert result['start_soc'] == 90 and result['end_soc'] == 88
    assert record['end_soc'] == 92 and record['observed_duration_sec'] == 6
    assert record['status'] == 'complete'
    assert result['basis'] == {'profile': 'custom', 'revision': configuration()['revision'],
                               'battery_gain': 2, 'estimate_basis': 'us3000_battery_custom_' + configuration()['revision'],
                               'source': 'usbmon', 'device': view(100)['device'],
                               'decoder_version': 4, 'formula_version': 2}
    commit(sessions, db)
    assert energy(BatterySessions(db), db) == result


@pytest.mark.parametrize('ending', ['online', 'unknown', 'offline'])
def test_observed_energy_completeness_does_not_claim_a_complete_discharge(db, ending):
    sessions = BatterySessions(db)
    sessions.ingest(view(100, soc=100))  # An unknown beginning still allows observed energy.
    sessions.ingest(view(102, soc=99))
    sessions.ingest({'fresh': False, 'sample': None} if ending == 'offline' else view(104, mode=ending))
    record = records(sessions, db)[0]
    assert record['status'] == 'incomplete'
    assert record['energy']['status'] == 'available'
    assert record['energy']['observed_duration_sec'] == 2
    assert record['energy']['end_soc'] == 99


@pytest.mark.parametrize('bad', [None, True, '20', 0, -1, float('nan'), float('inf'), 10 ** 400, 1e308])
def test_invalid_power_or_overflow_breaks_both_adjacent_intervals(db, bad):
    sessions = BatterySessions(db)
    for item in (view(100), view(102, bad), view(104), view(106)):
        sessions.ingest(item)
    result = energy(sessions, db)
    assert result['estimate_wh'] == pytest.approx(40 / 3600)
    assert result['covered_duration_sec'] == 2 and result['observed_duration_sec'] == 6
    assert result['coverage_ratio'] == pytest.approx(1 / 3)
    assert result['interval_count'] == 1 and result['status'] == 'partial'
    assert 'invalid_power' in result['reasons']
    commit(sessions, db)
    assert 'NaN' not in db.execute('SELECT payload FROM battery_session_energy').fetchone()[0]


@pytest.mark.parametrize('gap,covered,count,status', [(5, 7, 2, 'available'), (5.000001, 2, 1, 'partial'),
                                                       (10, 2, 1, 'partial')])
def test_five_second_energy_boundary_is_distinct_from_session_gap(db, gap, covered, count, status):
    sessions = BatterySessions(db)
    for ts in (100, 100 + gap, 102 + gap):
        sessions.ingest(view(ts))
    result = energy(sessions, db)
    assert len(records(sessions, db)) == 1
    assert result['covered_duration_sec'] == pytest.approx(covered)
    assert result['interval_count'] == count and result['status'] == status
    assert ('sample_gap' in result['reasons']) == (gap > 5)
    assert result['estimate_wh'] == pytest.approx(20 * covered / 3600)


def test_session_gap_starts_a_new_unknown_fragment_without_bridging_energy(db):
    sessions = BatterySessions(db)
    for ts in (100, 102, 113, 115):
        sessions.ingest(view(ts))
    newer, older = records(sessions, db)
    assert older['end_reason'] == 'gap'
    assert newer['start_known'] is False
    assert all(item['energy']['estimate_wh'] == pytest.approx(40 / 3600) for item in (newer, older))


def test_duplicates_do_not_integrate_or_change_identity_but_rollback_breaks_anchor(db):
    sessions = BatterySessions(db)
    for item in (view(100), view(102), view(102, 900, source='changed'), view(101, 900),
                 view(104), view(106), view(106, mode='charging')):
        sessions.ingest(item)
    result = energy(sessions, db)
    assert len(records(sessions, db)) == 1 and sessions.active['sample_count'] == 4
    assert result['interval_count'] == 2 and result['covered_duration_sec'] == 4
    assert result['estimate_wh'] == pytest.approx(80 / 3600)
    assert result['status'] == 'partial' and 'timestamp_rollback' in result['reasons']


@pytest.mark.parametrize('change', ['source', 'device', 'gain', 'base', 'decoder', 'formula', 'none'])
def test_changed_identity_or_configuration_separates_sessions_and_energy_basis(db, change):
    sessions = BatterySessions(db)
    for item in (view(98, mode='online'), view(100), view(102)):
        sessions.ingest(item)
    changed = view(104)
    if change == 'source':
        changed['source'] = 'replay'
    elif change == 'device':
        changed['device']['serial'] = 'SYNTHETIC-OTHER'
    elif change in ('gain', 'base', 'none'):
        changed = view(104, config=default_config() if change == 'none' else
                       configuration(3) if change == 'gain' else configuration(base=1.5))
    else:
        changed['sample'][change + '_version'] += 1
    sessions.ingest(changed)
    newer_sample = copy.deepcopy(changed)
    newer_sample['sample']['timestamp'] = 106
    sessions.ingest(newer_sample)
    newer, older = records(sessions, db)
    assert older['end_reason'] == 'context_changed' and older['status'] == 'incomplete'
    assert 'context_changed' in older['energy']['reasons']
    assert older['energy']['estimate_wh'] == pytest.approx(40 / 3600)
    assert newer['start_known'] is False and newer['energy']['observed_duration_sec'] == 2
    assert newer['energy']['estimate_wh'] == (None if change == 'none' else pytest.approx((30 if change == 'gain' else 20) * 2 / 3600))


def test_identity_change_at_external_sample_is_not_a_confirmed_return_of_the_same_device(db):
    sessions = BatterySessions(db)
    sessions.ingest(view(98, mode='online'))
    sessions.ingest(view(100))
    sessions.ingest(view(102))
    sessions.ingest(view(104, mode='online', source='other'))
    record = records(sessions, db)[0]
    assert record['end_reason'] == 'context_changed' and record['status'] == 'incomplete'
    assert record['energy']['observed_duration_sec'] == 2


@pytest.mark.parametrize('field', ['calibration_coefficients', 'calibration_revision', 'battery_estimate_basis',
                                  'decoder_version', 'formula_version', 'source', 'device'])
def test_missing_evidence_breaks_anchor_and_never_reuses_previous_sample_basis(db, field):
    sessions = BatterySessions(db)
    bad = view(102)
    del (bad if field in ('source', 'device') else bad['sample'])[field]
    for item in (view(100), bad, view(104), view(106)):
        sessions.ingest(item)
    result = energy(sessions, db)
    assert len(records(sessions, db)) == 1
    assert result['interval_count'] == 1 and result['covered_duration_sec'] == 2
    assert result['estimate_wh'] == pytest.approx(40 / 3600)
    assert result['status'] == 'partial' and 'invalid_basis' in result['reasons']


@pytest.mark.parametrize('mutation', ['revision', 'coefficient', 'basis', 'version_bool', 'voltage', 'device_bool'])
def test_invalid_metadata_never_turns_a_positive_raw_channel_into_calibrated_energy(db, mutation):
    sessions = BatterySessions(db)
    first = view(100)
    if mutation == 'revision':
        first['sample']['calibration_revision'] = 'forged'
    elif mutation == 'coefficient':
        first['sample']['calibration_coefficients'] = dict(first['sample']['calibration_coefficients'], battery_gain=3)
    elif mutation == 'basis':
        first['sample']['battery_estimate_basis'] = 'nominal_43_2wh_soc_v1'
    elif mutation == 'version_bool':
        first['sample']['decoder_version'] = True
    elif mutation == 'voltage':
        first['sample']['ac_voltage_nominal_v'] = 19
    else:
        first['device']['bus'] = True
    second = copy.deepcopy(first)
    second['sample']['timestamp'] = 102
    sessions.ingest(first)
    sessions.ingest(second)
    result = energy(sessions, db)
    assert result['estimate_wh'] is None and result['basis'] is None
    assert result['coverage_ratio'] == 0 and result['status'] == 'unavailable'
    assert set(result['reasons']) == {'invalid_basis', 'insufficient_samples'}


@pytest.mark.parametrize('config', [default_config(), configuration(None)])
def test_unconfigured_battery_model_does_not_borrow_default_gain(db, config):
    sessions = BatterySessions(db)
    sessions.ingest(view(100, config=config))
    sessions.ingest(view(102, config=config))
    result = energy(sessions, db)
    assert result['estimate_wh'] is None and result['basis'] is None
    assert set(result['reasons']) == {'not_configured', 'insufficient_samples'}
    assert result['observed_duration_sec'] == 2 and result['covered_duration_sec'] == 0


def test_local_profile_requires_explicit_full_coefficients_and_revision(db):
    config = default_config('local-19v-v1')
    sessions = BatterySessions(db)
    for ts in (100, 102):
        sessions.ingest(view(ts, config=config))
    result = energy(sessions, db)
    assert result['estimate_wh'] == pytest.approx(10 * config['coefficients']['battery_gain'] * 2 / 3600)
    assert result['basis']['estimate_basis'] == 'nominal_43_2wh_soc_v1'
    no_coefficients = view(104, config=config)
    no_coefficients['sample']['calibration_coefficients'] = None
    sessions.ingest(no_coefficients)
    assert energy(sessions, db)['status'] == 'partial'


def test_single_sample_has_no_observed_interval_and_no_zero_wh_claim(db):
    sessions = BatterySessions(db)
    sessions.ingest(view(100, soc=None))
    result = energy(sessions, db)
    assert result['estimate_wh'] is None and result['coverage_ratio'] is None
    assert result['observed_duration_sec'] == result['covered_duration_sec'] == 0
    assert result['start_soc'] is None and result['end_soc'] is None
    assert result['status'] == 'unavailable' and result['reasons'] == ['insufficient_samples']


def test_collector_identity_without_serial_is_usable_but_missing_device_is_not(db):
    sessions = BatterySessions(db)
    for ts in (100, 102):
        sessions.ingest(view(ts, device={'serial': '', 'path': '1-1', 'bus': 1, 'device': 2}))
    assert energy(sessions, db)['status'] == 'available'
    missing = view(104)
    missing['device'] = None
    sessions.ingest(missing)
    assert 'invalid_basis' in energy(sessions, db)['reasons']


def test_short_restart_resumes_only_committed_matching_anchor(db):
    sessions = BatterySessions(db)
    for ts in (100, 102):
        sessions.ingest(view(ts))
    identity = sessions.active['id']
    commit(sessions, db)
    resumed = BatterySessions(db)
    resumed.ingest(view(102, 500))
    resumed.ingest(view(104, 20))
    commit(resumed, db)
    result = records(BatterySessions(db), db)
    assert len(result) == 1 and result[0]['id'] == identity
    assert result[0]['energy']['interval_count'] == 2
    assert result[0]['energy']['estimate_wh'] == pytest.approx((40 + 60) / 3600)
    assert result[0]['energy']['coverage_ratio'] == 1


def test_store_transaction_failure_rolls_back_energy_with_session_and_retries_once(tmp_path, monkeypatch):
    store = Store(tmp_path / 'history.db')
    for ts in (100, 102):
        store.ingest(view(ts), now=ts)
    original = store.battery_sessions.write

    def fail(db):
        original(db)
        raise sqlite3.OperationalError('energy transaction failure')

    monkeypatch.setattr(store.battery_sessions, 'write', fail)
    with pytest.raises(sqlite3.OperationalError, match='energy transaction failure'):
        store.flush()
    with store.connect() as connection:
        for table in ('samples', 'battery_sessions', 'battery_session_energy'):
            assert connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0
        assert connection.execute('SELECT value FROM meta WHERE key=?', (STATE_KEY,)).fetchone() is None
    assert store.battery_history(1, now=103)['records'][0]['energy']['estimate_wh'] == pytest.approx(40 / 3600)
    monkeypatch.setattr(store.battery_sessions, 'write', original)
    store.ingest(view(104), now=104)
    store.flush()
    store.flush()
    restored = Store(store.path)
    restored.ingest(view(106), now=106)
    restored.flush()
    result = restored.battery_history(1, now=107)['records'][0]['energy']
    assert result['interval_count'] == 3 and result['estimate_wh'] == pytest.approx(120 / 3600)


def advance_with_legacy_panel(db, ts, *, close=False):
    state = json.loads(db.execute('SELECT value FROM meta WHERE key=?', (STATE_KEY,)).fetchone()[0])
    identity = state['active_id']
    db.execute('UPDATE battery_sessions SET last_ts=?,sample_count=sample_count+1,end_soc=85 WHERE id=?', (ts, identity))
    state.update(last_ts=ts, last_mode='battery')
    if close:
        db.execute("UPDATE battery_sessions SET end_ts=?,end_soc=87,end_reason='external' WHERE id=?", (ts + 2, identity))
        state.update(last_ts=ts + 2, last_mode='charging', active_id=None)
    db.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps(state), STATE_KEY))


def test_old_panel_advancing_session_cursor_prevents_restart_bridge(db):
    sessions = BatterySessions(db)
    sessions.ingest(view(100))
    sessions.ingest(view(102))
    commit(sessions, db)
    advance_with_legacy_panel(db, 104)
    resumed = BatterySessions(db)
    before = db.total_changes
    stale_energy = energy(resumed, db)
    assert db.total_changes == before
    assert stale_energy['observed_duration_sec'] == 4 and stale_energy['covered_duration_sec'] == 2
    assert stale_energy['status'] == 'partial' and 'continuity_lost' in stale_energy['reasons']
    resumed.ingest(view(106))
    resumed.ingest(view(108))
    newer, older = records(resumed, db)
    assert older['end_reason'] == 'context_changed'
    assert older['energy']['estimate_wh'] == pytest.approx(40 / 3600)
    assert older['energy']['status'] == 'partial'
    assert newer['start_known'] is False and newer['energy']['estimate_wh'] == pytest.approx(40 / 3600)


def test_old_panel_closing_session_keeps_unrecorded_tail_partial_and_soc_unknown(db):
    sessions = BatterySessions(db)
    sessions.ingest(view(100))
    sessions.ingest(view(102))
    commit(sessions, db)
    advance_with_legacy_panel(db, 104, close=True)
    resumed = BatterySessions(db)
    before = db.total_changes
    result = energy(resumed, db)
    assert db.total_changes == before
    assert result['observed_duration_sec'] == 4 and result['covered_duration_sec'] == 2
    assert result['status'] == 'partial' and result['end_soc'] is None
    assert result['estimate_wh'] == pytest.approx(40 / 3600)


def test_legacy_database_keeps_nine_columns_state_keys_and_user_version_and_does_not_backfill(tmp_path):
    store = Store(tmp_path / 'history.db')
    store.ingest(view(100), now=100)
    store.ingest(view(102), now=102)
    store.flush()
    with store.connect() as connection:
        connection.execute('DROP TABLE battery_session_energy')
    restored = Store(store.path)
    assert restored.battery_history(1, now=103)['records'][0]['energy'] is None
    restored.ingest(view(104), now=104)
    restored.ingest(view(106), now=106)
    restored.flush()
    newer, older = restored.battery_history(1, now=107)['records']
    assert older['energy'] is None and newer['start_known'] is False
    assert newer['energy']['estimate_wh'] == pytest.approx(40 / 3600)
    with restored.connect() as connection:
        assert tuple(row[1] for row in connection.execute('PRAGMA table_info(battery_sessions)')) == FIELDS
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 2
        state = json.loads(connection.execute('SELECT value FROM meta WHERE key=?', (STATE_KEY,)).fetchone()[0])
        assert set(state) == {'last_ts', 'last_mode', 'active_id', 'recording_since'}
        # An older panel can still execute its positional nine-value write.
        row = connection.execute('SELECT * FROM battery_sessions LIMIT 1').fetchone()
        connection.execute('INSERT OR REPLACE INTO battery_sessions VALUES(?,?,?,?,?,?,?,?,?)', row)


def test_energy_pruning_follows_legacy_retention_and_removes_old_version_orphans(db):
    sessions = BatterySessions(db)
    for item in (view(100), view(102), view(104, mode='online'), view(200), view(202)):
        sessions.ingest(item)
    commit(sessions, db)
    db.execute("INSERT INTO battery_session_energy VALUES('orphan','{}')")
    sessions.prune(db, 365 * 86400 + 105)
    assert [row[0] for row in db.execute('SELECT session_id FROM battery_session_energy')] == [sessions.active['id']]
    assert db.execute('SELECT COUNT(*) FROM battery_sessions').fetchone()[0] == 1


def test_reads_leave_pending_anchor_and_persisted_rows_unchanged(db):
    sessions = BatterySessions(db)
    sessions.ingest(view(100))
    sessions.ingest(view(102))
    commit(sessions, db)
    before = list(db.execute('SELECT * FROM battery_session_energy'))
    changes = db.total_changes
    result = records(sessions, db)[0]
    result['energy']['basis']['device']['serial'] = 'client-mutated'
    sessions.history(db, 1, 50, 500, False)
    assert db.total_changes == changes and list(db.execute('SELECT * FROM battery_session_energy')) == before
    sessions.ingest(view(104))
    assert energy(sessions, db)['interval_count'] == 2


def test_underflowing_integral_is_rejected_and_not_bridged_by_the_next_good_point(db):
    sessions = BatterySessions(db)
    config = configuration(1)
    for item in (view(100, 1e-323, config=config), view(101, 1e-323, config=config),
                 view(102, 10, config=config), view(104, 10, config=config)):
        sessions.ingest(item)
    result = energy(sessions, db)
    assert result['estimate_wh'] == pytest.approx(20 / 3600)
    assert result['interval_count'] == 1 and result['covered_duration_sec'] == 2
    assert result['observed_duration_sec'] == 4 and result['status'] == 'partial'
    assert 'invalid_energy' in result['reasons']


def test_finite_large_powers_do_not_overflow_intermediate_trapezoid_sum(db):
    sessions = BatterySessions(db)
    config = configuration(1)
    sessions.ingest(view(100, 1e308, config=config))
    sessions.ingest(view(105, 1e308, config=config))
    assert energy(sessions, db)['estimate_wh'] == pytest.approx(1e308 * (5 / 3600))
    commit(sessions, db)


def test_cumulative_energy_overflow_never_reaches_json_or_discards_previous_valid_intervals(db):
    sessions = BatterySessions(db)
    config = configuration(1)
    for index in range(1400):
        sessions.ingest(view(100 + index * 5, 1e308, config=config))
    result = energy(sessions, db, now=7100)
    assert result['status'] == 'partial' and 'invalid_energy' in result['reasons']
    assert 0 < result['interval_count'] < 1399 and 0 < result['estimate_wh'] < float('inf')
    commit(sessions, db)
    assert 'Infinity' not in db.execute('SELECT payload FROM battery_session_energy').fetchone()[0]


@pytest.mark.parametrize('mismatch', ['legacy_meta', 'legacy_count', 'legacy_clock', 'energy_anchor'])
def test_restart_requires_all_committed_cursors_to_match_not_just_a_recent_timestamp(db, mismatch):
    sessions = BatterySessions(db)
    sessions.ingest(view(100))
    sessions.ingest(view(102))
    commit(sessions, db)
    if mismatch == 'legacy_meta':
        state = json.loads(db.execute('SELECT value FROM meta WHERE key=?', (STATE_KEY,)).fetchone()[0])
        state['last_ts'] = 103
        db.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps(state), STATE_KEY))
    elif mismatch == 'legacy_count':
        db.execute('UPDATE battery_sessions SET sample_count=sample_count+1')
    elif mismatch == 'legacy_clock':
        advance_with_legacy_panel(db, 101)
    else:
        payload = json.loads(db.execute('SELECT payload FROM battery_session_energy').fetchone()[0])
        payload['anchor']['timestamp'] = 101
        db.execute('UPDATE battery_session_energy SET payload=?', (json.dumps(payload),))
    resumed = BatterySessions(db)
    resumed.ingest(view(104))
    resumed.ingest(view(106))
    newer, older = records(resumed, db)
    assert older['end_reason'] == 'context_changed' and 'continuity_lost' in older['energy']['reasons']
    assert newer['start_known'] is False
    assert older['energy']['estimate_wh'] == newer['energy']['estimate_wh'] == pytest.approx(40 / 3600)
