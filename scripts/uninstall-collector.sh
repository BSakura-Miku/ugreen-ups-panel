#!/bin/sh
set -eu
[ "$(id -u)" = 0 ] || { echo 'Run as root'; exit 1; }
systemctl disable --now ugreen-ups-collector.service
rm -f /etc/systemd/system/ugreen-ups-collector.service /etc/tmpfiles.d/ugreen-ups-panel.conf
systemctl daemon-reload
if [ "$(cat /opt/ugreen-ups-panel/module-initial-state 2>/dev/null || true)" = absent ]; then
    /sbin/modprobe -r usbmon || echo 'usbmon still in use; left loaded.'
fi
echo 'Collector stopped. Releases, snapshot and Docker history retained. NUT unchanged.'
