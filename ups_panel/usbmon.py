"""Linux usbmon binary reader. Never opens the UPS USB device or claims interfaces."""
import ctypes as C
import errno
import os
from pathlib import Path
import select
import struct


def discover(root: Path = Path('/sys/bus/usb/devices'), serial: str = ''):
    matches = []
    for path in root.iterdir():
        try:
            if (path / 'idVendor').read_text().strip().lower() != '2b89':
                continue
            if (path / 'idProduct').read_text().strip().lower() != 'ffff':
                continue
            actual_serial = (path / 'serial').read_text().strip() if (path / 'serial').exists() else ''
            if serial and serial != actual_serial:
                continue
            matches.append({'bus': int((path / 'busnum').read_text()),
                            'device': int((path / 'devnum').read_text()),
                            'serial': actual_serial, 'path': path.name})
        except (OSError, ValueError):
            continue
    if len(matches) > 1:
        raise RuntimeError('Multiple US3000 devices: configure UPS_SERIAL')
    return matches[0] if matches else None


def classify_event(header: bytes, payload: bytes, bus: int, device: int):
    """Classify one event without retaining payloads or naming unrelated devices.

    Acceptance is deliberately shared with ``decode_event``. Diagnostic categories
    must never relax the production filter, including cancelled/truncated transfers.
    Short headers cannot safely be attributed to the target and are not activity.
    """
    if len(header) != 64:
        return 'invalid_header'
    if header[11] != device or struct.unpack_from('=H', header, 12)[0] != bus:
        return 'unrelated'
    if header[8] != ord('C'):
        return 'not_completion'
    if header[9] != 1 or header[10] != 0x81:
        return 'not_interrupt_in'
    status, length, captured = struct.unpack_from('=iII', header, 28)
    if status not in (0, -2):
        return 'invalid_status'
    if header[15] != 0:
        return 'missing_payload'
    if (length != captured or captured != len(payload)
            or not 64 <= length <= 4096 or length % 64):
        return 'invalid_length'
    if any(payload[i] != 0x71 for i in range(0, length, 64)):
        return 'invalid_report_id'
    return 'accepted'


def decode_event(header: bytes, payload: bytes, bus: int, device: int):
    if classify_event(header, payload, bus, device) != 'accepted':
        return None
    status = struct.unpack_from('=i', header, 28)[0]
    sec = struct.unpack_from('=q', header, 16)[0]
    usec = struct.unpack_from('=i', header, 24)[0]
    # NUT requests 512 bytes and can accumulate several reports before cancellation.
    # Publish only the newest complete report; do not invent timestamps for older ones.
    return {'frame': payload[-64:], 'timestamp': sec + usec / 1e6, 'usb_status': status}


class GetArg(C.Structure):
    _fields_ = [('header', C.c_void_p), ('data', C.c_void_p), ('allocated', C.c_size_t)]


class Reader:
    def __init__(self, bus):
        self.fd = os.open(f'/dev/usbmon{bus}', os.O_RDONLY | os.O_NONBLOCK)
        self.lib = C.CDLL(None, use_errno=True)
        self.header = C.create_string_buffer(64)
        self.data = C.create_string_buffer(4096)
        self.arg = GetArg(C.addressof(self.header), C.addressof(self.data), 4096)

    def read(self, timeout=1):
        if not select.select([self.fd], [], [], timeout)[0]:
            return None
        command = 0x40000000 | (C.sizeof(GetArg) << 16) | (0x92 << 8) | 10
        if self.lib.ioctl(self.fd, command, C.byref(self.arg)) < 0:
            err = C.get_errno()
            if err == errno.EAGAIN:
                return None
            raise OSError(err, os.strerror(err))
        captured = struct.unpack_from('=I', self.header.raw, 36)[0]
        return self.header.raw, self.data.raw[:min(captured, 4096)]

    def stats(self):
        stats = C.create_string_buffer(8)
        if self.lib.ioctl(self.fd, 0x80089203, stats) < 0:
            raise OSError(C.get_errno(), 'usbmon stats failed')
        return struct.unpack('=II', stats.raw)

    def close(self):
        os.close(self.fd)
