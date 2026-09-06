# US3000 Power Monitor

[简体中文](README.md) · **English**

<p align="center"><img src="frontend/src/assets/us3000-logo.png" width="80" height="80" alt="US3000 Monitor logo" /></p>

![US3000 dashboard preview with demonstration data](docs/assets/dashboard-demo.png)

*The preview uses DEMO data, with no real NAS telemetry or serial numbers. The UI and detailed documentation are primarily in Simplified Chinese.*

A UGREEN US3000 dashboard for power state, battery charge, cell voltages, and historical trends. A host collector uses Linux usbmon to observe existing UPS-driver traffic without writing to the UPS. Docker Compose runs the web dashboard.

**The NAS's existing UPS service remains responsible for power-loss protection and shutdown.** This community monitoring project is not affiliated with or certified by UGREEN.

## Features

- External power, charging, and battery power states.
- Charge percentage, input/output voltage, pack voltage, four cell voltages, and cell voltage difference.
- Up to one year of history, power and connection events, and CSV export with averages and extrema.
- Mean and peak cell voltage difference, with the actual dates covered by recorded data.
- Individual battery-power records with start/end times, observed duration, start/end charge, occurrence counts, and net charge decrease.
- Web controls for calibration profiles, exact coefficients, formulas, and custom coefficients.
- Hardware references, NUT status, and capture diagnostics.

Trend ranges include 1 hour, 24 hours, 7 days, 30 days, 90 days, half a year (180 days), and one year (365 days). Ranges longer than 90 days use daily aggregates. Charts keep the entire selected time range, leaving unrecorded periods and gaps empty; a few days of data remain a few days of data.

Battery-power records begin with the first valid sample after upgrading; older events and aggregates are not converted into session details. The default range is 90 days, with choices from 7 days to one year. Missing transitions or interrupted capture produce incomplete records. Net charge decrease is measured in percentage points and may be negative when charge readings rise; it is not energy in Wh or a battery cycle count.

Power fields still have protocol and measurement-location limitations. The optional empirical model is disabled by default (calibration profile `none`), so coefficients from the development unit are not applied. See [power calibration](docs/calibration.md).

## Prerequisites

- A Linux NAS connected to the UPS, with systemd, Python 3.10+, curl, tar, Git, Docker, and Compose v2.
- A kernel with usbmon support and working `/dev/usbmonN` devices.
- An existing NUT/system UPS driver connected to the US3000 and continuously reading complete private `0x71` reports.

Images support `linux/amd64` and `linux/arm64`. Real hardware validation covers one x86_64 NAS and US3000; ARM validation covers container operation only.

The dashboard and API have no built-in authentication. Use them only on a trusted LAN.

## Install

On the **Linux NAS connected to the UPS**, choose a directory on persistent storage and run:

```sh
git clone https://github.com/BSakura-Miku/ugreen-ups-panel.git
cd ugreen-ups-panel
sudo sh scripts/install-collector.sh && docker compose up -d
```

The installer sets up the host collector and prepares the data directory. Compose automatically pulls the [Docker Hub image](https://hub.docker.com/r/bsakuramiku/ugreen-ups-panel); no local build is required.

Open `http://NAS_IP:9086`, replacing `NAS_IP` with your NAS's LAN address.

The collector runs automatically after installation. Upgrading to v0.5.0 with an existing v0.4.0 collector requires only a dashboard container update; no collector reinstall is needed.

## Update

**v0.5.0 supports the existing v0.4.0 collector.** Run from the project directory:

```sh
docker compose pull && docker compose up -d
```

The default image is `bsakuramiku/ugreen-ups-panel:latest`. Check [release notes](https://github.com/BSakura-Miku/ugreen-ups-panel/releases) for changes.

**If the collector is still v0.3.x or earlier, update it once** before calibration saved in the dashboard can take effect. Replace `/volume1/docker/ugreen-ups-panel` below with your existing project path. Source files are downloaded into a temporary directory and removed afterward; your existing `data` and `docker-compose.yaml` are retained.

```sh
(
  set -eu
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "$tmp_dir"' EXIT
  curl -fL https://codeload.github.com/BSakura-Miku/ugreen-ups-panel/tar.gz/refs/tags/v0.4.0 -o "$tmp_dir/source.tar.gz"
  tar -xzf "$tmp_dir/source.tar.gz" --strip-components=1 -C "$tmp_dir"
  sudo sh "$tmp_dir/scripts/install-collector.sh" --data-dir /volume1/docker/ugreen-ups-panel/data
  sudo docker compose -f /volume1/docker/ugreen-ups-panel/docker-compose.yaml pull
  sudo docker compose -f /volume1/docker/ugreen-ups-panel/docker-compose.yaml up -d
)
```

## Web power calibration

Open **功率校准** in the dashboard to view the active profile, three exact coefficients, and formulas. Choose `none` (estimates disabled), `local-19v-v1` (the development unit's profile), or `custom`. The default remains `none`.

For custom coefficients, `base_gain` and `battery_gain` must be greater than `0` and at most `10`; `charge_gain` accepts `0`–`10`. These are input limits, not evidence of accuracy. **Custom profiles always remain independently unverified.**

The dashboard distinguishes saved settings from active settings. A configuration is active only after the collector produces fresh telemetry using it. Settings live in the existing `./data/calibration.json`; no new Compose settings are needed. Raw readings and earlier history are retained, with different calibration revisions aggregated separately. The model keeps the 18–20 V AC-input constraint and 8-second smoothing window. See [power calibration](docs/calibration.md).

## Configuration and data

- The default port is `9086`. To change the port or image version, edit `docker-compose.yaml`, then run `docker compose up -d`.
- Compose automatically reads `docker-compose.yaml`; the top-level `name` field is optional. By default, the project name comes from the deployment directory, so the steps above use `ugreen-ups-panel`.
- History lives in `./data/history.sqlite`, and web calibration settings live in `./data/calibration.json`. Both survive container recreation. Keep and back up the whole `data` directory.
- History retains 10-second aggregates for 7 days, minute aggregates for 90 days, and daily aggregates for 365 days. Upgrading builds daily aggregates from the older records still present in the database; previously expired and deleted records cannot be recovered.
- The container reads host snapshots through a read-only mount. The collector runs alongside the existing UPS service.

See [architecture](docs/architecture.md) for data flow, history storage, and backup details.

## If there is no data

Check the collector and dashboard logs:

```sh
journalctl -u ugreen-ups-collector.service -n 50 --no-pager
docker compose logs --tail 50
```

NUT displaying a charge percentage does not prove that the driver reads the complete reports this dashboard needs. See [validation scope](docs/validation.md) for requirements and known limitations.

## More documentation

- [Architecture and data flow](docs/architecture.md) · [Protocol fields](docs/fields.md) · [Power calibration](docs/calibration.md)
- [Hardware reference](docs/hardware.md) · [Validation scope](docs/validation.md) · [Changelog](CHANGELOG.md)
- [Development and contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

## References and acknowledgments

Thanks to [cktk/ugreen-ups](https://github.com/cktk/ugreen-ups) for sharing its US3000 USB HID protocol research and telemetry field documentation, which provided the starting point for this project's protocol investigation. Field decoding was then checked against captures from a DXP4800 Plus and US3000.

The host capture interface follows the [Linux usbmon documentation](https://docs.kernel.org/usb/usbmon.html).

## License

Original source code uses the [MIT License](LICENSE). Third-party dependencies retain their own terms; see [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt). Product names, trademarks, and third-party media belong to their respective owners.
