"""Optional, explicitly selected empirical estimates; never universal device calibration.

The local-19v-v1 profile records one installation's offline calibration. Raw UPS
channels remain untouched, and the default profile exposes no calibrated estimate.
"""
from collections import deque
import math
from statistics import mean

from .calibration import DEFAULT_COEFFICIENTS, PRESET_PROFILES, default_config, normalize_config

BASE_GAIN = DEFAULT_COEFFICIENTS['base_gain']
BATTERY_NOMINAL_GAIN = DEFAULT_COEFFICIENTS['battery_gain']
CHARGE_GAIN = DEFAULT_COEFFICIENTS['charge_gain']
CALIBRATION_PROFILES = PRESET_PROFILES
ESTIMATE_FIELDS = (
    'battery_energy_estimate_w', 'battery_estimate_basis', 'battery_estimate_quality',
    'ac_input_estimate_w', 'ac_estimate_quality', 'ac_estimate_model',
    'ac_estimate_window_sec', 'calibration_profile', 'calibration_verified',
    'calibration_revision', 'calibration_coefficients',
)


def finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class PowerEstimator:
    def __init__(self, profile='none', *, config=None):
        self.config = default_config(profile) if config is None else normalize_config(config)
        self.profile = self.config['profile']
        self.coefficients = self.config['coefficients']
        self.window = deque()
        self.mode = None
        self.last_ts = None
        self.last_estimate = None

    def update(self, sample):
        result = self._update(sample)
        if finite_number(sample.get('timestamp')):
            self.last_ts = sample['timestamp']
            self.last_estimate = {k: dict(result[k]) if isinstance(result[k], dict) else result[k]
                                  for k in ESTIMATE_FIELDS}
        return result

    def _update(self, sample):
        enabled = self.profile != 'none'
        custom = self.profile == 'custom'
        revision = self.config['revision']
        sample.update(
            battery_energy_estimate_w=None,
            battery_estimate_basis=(f'us3000_battery_custom_{revision}' if custom else
                                    'nominal_43_2wh_soc_v1' if enabled else None),
            battery_estimate_quality='unavailable' if enabled else 'not_configured',
            ac_input_estimate_w=None,
            ac_estimate_quality='unavailable' if enabled else 'not_configured',
            ac_estimate_model=(f'us3000_19v_custom_{revision}' if custom else
                               'us3000_19v_v1' if enabled else None),
            ac_estimate_window_sec=8, calibration_profile=self.profile,
            calibration_revision=revision,
            calibration_coefficients=dict(self.coefficients) if enabled else None,
            calibration_verified=False,
        )
        ts, mode = sample.get('timestamp'), sample.get('mode')
        if not finite_number(ts):
            self.window.clear()
            self.last_ts = self.last_estimate = None
            sample.update(ac_estimate_quality='invalid_data', battery_estimate_quality='invalid_data')
            return sample
        if ts == self.last_ts and mode == self.mode and self.last_estimate is not None:
            # Re-reading one snapshot must not count it twice or reset smoothing.
            sample.update({k: dict(value) if isinstance(value, dict) else value
                           for k, value in self.last_estimate.items()})
            return sample
        if (mode != self.mode or (self.last_ts is not None and
                (ts < self.last_ts or ts - self.last_ts > 5))):
            self.window.clear()
        self.mode = mode
        if not enabled:
            return sample
        if mode == 'battery':
            raw = sample.get('battery_discharge_power_candidate_w')
            if not finite_number(raw) or raw <= 0:
                self.window.clear()
                sample['battery_estimate_quality'] = 'invalid_data'
                return sample
            value = self._smooth(ts, raw * self.coefficients['battery_gain'])
            sample['battery_energy_estimate_w'] = value
            sample['battery_estimate_quality'] = (('custom_unverified' if custom else
                                                   'nominal_capacity_assumption')
                                                  if value is not None else 'warming_up')
            return sample
        if mode not in ('online', 'charging'):
            self.window.clear()
            return sample
        vin = sample.get('input_voltage')
        if not finite_number(vin) or not 18 <= vin <= 20:
            self.window.clear()
            sample['ac_estimate_quality'] = 'unsupported_voltage'
            return sample
        charge = sample.get('battery_charge_power_candidate_w') if mode == 'charging' else 0
        fields = sample.get('raw_fields')
        raw = fields.get('byte_28') if isinstance(fields, dict) else None
        if (not finite_number(charge) or charge < 0 or not finite_number(raw)
                or not 0 <= raw <= 255):
            self.window.clear()
            sample['ac_estimate_quality'] = 'invalid_data'
            return sample
        estimate = self._smooth(ts, raw * self.coefficients['base_gain'] +
                               charge * self.coefficients['charge_gain'])
        if estimate is None:
            sample['ac_estimate_quality'] = 'warming_up'
            return sample
        sample['ac_input_estimate_w'] = estimate
        if custom:
            sample['ac_estimate_quality'] = 'custom_unverified'
            return sample
        lo, hi = (57, 79) if mode == 'online' else (70, 84)
        current = sample.get('battery_charge_current_candidate_a')
        sample['ac_estimate_quality'] = ('calibrated_range' if lo <= estimate <= hi and
            (mode != 'charging' or finite_number(current) and .60 <= current <= .70) else 'extrapolated')
        return sample

    def _smooth(self, ts, value):
        self.window.append((ts, value))
        while self.window and self.window[0][0] < ts - 8:
            self.window.popleft()
        if len(self.window) < 4 or ts - self.window[0][0] < 6:
            return None
        return round(mean(x[1] for x in self.window), 2)
