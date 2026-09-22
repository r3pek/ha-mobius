# ha-mobius

A Home Assistant integration for "Mobius" aquarium devices: EcoTech Marine
(VorTech and Vectra pumps, Radion lights), AquaIllumination (Prime, Hydra,
Orbit, Axis), Neptune Systems and NYOS. Built on
[`python-mobius`](https://code.r3pek.org/r3pek/python-mobius), whose
documentation describes the protocol.

Not affiliated with or endorsed by any of these companies.

## Features

- **Automatic discovery** through Home Assistant's Bluetooth integration,
  or manual setup from *Settings → Devices & services → Add integration*.
- **Tanks**: the devices of one tank are added together, under one tank
  device, and share a single Bluetooth connection: one device (the
  gateway) is connected and the others are reached through it. The
  gateway moves to another device automatically when it fails. New
  devices of a configured tank are added to it automatically.
- **Lights**: current intensity per channel, schedule intensity, lunar
  phases, calibration status.
- **Pumps**: speed, flow (only when the pump reports it reliably),
  operation state and current mode; battery backup settings and whether
  the pump is running on battery.
- **Scenes**: start a scene on the whole tank or return to the schedule.
- **Settings**: Local control, LED auto-dim, max fan speed and fan
  shutdown, where the device supports them.
- **Dashboard cards** for editing schedules and starting scenes.
- **Maintenance**: reboot buttons, hourly clock sync, diagnostics
  download.

## Entities

| Entity | Where | |
|---|---|---|
| Channel intensity sensors, Schedule intensity | Lights | |
| Calibration | Lights that report it | Diagnostic |
| Lunar phases switch | Lights that support it | |
| Motor speed, Estimated flow, Operation state, Current mode | Pumps | Estimated flow only when reliable |
| Battery backup speed; Running on battery | Pumps with battery backup | |
| Boosted battery power, on time, off time | Pumps that support them | Only changeable while running on battery |
| Local control, Fan shutdown switches; LED auto-dim timeout, Max fan speed selects | Devices that support them | |
| Support tier, Error state, Schedule points, Firmware version, Hardware revision, Mesh address, Configured scenes | Every device | Diagnostic |
| Reboot button | Every device | |
| Scene selection | Tank | |
| Restart all devices button, Time sync switch, Poll interval (10-300 s) | Tank | |
| Gateway device, Mesh prefix | Tanks with several devices | Diagnostic |

## Dashboard cards

The cards are installed and registered with the integration (dashboards
in storage mode). Add them from the card picker or in YAML:

```yaml
type: custom:mobius-schedule-card
device_id: <a light or pump device, not the tank>
```

Shows a light's channel history, intensity slider and lunar toggle, or a
pump's flow and current mode, and opens a schedule editor: add, change
and delete points, load and download the app's `.mob` files, and save to
the device. Lights that share a schedule group are written together.

```yaml
type: custom:mobius-scene-card
entity: select.<tank>_scene_selection
```

Tiles for the tank's scenes, with the remaining time of the active one.

## Services

| Service | Does |
|---|---|
| `mobius.write_schedule_group` | Writes a schedule to a device and every light in its schedule group |
| `mobius.set_schedule_intensity` | Sets the schedule intensity (0-100 %) of a light and its group |

## Install

### HACS

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=r3pek&repository=ha-mobius&category=integration)

Or in HACS: *Custom repositories* → add
`https://github.com/r3pek/ha-mobius` (type: Integration).

### Manual

Download `mobius.zip` from a release, extract it into
`config/custom_components/mobius/` and restart Home Assistant. Copying the
repository's `custom_components/mobius/` works too, but the cards then
need to be built first (see [`frontend-src/`](./frontend-src/README.md)).

Requires Home Assistant 2026.7.0 or newer. `python-mobius` and
`bleak-retry-connector` are installed automatically.

## Bluetooth proxy hardware

Every Mobius device needs to be within Bluetooth range of *something* Home
Assistant can talk to — either the HA host's own adapter, or an [ESPHome
Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html). A tank
is rarely right next to the server, so a proxy placed near the tank itself
is the practical way to get reliable connections.

Hardware known to work (one option among many):

- [ESP32-S3 DevKitC-1 N16R8 development
  board](https://amzn.to/4gOtYEE): one per tank or area is a good start;
  more give better coverage and more gateway candidates.
- [Waterproof junction box](https://amzn.to/4qtQKF6): reef tanks are
  humid, so house the board in something water-resistant.

(Amazon affiliate links)

An example ESPHome configuration for the ESP32-S3 board as a Bluetooth
proxy is in [`esphome/mobius-bt-proxy.yaml`](./esphome/mobius-bt-proxy.yaml).
The important setting is `bluetooth_proxy: active: true`: the integration
needs real connections to the devices, and active scanning to read their
serial numbers, so a passive-only proxy isn't enough.

## Troubleshooting

- **"No unconfigured Mobius devices found" or "serial number couldn't be
  read"**: the serial number is only received while scanning actively.
  Set at least one Bluetooth adapter or proxy near the tank to the
  "Active" or "Auto" scanning mode.
- **Debug logging**: *Settings → Devices & services → Mobius → Enable
  debug logging* logs connections, gateway changes and mesh scans.
- **Diagnostics**: the entry's menu → *Download diagnostics* includes the
  gateway state and whether Home Assistant currently sees each device over
  Bluetooth. Attach it to bug reports.

## Development

```bash
git clone https://code.r3pek.org/r3pek/ha-mobius
cd ha-mobius
pip install -r requirements_test.txt
pip install python-mobius bleak-retry-connector
pytest tests/

cd frontend-src && npm install && npm test   # cards
```

CI (`.forgejo/workflows/test.yml`) installs the runtime requirements from
`manifest.json`.

## License

GPLv2, see [`LICENSE`](./LICENSE).
