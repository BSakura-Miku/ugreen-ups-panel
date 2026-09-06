import copy
import json
import os
from pathlib import Path
import stat

import pytest

from ups_panel import calibration, collector
from ups_panel.calibration import (CalibrationError, DEFAULT_COEFFICIENTS, default_config,
                                   load_config, normalize_config, save_config)
from ups_panel.protocol import parse_frame


ROOT = Path(__file__).parents[1]


def custom(base=1.5, charge=.75, battery=1.25):
    return {'schema': 1, 'profile': 'custom', 'coefficients': {
        'base_gain': base, 'charge_gain': charge, 'battery_gain': battery}}


def sample(timestamp):
    frame = bytes.fromhex((ROOT / 'fixtures' / 'online.hex').read_text().splitlines()[0])
    return parse_frame(frame, timestamp)


def test_revision_is_content_derived_and_numeric_spellings_are_canonical():
    first = custom(base=1, charge=0, battery=2)
    supplied = copy.deepcopy(first)
    supplied['revision'] = 'client-controlled'
    normalized = normalize_config(supplied)
    reordered = {'coefficients': {'battery_gain': 2.0, 'charge_gain': -0.0, 'base_gain': 1.0},
                 'profile': 'custom', 'schema': 1, 'revision': 'another-value'}
    assert normalize_config(reordered) == normalized
    assert normalize_config(normalized) == normalized
    assert len(normalized['revision']) == 64
    assert normalized['revision'] != supplied['revision']
    assert supplied['revision'] == 'client-controlled'
    assert normalize_config(custom(base=1.01))['revision'] != normalized['revision']


def test_presets_supply_exact_defaults_and_do_not_accept_other_coefficients():
    assert default_config()['coefficients'] is None
    local = default_config('local-19v-v1')
    assert local['coefficients'] == DEFAULT_COEFFICIENTS
    assert normalize_config({'schema': 1, 'profile': 'local-19v-v1', 'coefficients': None}) == local
    assert normalize_config(local) == local
    for config in ({'schema': 1, 'profile': 'none', 'coefficients': DEFAULT_COEFFICIENTS},
                   {'schema': 1, 'profile': 'local-19v-v1', 'coefficients': custom()['coefficients']}):
        with pytest.raises(CalibrationError):
            normalize_config(config)
    with pytest.raises(CalibrationError):
        default_config('custom')


@pytest.mark.parametrize('value', [True, False, None, '1.1', [], {}, float('nan'),
                                 float('inf'), -float('inf'), 10 ** 400, -1, 10.0001])
def test_custom_rejects_invalid_numbers(value):
    for name in DEFAULT_COEFFICIENTS:
        config = custom()
        config['coefficients'][name] = value
        with pytest.raises(CalibrationError):
            normalize_config(config)


@pytest.mark.parametrize('config', [None, [], {}, {'schema': True, 'profile': 'none'},
    {'schema': 2, 'profile': 'none'}, {'schema': 1, 'profile': 'custom'},
    {'schema': 1, 'profile': 'other'}, {'schema': 1, 'profile': 'none', 'unexpected': 1},
    {'schema': 1, 'profile': 'custom', 'coefficients': {'base_gain': 1}},
    {'schema': 1, 'profile': 'custom', 'coefficients': {**DEFAULT_COEFFICIENTS, 'extra': 1}}])
def test_schema_and_keys_are_strict(config):
    with pytest.raises(CalibrationError):
        normalize_config(config)


def test_zero_charge_and_upper_bounds_are_allowed_but_other_zeroes_are_not():
    assert normalize_config(custom(10, 0, 10))['coefficients']['charge_gain'] == 0
    for name in ('base_gain', 'battery_gain'):
        config = custom()
        config['coefficients'][name] = 0
        with pytest.raises(CalibrationError):
            normalize_config(config)


def test_atomic_roundtrip_permissions_size_limit_and_missing_file(tmp_path):
    path = tmp_path / 'calibration.json'
    assert load_config(path) is None
    stored = save_config(path, custom())
    assert load_config(path) == stored == normalize_config(custom())
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    encoded = path.read_bytes()
    path.write_bytes(encoded + b' ' * (4096 - len(encoded)))
    assert load_config(path) == stored
    with path.open('ab') as source:
        source.write(b' ')
    with pytest.raises(CalibrationError, match='4 KiB'):
        load_config(path)


@pytest.mark.parametrize('encoded', [b'{', b'\xff',
    b'{"schema":1,"profile":"none","profile":"local-19v-v1"}',
    b'{"schema":1,"profile":"custom","coefficients":{"base_gain":NaN}}'])
def test_invalid_json_is_rejected(tmp_path, encoded):
    path = tmp_path / 'calibration.json'
    path.write_bytes(encoded)
    with pytest.raises(CalibrationError):
        load_config(path)


def test_symlinks_directories_and_fifo_are_rejected_without_touching_target(tmp_path):
    target = tmp_path / 'target.json'
    save_config(target, default_config())
    original = target.read_bytes()
    link = tmp_path / 'link.json'
    link.symlink_to(target)
    dangling = tmp_path / 'dangling.json'
    dangling.symlink_to(tmp_path / 'missing.json')
    fifo = tmp_path / 'fifo'
    os.mkfifo(fifo)
    for path in (link, dangling, tmp_path, fifo):
        with pytest.raises(CalibrationError):
            load_config(path)
        with pytest.raises(CalibrationError):
            save_config(path, custom())
    assert target.read_bytes() == original
    assert link.is_symlink() and dangling.is_symlink()


def test_failed_atomic_replace_keeps_previous_config_and_removes_temporary(tmp_path, monkeypatch):
    path = tmp_path / 'calibration.json'
    save_config(path, default_config())
    original = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError('replace failed')

    monkeypatch.setattr(calibration.os, 'replace', fail)
    with pytest.raises(CalibrationError):
        save_config(path, custom())
    assert path.read_bytes() == original
    assert not list(tmp_path.glob('.calibration-*'))


def test_hot_reload_preserves_last_valid_config_and_resets_smoothing(tmp_path):
    path = tmp_path / 'calibration.json'
    state = collector.CalibrationState(path, 'local-19v-v1')
    assert not state.refresh(now=90)
    assert state.snapshot()['configurable'] is True
    first = save_config(path, custom(base=1))
    assert state.refresh(now=99)
    for timestamp in (100, 102, 104, 106):
        previous = state.update(sample(timestamp))
    assert previous['ac_input_estimate_w'] is not None

    # A malformed file must neither replace the current model nor reset its window.
    estimator = state.estimator
    path.write_text('{broken')
    assert not state.refresh(now=107)
    assert state.error and state.estimator is estimator
    assert state.update(sample(108))['calibration_revision'] == first['revision']
    save_config(path, first)
    assert not state.refresh(now=109)
    assert state.error is None and state.estimator is estimator

    second = save_config(path, custom(base=2))
    assert state.refresh(now=110)
    assert not state.estimator.window
    assert previous['calibration_revision'] == first['revision']
    with pytest.raises(ValueError, match='predates'):
        state.update(sample(110))
    for timestamp in (112, 114, 116):
        assert state.update(sample(timestamp))['ac_input_estimate_w'] is None
    updated = state.update(sample(118))
    assert updated['calibration_revision'] == second['revision']
    assert updated['ac_input_estimate_w'] == pytest.approx(previous['ac_input_estimate_w'] * 2)

    state.reset_estimator()  # The same reset used on USB device rediscovery.
    assert state.config == second and state.estimator.config == second
    assert state.update(sample(120))['ac_input_estimate_w'] is None

    path.unlink()
    assert state.refresh(now=121)
    assert state.config == default_config('local-19v-v1')
    assert state.update(sample(122))['ac_input_estimate_w'] is None


def test_clock_rollback_after_first_new_report_clears_window_and_recovers(tmp_path):
    path = tmp_path / 'calibration.json'
    state = collector.CalibrationState(path)
    config = save_config(path, custom())
    assert state.refresh(now=1000)
    with pytest.raises(ValueError, match='predates'):
        state.update(sample(999))
    assert state.changed_at == 1000
    for timestamp in (1001, 1003, 1005, 1007):
        previous = state.update(sample(timestamp))
    assert state.changed_at is None
    assert previous['ac_input_estimate_w'] is not None

    # A new report is fresh against the corrected wall clock even though its
    # timestamp is lower than both the prior sample and the old switch barrier.
    for timestamp in (950, 952, 954):
        value = state.update(sample(timestamp))
        assert value['ac_input_estimate_w'] is None
        assert value['ac_estimate_quality'] == 'warming_up'
    recovered = state.update(sample(956))
    assert recovered['ac_input_estimate_w'] == previous['ac_input_estimate_w']
    assert recovered['calibration_revision'] == config['revision']


def test_replay_publishes_empty_sample_on_change_and_never_mixes_revisions(tmp_path, monkeypatch):
    path = tmp_path / 'calibration.json'
    first = save_config(path, custom(base=1))
    second = normalize_config(custom(base=2))
    snapshots = []

    class Clock:
        now = 100.0
        changed = False

        def time(self):
            return self.now

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now = round(self.now + seconds, 6)
            if self.now >= 112 and not self.changed:
                save_config(path, second)
                self.changed = True

    clock = Clock()
    configuration_reads = []
    original_load = collector.load_config

    def counted_load(path):
        configuration_reads.append(clock.monotonic())
        return original_load(path)

    monkeypatch.setattr(collector, 'time', clock)
    monkeypatch.setattr(collector, 'load_config', counted_load)
    monkeypatch.setattr(collector.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(collector, 'atomic_json', lambda path, data: snapshots.append(copy.deepcopy(data)))
    collector.run(tmp_path / 'latest.json', duration=24, replay=ROOT / 'fixtures' / 'online.hex',
                  calibration_config=path)
    old = [s for s in snapshots if s['calibration']['config']['revision'] == first['revision']]
    new = [s for s in snapshots if s['calibration']['config']['revision'] == second['revision']]
    assert any(s['sample'] and s['sample']['ac_input_estimate_w'] is not None for s in old)
    assert new and new[0]['sample'] is None
    assert new[0]['heartbeat'] == 112
    assert any(s['sample'] and s['sample']['ac_input_estimate_w'] is not None for s in new)
    assert len(configuration_reads) <= 25
    assert all(second - first >= 1 for first, second in zip(configuration_reads, configuration_reads[1:]))
    for snapshot in snapshots:
        assert snapshot['source'] == 'replay'
        assert snapshot['calibration']['configurable'] is True
        if snapshot['sample']:
            assert snapshot['sample']['calibration_revision'] == snapshot['calibration']['config']['revision']
            if snapshot['calibration']['config']['revision'] == second['revision']:
                assert snapshot['sample']['timestamp'] > 112
                if snapshot['sample']['timestamp'] < 120:
                    assert snapshot['sample']['ac_input_estimate_w'] is None


def test_collector_cli_uses_file_environment_and_preserves_preset_fallback(monkeypatch):
    calls = []
    monkeypatch.setenv('UPS_CALIBRATION_CONFIG', '/example/calibration.json')
    monkeypatch.setenv('UPS_CALIBRATION_PROFILE', 'local-19v-v1')
    monkeypatch.setattr('sys.argv', ['collector'])
    monkeypatch.setattr(collector, 'run', lambda *args: calls.append(args))
    collector.main()
    assert calls[0][-2:] == ('local-19v-v1', '/example/calibration.json')
