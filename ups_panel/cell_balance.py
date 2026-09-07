"""Continuous standby observations with product-reference cell-delta bands.

These thresholds are not official UGREEN grades, fault diagnoses, or battery
health/SOH estimates. The monitor neither controls the UPS nor emits alarms.
Its in-memory observations deliberately start over after a process restart.
"""
from collections import Counter, deque
from dataclasses import dataclass
import json
import math
from threading import RLock


REQUIRED_STANDBY_SEC = 1800
PERSISTENCE_SEC = 120
RECENT_WINDOW_SEC = 1800
MAX_SAMPLE_GAP_SEC = 5
# The independent observer polls every 0.5 seconds; new collector frames usually
# arrive every two seconds. Even a new frame at every poll fits this window.
# Also bound memory if an unexpected caller supplies timestamps much faster.
MAX_RECENT_SAMPLES = 4096
THRESHOLDS = {'good_below_mv': 20, 'minor_below_mv': 50, 'elevated_below_mv': 100}


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True)
class _Observation:
    timestamp: float
    mode: str
    cells: tuple
    identity: str
    delta_mv: float
    lowest_cells: tuple


def _observation(view):
    if not isinstance(view, dict) or view.get('fresh') is not True:
        return None
    sample = view.get('sample')
    if not isinstance(sample, dict):
        return None
    timestamp, mode, cells = sample.get('timestamp'), sample.get('mode'), sample.get('cells')
    if (not _finite(timestamp) or timestamp < 0 or mode not in ('online', 'charging', 'battery')
            or not isinstance(cells, list) or len(cells) != 4
            or any(not _finite(value) or not 1 <= value <= 5 for value in cells)):
        return None
    try:
        identity = json.dumps([view.get('source'), view.get('device'), sample.get('decoder_version')],
                              sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return None
    low, high = min(cells), max(cells)
    # Remove binary subtraction noise at 20/50/100 mV without rounding to the
    # dashboard's display precision. The collector supplies millivolt cells.
    delta_mv = round((high - low) * 1000, 9)
    lowest = tuple(index + 1 for index, value in enumerate(cells) if value == low) if delta_mv else ()
    return _Observation(timestamp, mode, tuple(cells), identity, delta_mv, lowest)


def _level(delta_mv):
    if delta_mv < THRESHOLDS['good_below_mv']:
        return 'good'
    if delta_mv < THRESHOLDS['minor_below_mv']:
        return 'minor'
    if delta_mv < THRESHOLDS['elevated_below_mv']:
        return 'elevated'
    return 'check'


class CellBalanceMonitor:
    """Feed from the independent observer; snapshot() never changes observation state."""

    def __init__(self):
        self._lock = RLock()
        self._recent = deque(maxlen=MAX_RECENT_SAMPLES)
        self._reset()

    def _reset(self):
        self._last = None
        self._standby_start = None
        self._candidate_level = None
        self._candidate_since = None
        self._recent.clear()

    def ingest(self, view):
        """Observe one background snapshot independently of database writes."""
        with self._lock:
            current = _observation(view)
            if current is None:
                self._reset()
                return
            previous = self._last
            if current == previous:
                return
            if (current.mode != 'online' or (previous is not None and (
                    current.mode != previous.mode or current.identity != previous.identity
                    or current.timestamp <= previous.timestamp
                    or current.timestamp - previous.timestamp > MAX_SAMPLE_GAP_SEC))):
                # Same-timestamp edits are conflicting telemetry, not another
                # independent sample. Restart rather than reuse a prior grade.
                self._reset()
            if current.mode == 'online':
                cutoff = current.timestamp - RECENT_WINDOW_SEC
                while self._recent and self._recent[0][0] < cutoff:
                    self._recent.popleft()
                if len(self._recent) == MAX_RECENT_SAMPLES:
                    # Do not silently discard part of an advertised window if
                    # an unexpected sampling rate reaches the memory bound.
                    self._reset()
                if self._standby_start is None:
                    self._standby_start = current.timestamp
                candidate = _level(current.delta_mv)
                if candidate != self._candidate_level:
                    self._candidate_level = candidate
                    self._candidate_since = current.timestamp
                self._recent.append((current.timestamp, current.delta_mv, current.lowest_cells))
            self._last = current

    def snapshot(self, view):
        """Describe this exact live snapshot, without counting GETs as samples."""
        with self._lock:
            current = _observation(view)
            result = {
                'schema': 1, 'state': 'unavailable', 'level': None,
                'delta_mv': current.delta_mv if current else None,
                'standby_duration_sec': 0, 'required_standby_sec': REQUIRED_STANDBY_SEC,
                'persistence_sec': PERSISTENCE_SEC,
                'candidate_level': None, 'candidate_duration_sec': 0,
                'lowest_cells': list(current.lowest_cells) if current else [],
                'frequent_lowest_cell': None, 'recent_max_delta_mv': None,
                'recent_window_sec': RECENT_WINDOW_SEC, 'recent_sample_count': 0,
                'sample_timestamp': current.timestamp if current else None,
                'observed': current is not None and current == self._last,
                'reason': None, 'thresholds': dict(THRESHOLDS), 'reference_only': True,
            }
            if current is None:
                result['reason'] = ('no_fresh_sample' if not isinstance(view, dict)
                                    or view.get('fresh') is not True else 'invalid_sample')
                return result
            if current.mode != 'online':
                result['state'] = 'charging' if current.mode == 'charging' else 'discharging'
                if not result['observed']:
                    result['reason'] = 'not_observed'
                return result
            if not result['observed']:
                result.update(state='observing', reason='not_observed')
                return result

            standby_duration = current.timestamp - self._standby_start
            candidate_duration = current.timestamp - self._candidate_since
            unique_lows = Counter(lowest[0] for _, _, lowest in self._recent if len(lowest) == 1)
            frequent = None
            if unique_lows:
                leaders = [cell for cell, count in unique_lows.items() if count == max(unique_lows.values())]
                if len(leaders) == 1:
                    frequent = leaders[0]
            result.update(
                standby_duration_sec=standby_duration,
                candidate_level=self._candidate_level, candidate_duration_sec=candidate_duration,
                frequent_lowest_cell=frequent,
                recent_max_delta_mv=max(delta for _, delta, _ in self._recent),
                recent_sample_count=len(self._recent),
            )
            if standby_duration < REQUIRED_STANDBY_SEC:
                result.update(state='settling', reason='waiting_standby')
            elif candidate_duration < PERSISTENCE_SEC:
                result.update(state='observing', reason='confirming_level')
            else:
                result.update(state='assessed', level=self._candidate_level)
            return result
