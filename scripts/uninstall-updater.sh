#!/bin/sh
set -eu
exec /usr/bin/python3 "$(dirname "$0")/updater-admin.py" uninstall "$@"
