# ha-mobius

A Home Assistant custom integration for "Mobius"-protocol aquarium
equipment (EcoTech Marine VorTech/Radion, AquaIllumination, NYOS Quantum,
etc.), built on [`python-mobius`](https://pypi.org/project/python-mobius/).

Not affiliated with or endorsed by any of these companies. See
`python-mobius`'s own documentation for the full protocol writeup this is
built on.

## Status: read-only

This integration currently does **discovery and status reporting only** —
no control (starting scenes, changing schedules) yet. That's a deliberate
choice, not a limitation of the underlying library: control support is
being kept in lockstep with `python-mobius` itself as it grows write
capabilities, rather than getting ahead of it.

### Development / running the tests

```bash
git clone https://code.r3pek.org/r3pek/ha-mobius
cd ha-mobius
pip install -r requirements_test.txt
pip install python-mobius bleak-retry-connector
pytest tests/
```

`requirements_test.txt` pins the exact `pytest-homeassistant-custom-component`
version this integration's own CI is confirmed to pass against (see that
file's own comments for why); CI (`.forgejo/workflows/test.yml`) additionally
reads `custom_components/mobius/manifest.json`'s own `requirements` at
test time, so runtime dependencies never have to be duplicated by hand.

## What it does

- **Autodiscovery**: Home Assistant's own Bluetooth integration triggers
  setup automatically when it sees a device advertising as `MOBIUS` (or
  matching the confirmed manufacturer ID `0x0202`) — no manual scanning
  needed. Manual setup (picking from already-seen-but-unconfigured devices)
  is also available via *Settings → Devices & Services → Add Integration*.
  A device split across multiple advertisement packets that don't all
  carry the identifying data (confirmed real, not hypothetical) is still
  found once the fuller packet arrives, rather than being silently missed.
- **Multi-device tanks**: several devices sharing one Thread mesh are set
  up as a single tank, not one entry per device — one device relays for
  the others rather than each opening its own connection.
- **One Home Assistant device per physical device**, with model,
  manufacturer, serial number, and firmware version ("Product OS" —
  confirmed via real hardware to be the most meaningful single "what
  firmware is this" answer, ported from the app's own display labels;
  see `python-mobius`'s `get_firmware_versions()`) populated in the
  device registry.
- **Sensors** (all read-only, diagnostic entities unless noted):
  - Every device: support tier, error state, schedule point count, mesh
    address (this device's own Thread mesh-local IPv6 address, with the
    time it was last heard from on the mesh as an attribute), firmware
    version (full per-component breakdown as attributes), hardware
    revision (full breakdown as attributes).
  - Pumps (all main entities, not diagnostic): operation state, motor
    speed, estimated flow, current pump mode.
  - Lights: one intensity sensor per channel (main entities, not
    diagnostic), reflecting the same client-side schedule interpolation
    `python-mobius` replicates from the official app — not a live device
    read (there isn't one; see `python-mobius`'s docs on why). Also a
    diagnostic calibration sensor (completed True/False, plus
    last-calibration-date as an attribute) — confirmed via real hardware
    and the app's own UI gating to be light-specific; not added for
    pumps, which don't support it.
  - Per tank (a synthetic device, not any one physical one): which device
    currently holds the gateway role, and the mesh's own shared prefix.
- **Debug logging**: enabling it (Settings → Devices & Services → Mobius
  → Enable debug logging) surfaces connection attempts, gateway elections
  and failovers, and mesh scans in detail — built specifically to make a
  real "why isn't this connecting" report diagnosable from the logs
  alone, not something to enable blind and hope helps.
- **Diagnostics download** (entry's own three-dot menu → Download
  diagnostics) includes registry/coordinator state per device, and,
  critically, whether Home Assistant's own Bluetooth stack currently sees
  each device at all — independent of what this integration itself has
  cached — to tell "our own cached state is stale" apart from "Home
  Assistant hasn't seen this device recently" when nothing's connecting.

## Polling design

Each device is polled every `POLL_INTERVAL` (30s) — the gateway's own
direct read, or a relayed read through it, depending on role. A separate,
much less frequent per-tank task handles maintenance that doesn't need to
happen every poll cycle.

## Install

### Manual (works today, no GitHub/HACS needed)

Copy `custom_components/mobius/` into your Home Assistant `config/custom_components/`
directory, then restart Home Assistant.

### HACS

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=r3pek&repository=ha-mobius&category=integration)

Or manually: in HACS, Settings → Custom repositories → add
`https://github.com/r3pek/ha-mobius` (type: Integration).

## Dependencies

`python-mobius` and `bleak-retry-connector` are declared in `manifest.json`
and installed automatically by Home Assistant when the integration loads —
no separate `pip install` needed.

## License

GPLv2 — see [`LICENSE`](./LICENSE).
