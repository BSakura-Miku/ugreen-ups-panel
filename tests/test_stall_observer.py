import json
import pytest
from ups_panel.stall_observer import EvidenceWindow, reasons
from ups_panel.update_client import UpdateClient, UpdateError


def row(t=100, **extra):
    return dict(wall=t, mono=t, heartbeat=t, sample_time=t, collector={'pid': 1, 'start_ticks': 2}, **extra)


def test_evidence_distinguishes_clock_snapshot_usb_and_writer():
    assert reasons(row()) == []
    current = row(111, api={'last_success': 98}, usb={'accepted_mono': 97})
    current['sample_time'] = 98
    assert set(reasons(current, row())) == {'sample_time_stale', 'writer_stale', 'usb_completion_gap'}
    current = row(90)
    current['mono'] = 101
    assert reasons(current, row()) == ['wall_clock_changed']


def test_bounded_triggered_evidence_keeps_before_and_after_and_retention(tmp_path):
    window = EvidenceWindow(tmp_path, before=2, after=1, cooldown=0, max_files=2)
    for t in range(100, 104):
        window.ingest(row(t))
    assert not list(tmp_path.iterdir())
    for t in range(104, 110):
        current = row(t)
        current['heartbeat'] = 1
        window.ingest(current)
    files = list(tmp_path.glob('stall-*.json'))
    assert len(files) == 2
    for file in files:
        data = json.loads(file.read_text())
        assert len(data['rows']) == 4
        assert file.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('error,reason,availability', [
    (FileNotFoundError, 'endpoint_missing', 'not_installed'),
    (PermissionError, 'permission_denied', 'unreachable'),
    (ConnectionRefusedError, 'connection_refused', 'unreachable'),
    (TimeoutError, 'timeout', 'unreachable'),
    (OSError, 'connection_failed', 'unreachable'),
])
def test_connection_errors_are_sanitized_and_do_not_claim_host_absence(monkeypatch, error, reason, availability):
    def fail(*args):
        raise error('private path and credentials')
    monkeypatch.setattr('ups_panel.update_client.socket.socket', fail)
    result = UpdateClient().request()
    assert result['connection_reason'] == reason
    assert result['availability'] == availability
    assert result['installation_status'] == 'unknown'
    assert 'private' not in json.dumps(result)
    with pytest.raises(UpdateError, match='宿主更新服务'):
        UpdateClient().request('check')
