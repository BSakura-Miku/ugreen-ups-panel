"""Bounded, passive observations of unclassified US3000 raw byte channels.

Only the independent background observer calls ingest(). Reading a snapshot does
not accept telemetry, create aliases, change segments or increment counters.
The channel values deliberately have no temperature unit or sensor attribution.
"""
from collections import deque
from dataclasses import dataclass
import hashlib
import json
import math
import re
import secrets
from threading import RLock
import time

from .calibration import PROFILES


MAX_WINDOW_SEC = 3600
MAX_SAMPLES = 4096
MAX_SAMPLE_AGE_SEC = 10
MAX_SAMPLE_GAP_SEC = 5
MAX_TIMESTAMP = 253402300799  # A bounded Unix timestamp, through year 9999.
SOURCES = ('usbmon', 'replay')
MODES = ('online', 'charging', 'battery', 'unknown')
POINT_FIELDS = (
    'timestamp', 'segment', 'device_alias', 'source', 'mode', 'raw_status',
    'byte_26', 'byte_27', 'byte_28', 'soc', 'battery_voltage',
    'adapter_input_voltage_v', 'ups_output_voltage_v', 'current',
    'battery_charge_current_candidate_a', 'battery_discharge_current_candidate_a',
    'decoder_version', 'formula_version', 'calibration_revision', 'calibration_profile',
)
_DATA_FIELDS = tuple(key for key in POINT_FIELDS if key not in ('segment', 'device_alias'))
_LATEST_FIELDS = ('timestamp', 'byte_26', 'byte_27', 'byte_28', 'source', 'mode', 'device_alias')


def _finite(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _integer(value, lower, upper):
    return value if type(value) is int and lower <= value <= upper else None


def _number(value, lower, upper):
    return value if _finite(value) and lower <= value <= upper else None


def _time(value):
    return _number(value, 0, MAX_TIMESTAMP)


def _now(value):
    result = time.time() if value is None else value
    if _time(result) is None:
        raise ValueError('now must be a finite non-negative Unix timestamp')
    return result


def _marker(value):
    """Small internal comparison keys, including for malformed scalar fields."""
    if value is None or type(value) is bool or _finite(value):
        return (type(value).__name__, value)
    if type(value) is str and len(value) <= 256:
        return ('str', value)
    return (type(value).__name__, None)


@dataclass(frozen=True)
class _Candidate:
    data: dict
    device_key: bytes
    context: tuple
    signature: tuple

    @property
    def key(self):
        return (self.context, self.data['timestamp'])


@dataclass(frozen=True)
class _Record:
    candidate: _Candidate
    point: dict
    clock: float


class RawObservationMonitor:
    """An in-memory window, with cumulative counters since process startup.

    Windows use elapsed observation time. A backwards wall-clock correction
    starts a segment without deleting the previous segment or giving records a
    negative age. Point timestamps always preserve the original sample time.
    Modes, identities, decoder/formula and calibration changes also start new
    segments. Calibration changes keep the same timestamp deduplication context.

    gap_count counts interrupted sampling continuity (staleness, invalid or
    conflicting input, gaps over five seconds and clock reversals), once when
    valid sampling resumes. Ordinary mode/identity changes alone are not gaps.
    conflict_count counts conflicting context/timestamp pairs in the retained
    window. Consecutive repeats of the same malformed input count as one
    rejection; stale input is not a rejection.
    """

    def __init__(self, window_sec=MAX_WINDOW_SEC, max_samples=MAX_SAMPLES):
        if not _finite(window_sec) or not 0 < window_sec <= MAX_WINDOW_SEC:
            raise ValueError('window_sec must be greater than zero and at most 3600')
        if type(max_samples) is not int or not 1 <= max_samples <= MAX_SAMPLES:
            raise ValueError('max_samples must be an integer from 1 to 4096')
        self.window_sec = window_sec
        self.max_samples = max_samples
        self._lock = RLock()
        self._salt = secrets.token_bytes(32)
        self._records = deque()
        self._seen = {}
        self._conflicted = set()
        self._aliases = {}
        self._last = None
        self._last_wall = None
        self._clock = 0
        self._segment = 0
        self._started_at = None
        self._interrupted = False
        self._latest_available = False
        self._last_rejection = None
        self._capacity_discarded_clock = None
        self._gap_count = self._conflict_count = self._rejected_count = 0

    def _device_key(self, device):
        # Device details are compared internally, then immediately reduced to a
        # keyed digest. Neither their JSON nor a stable serial-derived digest is
        # part of the public point/export contract.
        try:
            encoded = json.dumps(device, sort_keys=True, separators=(',', ':'),
                                 allow_nan=False).encode('utf-8')
        except (TypeError, ValueError, RecursionError, UnicodeError):
            return None
        if len(encoded) > 8192:
            return None
        return hashlib.blake2b(encoded, key=self._salt, digest_size=16).digest()

    def _candidate(self, view, now):
        if not isinstance(view, dict) or view.get('fresh') is not True:
            return None, 'not_fresh'
        source, sample = view.get('source'), view.get('sample')
        if source not in SOURCES or not isinstance(sample, dict):
            return None, 'invalid_sample'
        timestamp = _time(sample.get('timestamp'))
        if timestamp is None:
            return None, 'invalid_timestamp'
        if not 0 <= now - timestamp <= MAX_SAMPLE_AGE_SEC:
            return None, 'not_fresh'
        fields = sample.get('raw_fields')
        if not isinstance(fields, dict):
            return None, 'invalid_channels'
        byte26 = _integer(fields.get('byte_26'), 0, 255)
        byte27 = _integer(fields.get('byte_27'), 0, 255)
        if byte26 is None or byte27 is None:
            return None, 'invalid_channels'
        device_key = self._device_key(view.get('device'))
        if device_key is None:
            return None, 'invalid_identity'
        mode = sample.get('mode')
        profile = sample.get('calibration_profile')
        revision = sample.get('calibration_revision')
        data = {
            'timestamp': timestamp, 'source': source,
            'mode': mode if mode in MODES else 'unknown',
            'raw_status': _integer(sample.get('raw_status'), 0, 255),
            'byte_26': byte26, 'byte_27': byte27,
            'byte_28': _integer(fields.get('byte_28'), 0, 255),
            'soc': _number(sample.get('soc'), 0, 100),
            'battery_voltage': _number(sample.get('battery_voltage'), 0, 40),
            'adapter_input_voltage_v': _number(sample.get('adapter_input_voltage_v'), 0, 40),
            'ups_output_voltage_v': _number(sample.get('ups_output_voltage_v'), 0, 40),
            'current': _number(sample.get('current'), 0, 30),
            'battery_charge_current_candidate_a': _number(
                sample.get('battery_charge_current_candidate_a'), 0, 30),
            'battery_discharge_current_candidate_a': _number(
                sample.get('battery_discharge_current_candidate_a'), 0, 30),
            'decoder_version': _integer(sample.get('decoder_version'), 1, 2147483647),
            'formula_version': _integer(sample.get('formula_version'), 1, 2147483647),
            'calibration_revision': revision if type(revision) is str and
                re.fullmatch(r'[0-9a-f]{64}', revision) else None,
            'calibration_profile': profile if type(profile) is str and profile in PROFILES else None,
        }
        context = (device_key, source, data['decoder_version'], data['formula_version'])
        return _Candidate(data, device_key, context,
                          tuple(data[key] for key in _DATA_FIELDS)), None

    def _clock_at(self, now):
        return self._clock + (max(0, now - self._last_wall) if self._last_wall is not None else 0)

    def _evict(self, capacity=False):
        record = self._records.popleft()
        key = record.candidate.key
        self._seen.pop(key, None)
        self._conflicted.discard(key)
        if capacity:
            self._capacity_discarded_clock = record.clock

    def _prune(self, clock):
        while self._records and self._records[0].clock < clock - self.window_sec:
            self._evict()

    def _prune_aliases(self):
        used = {record.candidate.device_key for record in self._records}
        known = set(self._seen)
        if self._last is not None:
            used.add(self._last.candidate.device_key)
            known.add(self._last.candidate.key)
        self._aliases = {key: value for key, value in self._aliases.items() if key in used}
        self._conflicted.intersection_update(known)

    def _reject(self, view, reason):
        self._interrupted = True
        self._latest_available = False
        if reason == 'not_fresh':
            self._last_rejection = None
            return
        sample = view.get('sample') if isinstance(view, dict) else None
        sample = sample if isinstance(sample, dict) else {}
        fields = sample.get('raw_fields')
        fields = fields if isinstance(fields, dict) else {}
        marker = (reason, self._device_key(view.get('device')),
                  _marker(view.get('source')), _marker(sample.get('timestamp')),
                  _marker(fields.get('byte_26')), _marker(fields.get('byte_27')))
        if marker != self._last_rejection:
            self._rejected_count += 1
        self._last_rejection = marker

    def ingest(self, view, now=None):
        """Accept one distinct valid observation; return whether a point was added."""
        now = _now(now)
        with self._lock:
            clock = self._clock_at(now)
            if self._last_wall is not None and now < self._last_wall:
                self._interrupted = True
            self._clock, self._last_wall = clock, now
            self._prune(clock)
            current, reason = self._candidate(view, now)
            if current is None:
                self._reject(view, reason)
                self._prune_aliases()
                return False
            self._last_rejection = None
            previous = self._last
            existing = self._seen.get(current.key)
            if existing is None and previous is not None and current.key == previous.candidate.key:
                existing = previous
            if existing is not None:
                same = current.signature == existing.candidate.signature
                if not same:
                    if current.key not in self._conflicted:
                        self._conflict_count += 1
                        self._conflicted.add(current.key)
                    self._interrupted = True
                if previous is not None and (current.context != previous.candidate.context or
                        current.data['timestamp'] < previous.point['timestamp']):
                    self._interrupted = True
                self._latest_available = same and previous is existing
                self._prune_aliases()
                return False
            gap = previous is not None and (self._interrupted or
                current.data['timestamp'] < previous.point['timestamp'] or
                current.data['timestamp'] - previous.point['timestamp'] > MAX_SAMPLE_GAP_SEC)
            if previous is None or gap or current.context != previous.candidate.context or any(
                    current.data[key] != previous.point[key]
                    for key in ('mode', 'calibration_profile', 'calibration_revision')):
                self._segment += 1
            if gap:
                self._gap_count += 1
            if self._started_at is None:
                self._started_at = current.data['timestamp']
            alias = self._aliases.get(current.device_key)
            if alias is None:
                alias = 'device-' + secrets.token_hex(8)
                self._aliases[current.device_key] = alias
            values = dict(current.data, segment=self._segment, device_alias=alias)
            record = _Record(current, {key: values[key] for key in POINT_FIELDS}, clock)
            if len(self._records) == self.max_samples:
                self._evict(capacity=True)
            self._records.append(record)
            self._seen[current.key] = record
            self._last = record
            self._interrupted = False
            self._latest_available = True
            self._prune_aliases()
            return True

    def _latest(self, view, now):
        result = dict.fromkeys(_LATEST_FIELDS)
        result.update(fresh=False, observed=False)
        previous = self._last
        if view is not None:
            current, _ = self._candidate(view, now)
            if current is None:
                return result
            values = dict(current.data, device_alias=self._aliases.get(current.device_key))
            result.update({key: values[key] for key in _LATEST_FIELDS})
            result.update(fresh=True, observed=previous is not None and
                          current.key == previous.candidate.key and
                          current.signature == previous.candidate.signature)
        elif previous is not None:
            result.update({key: previous.point[key] for key in _LATEST_FIELDS})
            result.update(fresh=self._latest_available and
                          0 <= now - previous.point['timestamp'] <= MAX_SAMPLE_AGE_SEC and
                          self._clock_at(now) - previous.clock <= MAX_SAMPLE_AGE_SEC,
                          observed=True)
        return result

    def snapshot(self, view=None, now=None, minutes=60):
        """Return a fresh projection of the window, without mutating the observer."""
        now = _now(now)
        if not _finite(minutes) or not 1 <= minutes <= 60:
            raise ValueError('minutes must be from 1 to 60')
        window = min(self.window_sec, minutes * 60)
        with self._lock:
            cutoff = self._clock_at(now) - window
            points = [dict(record.point) for record in self._records if record.clock >= cutoff]
            return {
                'schema': 1, 'window_sec': window, 'max_samples': self.max_samples,
                'count': len(points), 'points': points, 'started_at': self._started_at,
                'first_timestamp': points[0]['timestamp'] if points else None,
                'last_timestamp': points[-1]['timestamp'] if points else None,
                'truncated': self._capacity_discarded_clock is not None and
                    self._capacity_discarded_clock >= cutoff,
                'gap_count': self._gap_count, 'conflict_count': self._conflict_count,
                'rejected_count': self._rejected_count, 'latest': self._latest(view, now),
            }
