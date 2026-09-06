#!/usr/bin/env python3
"""Bounded, target-only passive capture for electrical field investigation.

Requires usbmon already loaded. Does not modify modules, NUT, loads or UPS state.
Unmapped words are recorded as raw integers, never relabeled as amps/watts.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ups_panel.usbmon import Reader, decode_event, discover
from ups_panel.protocol import parse_frame


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=30, choices=range(5, 121), metavar='5..120')
    parser.add_argument('--output', required=True)
    parser.add_argument('--serial', default=os.getenv('UPS_SERIAL', ''))
    args = parser.parse_args()
    device = discover(serial=args.serial)
    if not device:
        parser.error('Target US3000 not found')
    # Exclusive creation prevents accidental overwrite of earlier evidence.
    with open(args.output, 'x') as output:
        reader = Reader(device['bus'])
        start = time.monotonic()
        try:
            while time.monotonic() - start < args.seconds:
                packet = reader.read(.5)
                if not packet:
                    continue
                event = decode_event(*packet, device['bus'], device['device'])
                if not event:
                    continue
                frame = event['frame']
                try:
                    parsed = parse_frame(**event)
                except ValueError as exc:
                    parsed = {'error': str(exc)}
                record = {'timestamp': event['timestamp'], 'raw_hex': frame.hex(),
                          'usb_status': event['usb_status'], 'decoded': parsed,
                          'raw_be_u16': {str(i): int.from_bytes(frame[i:i + 2], 'big')
                                         for i in (16, 18, 20, 22, 24, 26, 29, 31, 33)}}
                output.write(json.dumps(record, ensure_ascii=False) + '\n')
                output.flush()
        finally:
            reader.close()


if __name__ == '__main__':
    main()
