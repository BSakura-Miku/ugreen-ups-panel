"""Observed battery-side energy estimates, never capacity or health measurements.

The side table is deliberately independent of the legacy nine-column session
table. Its integration anchor and totals commit with the session cursor, allowing
old versions to keep writing sessions without pretending they recorded energy.
"""
import json

from .calibration import CalibrationError, normalize_config
from .power import finite_number


MAX_INTERVAL_SEC = 5
TABLE = 'battery_session_energy'


def calibration_rejected(view):
    """Explicit ingestion rejection wins over any residual sample metadata."""
    validation = view.get('calibration_validation') if isinstance(view, dict) else None
    return isinstance(validation, dict) and validation.get('valid') is False


def _soc(sample):
    value = sample.get('soc')
    return value if finite_number(value) and 0 <= value <= 100 else None


def device_identity(value):
    """Use the collector's USB identity, including devices with no serial number."""
    if not isinstance(value, dict):
        return None
    result = {}
    for key in ('serial', 'path', 'vendor', 'product'):
        if key in value:
            item = value[key]
            if not isinstance(item, str) or len(item) > 256:
                return None
            result[key] = item
    for key in ('bus', 'device', 'address'):
        if key in value:
            item = value[key]
            if type(item) is not int or item < 0:
                return None
            result[key] = item
    if (result.get('serial') or
            result.get('path') and 'bus' in result and ('device' in result or 'address' in result)):
        return result
    return None


def evidence(view):
    """Rebuild the supplied configuration; a model name alone proves nothing."""
    sample = view['sample']
    source = view.get('source')
    source = source if isinstance(source, str) and 0 < len(source) <= 128 else None
    identity = {'source': source, 'device': device_identity(view.get('device')),
                'decoder_version': sample.get('decoder_version'),
                'formula_version': sample.get('formula_version'), 'config': None}
    for key in ('decoder_version', 'formula_version'):
        if type(identity[key]) is not int or identity[key] < 1:
            identity[key] = None
    if calibration_rejected(view):
        return identity, None, 'invalid_basis'
    # A disabled model never acquires a default coefficient or a numeric estimate.
    reason = 'not_configured' if sample.get('calibration_profile') == 'none' else 'invalid_basis'
    config = {'schema': sample.get('calibration_schema', 1),
              'profile': sample.get('calibration_profile'),
              'coefficients': sample.get('calibration_coefficients')}
    if 'ac_voltage_nominal_v' in sample:
        config['ac_voltage_nominal_v'] = sample['ac_voltage_nominal_v']
    try:
        if 'calibration_coefficients' not in sample:
            return identity, None, reason
        if config['profile'] != 'none' and not isinstance(config['coefficients'], dict):
            return identity, None, reason
        normalized = normalize_config(config)
    except (CalibrationError, TypeError, ValueError, OverflowError):
        return identity, None, reason
    if sample.get('calibration_revision') != normalized['revision']:
        return identity, None, reason
    identity['config'] = normalized
    if normalized['profile'] == 'none' or normalized['coefficients']['battery_gain'] is None:
        return identity, None, 'not_configured'
    expected_basis = ('nominal_43_2wh_soc_v1' if normalized['profile'] == 'local-19v-v1'
                      else 'us3000_battery_custom_' + normalized['revision'])
    if sample.get('battery_estimate_basis') != expected_basis or any(value is None for value in identity.values()):
        return identity, None, 'invalid_basis'
    basis = {'profile': normalized['profile'], 'revision': normalized['revision'],
             'battery_gain': normalized['coefficients']['battery_gain'],
             'estimate_basis': expected_basis, 'source': source, 'device': identity['device'],
             'decoder_version': identity['decoder_version'], 'formula_version': identity['formula_version']}
    return identity, basis, None


def context_changed(previous, current):
    """Missing evidence breaks integration; positively changed evidence splits sessions."""
    return any(previous.get(key) is not None and value is not None and previous[key] != value
               for key, value in current.items())


def _reason(state, reason):
    if reason not in state['reasons']:
        state['reasons'].append(reason)


def _power(sample, basis):
    raw = sample.get('battery_discharge_power_candidate_w')
    if not finite_number(raw) or raw <= 0:
        return None
    try:
        value = raw * basis['battery_gain']
    except OverflowError:
        return None
    return value if finite_number(value) and value > 0 else None


def new_state(session, view, identity, basis):
    sample = view['sample']
    return {'schema': 1, 'session_id': session['id'], 'start_ts': session['start_ts'],
            'last_ts': session['last_ts'], 'sample_count': session['sample_count'],
            'start_soc': _soc(sample), 'end_soc': _soc(sample),
            'estimate_wh': 0.0, 'covered_duration_sec': 0.0, 'interval_count': 0,
            'reasons': [], 'basis': basis, 'identity': identity, 'anchor': None}


def advance(state, session, view, identity, basis, reason):
    """Only observed, valid adjacent battery points form trapezoids."""
    sample = view['sample']
    ts = sample['timestamp']
    state.update(last_ts=ts, sample_count=session['sample_count'], end_soc=_soc(sample))
    state['identity'].update({key: value for key, value in identity.items() if value is not None})
    if basis is None:
        _reason(state, reason)
        state['anchor'] = None
        return
    if state['basis'] is None:
        state['basis'] = basis
    power = _power(sample, basis)
    if power is None:
        _reason(state, 'invalid_power')
        state['anchor'] = None
        return
    previous = state['anchor']
    state['anchor'] = {'timestamp': ts, 'power': power}
    if previous is None:
        return
    elapsed = ts - previous['timestamp']
    if elapsed > MAX_INTERVAL_SEC:
        _reason(state, 'sample_gap')
        return
    if elapsed <= 0:
        # Session ingest normally filters these; keep the accumulator defensive.
        state['anchor'] = None
        _reason(state, 'timestamp_rollback')
        return
    amount = (previous['power'] / 2 + power / 2) * (elapsed / 3600)
    total = state['estimate_wh'] + amount
    if not finite_number(amount) or amount <= 0 or not finite_number(total) or total <= 0:
        state['anchor'] = None
        _reason(state, 'invalid_energy')
        return
    state['estimate_wh'] = total
    state['covered_duration_sec'] += elapsed
    state['interval_count'] += 1


def render(state, session=None):
    if state is None:
        return None
    last_ts = state['last_ts']
    end_soc = state['end_soc']
    reasons = list(state['reasons'])
    if session is not None and session['last_ts'] > last_ts:
        # An old version may have continued and closed this session. Render the
        # uncovered tail without writing from a GET or inventing its battery SOC.
        last_ts = session['last_ts']
        end_soc = session['end_soc'] if session['end_reason'] is None else None
        if 'continuity_lost' not in reasons:
            reasons.append('continuity_lost')
    observed = max(0, last_ts - state['start_ts'])
    covered = min(observed, state['covered_duration_sec'])
    intervals = state['interval_count']
    if not intervals:
        reasons.append('insufficient_samples')
    ratio = min(1.0, covered / observed) if observed > 0 else None
    return {'schema': 1, 'estimate_wh': state['estimate_wh'] if intervals else None,
            'covered_duration_sec': covered, 'observed_duration_sec': observed,
            'coverage_ratio': ratio, 'interval_count': intervals,
            'status': 'unavailable' if not intervals else 'available' if covered == observed else 'partial',
            'reasons': reasons, 'basis': state['basis'],
            'start_soc': state['start_soc'], 'end_soc': end_soc}


class BatteryEnergy:
    def __init__(self, db):
        db.execute('''CREATE TABLE IF NOT EXISTS battery_session_energy (
                        session_id TEXT PRIMARY KEY, payload TEXT NOT NULL)''')
        self.pending = {}
        self.active = None

    def restore(self, db, session, legacy_state):
        if session is None:
            return True
        row = db.execute('SELECT payload FROM battery_session_energy WHERE session_id=?', (session['id'],)).fetchone()
        if row is None:
            return False
        state = json.loads(row[0])
        self.active = state
        return (state['schema'] == 1 and state['session_id'] == session['id'] and
                session['end_reason'] is None and session['end_ts'] is None and
                state['start_ts'] == session['start_ts'] and state['last_ts'] == session['last_ts'] and
                state['sample_count'] == session['sample_count'] and
                (state['anchor'] is None or state['anchor']['timestamp'] == state['last_ts']) and
                legacy_state['active_id'] == session['id'] and legacy_state['last_mode'] == 'battery' and
                legacy_state['last_ts'] == state['last_ts'])

    def remember(self):
        if self.active is not None:
            # JSON also validates that no NaN/Infinity can reach the database.
            self.pending[self.active['session_id']] = json.dumps(self.active, sort_keys=True, allow_nan=False)

    def break_anchor(self, reason):
        if self.active is not None:
            self.active['anchor'] = None
            _reason(self.active, reason)
            self.remember()

    def close(self, reason=None, legacy_session=None):
        if self.active is not None:
            self.active['anchor'] = None
            if reason:
                _reason(self.active, reason)
            # An old panel may have extended the legacy session without recording
            # energy. Its known last battery point enlarges the uncovered range.
            if legacy_session is not None and legacy_session['last_ts'] > self.active['last_ts']:
                self.active.update(last_ts=legacy_session['last_ts'], end_soc=legacy_session['end_soc'],
                                   sample_count=legacy_session['sample_count'])
            self.remember()
            self.active = None

    def start(self, session, view, identity, basis, reason):
        self.active = new_state(session, view, identity, basis)
        self.ingest(session, view, identity, basis, reason)

    def ingest(self, session, view, identity, basis, reason):
        advance(self.active, session, view, identity, basis, reason)
        self.remember()

    def write(self, db):
        db.executemany('INSERT OR REPLACE INTO battery_session_energy VALUES(?,?)', self.pending.items())

    def committed(self):
        self.pending.clear()

    def history(self, db, start, now):
        records = dict(db.execute('''SELECT e.session_id,e.payload FROM battery_session_energy e
                                    JOIN battery_sessions s ON s.id=e.session_id
                                    WHERE COALESCE(s.end_ts,s.last_ts)>=? AND s.start_ts<=?''', (start, now)))
        records.update(self.pending)
        return {key: json.loads(payload) for key, payload in records.items()}

    @staticmethod
    def prune(db):
        db.execute('''DELETE FROM battery_session_energy
                      WHERE NOT EXISTS(SELECT 1 FROM battery_sessions s WHERE s.id=battery_session_energy.session_id)''')
