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
- Reference cell-voltage-difference levels after stable standby, with recent low-cell observations and guidance.
- Individual battery-power records with start/end times, observed duration, start/end charge, occurrence counts, and net charge decrease.
- Estimated discharge energy in Wh within observed intervals, with coverage and power provenance.
- A step-by-step assistant using manually entered AC readings, with confirmed 12/19/20 V adapter input and editable coefficients.
- Hardware references, NUT status, and capture diagnostics.

Trend ranges include 1 hour, 24 hours, 7 days, 30 days, 90 days, half a year (180 days), and one year (365 days). Ranges longer than 90 days use daily aggregates. Charts keep the entire selected time range, leaving unrecorded periods and gaps empty; a few days of data remain a few days of data.

Battery-power records begin with valid sampling after first enabling v0.5.0 or later; older events and aggregates are not converted into session details. The default range is 90 days, with choices from 7 days to one year. Missing transitions or interrupted capture produce incomplete records. Net charge decrease is measured in percentage points and may be negative when charge readings rise; it is not energy in Wh or a battery cycle count.

In v0.7.0, reference levels require 30 minutes of continuous standby and 2 minutes in the current voltage-difference band. These are project guidance, not manufacturer health limits. Estimated discharge energy starts with valid samples observed by the new version and requires a configured battery gain; older records are not backfilled. Neither feature reports full battery capacity or SOH. See [battery observations](docs/battery-observation.md).

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

The default [`docker-compose.yaml`](docker-compose.yaml) sets `panel.user` to `"0:0"` for compatibility with data-directory permissions on some NAS systems. See [container user and data-directory permissions](#container-user-and-data-directory-permissions) for the reason, implications, and non-root alternative.

Open `http://NAS_IP:9086`, replacing `NAS_IP` with your NAS's LAN address.

Successful installation starts the collector and enables it at boot.

## Update

**With a v0.6.0 collector already installed, v0.7.0 only needs a dashboard update.** Follow the [backup instructions](docs/architecture.md#备份与维护) first. Replace the paths below with your existing deployment path and keep the original Compose project name; add `-p original-project-name` consistently if you previously set a custom name.

```sh
sudo docker compose -f /volume1/docker/ugreen-ups-panel/docker-compose.yaml pull
sudo docker compose -f /volume1/docker/ugreen-ups-panel/docker-compose.yaml up -d
```

For a collector older than v0.6.0, use the compatibility upgrade below to update the collector before the dashboard, providing the calibration assistant and complete power provenance. Temporary source files are removed afterward; existing `data` and `docker-compose.yaml` are retained.

```sh
(
  set -eu
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "$tmp_dir"' EXIT
  curl -fL https://codeload.github.com/BSakura-Miku/ugreen-ups-panel/tar.gz/refs/tags/v0.6.0 -o "$tmp_dir/source.tar.gz"
  tar -xzf "$tmp_dir/source.tar.gz" --strip-components=1 -C "$tmp_dir"
  sudo sh "$tmp_dir/scripts/install-collector.sh" --data-dir /volume1/docker/ugreen-ups-panel/data
  sudo docker compose -f /volume1/docker/ugreen-ups-panel/docker-compose.yaml pull
  sudo docker compose -f /volume1/docker/ugreen-ups-panel/docker-compose.yaml up -d
)
```

The default image is `bsakuramiku/ugreen-ups-panel:latest`; see [release notes](https://github.com/BSakura-Miku/ugreen-ups-panel/releases). Older collectors cannot read the new calibration format. A rollback also requires a compatible configuration; see [calibration upgrades and rollback](docs/calibration.md#升级与回滚).

## Web power calibration

Open **功率校准** to view the active configuration, coefficients, and formulas. The assistant offers these steps:

1. **Confirm the adapter voltage.** The page suggests `12/19/20 V` from the adapter input reading, but requires your confirmation. UPS output voltage is not used for this selection.
2. **Calibrate the AC baseline.** With stable external power and no charging, collect 30 seconds of telemetry, then enter the matching AC reading in watts from your smart plug or power meter. This step can be saved on its own to enable estimates while not charging.
3. **Add charging compensation later.** When the UPS naturally enters charging mode, collect another window and enter its matching AC reading. Leave uncalibrated charging and battery coefficients empty.

Each window needs at least 12 distinct readings. Missing data, mode changes, or more than 10% variation in relevant raw readings require another window. Save the calculated settings and wait for collector confirmation. AC readings are entered manually; the assistant does not connect to HA, switch the plug, or initiate a power interruption.

The default remains `none`; `local-19v-v1` and `custom` are also available. The development preset retains its original 19 V scope and cannot be used for 12 V. Custom AC estimates require input within ±1 V of the selected voltage and use 8-second smoothing. They always remain independently unverified. See [power calibration](docs/calibration.md) for coefficient limits, partial configurations, and model evidence.

## Configuration and data

- The default port is `9086`. To change the port or image version, edit `docker-compose.yaml`, then run `docker compose up -d`.
- Compose automatically reads `docker-compose.yaml`; the top-level `name` field is optional. By default, the project name comes from the deployment directory, so the steps above use `ugreen-ups-panel`.
- History lives in `./data/history.sqlite`, and web calibration settings live in `./data/calibration.json`. Both survive container recreation. Keep and back up the whole `data` directory.
- History retains 10-second aggregates for 7 days, minute aggregates for 90 days, and daily aggregates for 365 days. Upgrading builds daily aggregates from the older records still present in the database; previously expired and deleted records cannot be recovered.
- The container reads host snapshots through a read-only mount. The collector runs alongside the existing UPS service.

See [architecture](docs/architecture.md) for data flow, history storage, and backup details.

<a id="non-root"></a>

## Container user and data-directory permissions

The default Compose file sets `user: "0:0"` under `panel`, running the dashboard as root inside the container to accommodate bind-mounted `data` directories and existing SQLite files on some NAS systems. Unwritable directories or files can cause SQLite “unable to open database” or read-only errors; the dashboard itself does not require root, and this default addresses write access to the actual data directory.

The image itself still defaults to `10001:10001`; Compose's [`user`](https://docs.docker.com/reference/compose-file/services/#user) overrides only the dashboard container's process user. Collector snapshots remain mounted read-only. The dashboard needs no `privileged` mode, USB device mapping, or `docker.sock` mount. Root has greater ability to modify writable mounted files, so a bad mount configuration can affect host files and other services. It is not an absolute safety guarantee; keep mounts narrowly scoped. See [Docker's bind-mount considerations](https://docs.docker.com/engine/storage/bind-mounts/#considerations-and-constraints) and [security notes](SECURITY.md).

All Compose commands in this section must use the original deployment's project name. If the UGOS GUI or `-p` set a name different from the directory name, consistently use `sudo docker compose -p original-project-name ...`, including for `config`, `stop`, and `up`, so these operations target the original dashboard.

To adopt this default in an existing deployment, add `user: "0:0"` under `panel` in the Compose file actually used by that deployment, then run `sudo docker compose up -d --force-recreate panel` from that project directory. Pulling an image or restarting the old container alone does not change its user.

Running as non-root is an alternative. **Identify the actual current data directory and back up the existing data first**; do not replace the database with a new empty directory. From the existing project directory, run:

```sh
cd /volume1/docker/ugreen-ups-panel
sudo docker compose config
```

Replace the example path with your deployment path and inspect the `source` for `target: /data`. Compose resolves `./data` relative to the Compose file's directory; it must refer to the same actual directory as `ups_panel_data_dir` below. Adjust that variable if you use a custom mount. Then change `panel`'s `user` in your existing Compose file to `"10001:10001"`, or remove the override to use the image's default user.

Next, stop the dashboard, adjust only this directory and the four named files, and recreate the dashboard. Before stopping the dashboard or changing permissions, the commands check that the directory is not a symbolic link and each existing named file is a regular file without a symbolic link. If a check fails, verify the actual target and file type first. Missing files are skipped, and the database, WAL, and calibration configuration are preserved.

```sh
(
  set -eu
  cd /volume1/docker/ugreen-ups-panel
  ups_panel_data_dir=/volume1/docker/ugreen-ups-panel/data
  sudo test -d "$ups_panel_data_dir"
  sudo test ! -L "$ups_panel_data_dir"
  for ups_panel_file in history.sqlite history.sqlite-wal history.sqlite-shm calibration.json; do
    sudo test ! -L "$ups_panel_data_dir/$ups_panel_file"
    if sudo test -e "$ups_panel_data_dir/$ups_panel_file"; then
      sudo test -f "$ups_panel_data_dir/$ups_panel_file"
    fi
  done
  sudo docker compose stop panel
  sudo chown 10001:10001 "$ups_panel_data_dir"
  sudo chmod 0750 "$ups_panel_data_dir"
  for ups_panel_file in history.sqlite history.sqlite-wal history.sqlite-shm calibration.json; do
    if sudo test -f "$ups_panel_data_dir/$ups_panel_file"; then
      sudo chown 10001:10001 "$ups_panel_data_dir/$ups_panel_file"
      sudo chmod 0640 "$ups_panel_data_dir/$ups_panel_file"
    fi
  done
  sudo docker compose up -d --force-recreate panel
)
```

The directory gets mode `0750`; existing `history.sqlite`, `history.sqlite-wal`, `history.sqlite-shm`, and `calibration.json` get `0640`. All are owned by `10001:10001`. Do not use `chmod 777` or recursively change ownership or permissions across a NAS shared folder. If NAS ACLs impose additional restrictions, ensure this UID/GID can access the actual data directory; these mode changes do not replace ACL configuration.

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
