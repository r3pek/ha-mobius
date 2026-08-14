# ha-mobius

A Home Assistant custom integration for "Mobius"-protocol aquarium
equipment (EcoTech Marine VorTech/Radion, AquaIllumination, NYOS Quantum,
etc.), built on [`python-mobius`](https://pypi.org/project/python-mobius/).

Not affiliated with or endorsed by any of these companies. See
`python-mobius`'s own documentation for the full protocol writeup this is
built on.

## Status: read-only, running against real hardware

This integration currently does **discovery and status reporting only** —
no control (starting scenes, changing schedules) yet. That's a deliberate
choice, not a limitation of the underlying library: control support is
being kept in lockstep with `python-mobius` itself as it grows write
capabilities, rather than getting ahead of it.

**Tested against real hardware, not just written against docs.** Beyond
the test suite described below, this integration has been running against
actual, multi-device VorTech/Radion tanks in production — several real
production issues (gateway failover getting stuck between the same two
devices, a gateway persistently failing to relay to one specific target
while everything else on the tank kept working, a device going missing
from Home Assistant's own Bluetooth cache for hours) were found and fixed
from real, live logs, not simulated scenarios.

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
  up as a single tank, not one entry per device. See "Tank / gateway
  architecture" below for how they actually talk to each other.
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

## Tank / gateway architecture

Multiple devices on the same physical Thread mesh don't each open their
own BLE connection. One device is elected **gateway** (by RSSI at
first setup, or when explicitly preferred for a device already confirmed
directly reachable) and holds the single real BLE connection for the
whole group; every other device's own data is **relayed** through it
over the mesh, addressed by that device's own Thread mesh-local address
rather than a second BLE connection.

**Two, deliberately separate, automatic failover mechanisms:**

- If the *gateway's own* direct reads fail `GATEWAY_FAILURE_THRESHOLD`
  (3) consecutive times, a different member is promoted automatically,
  cycling through every member before any one of them is reconsidered —
  not just ping-ponging between the two best-signal candidates forever,
  which is a real, confirmed bug this replaced.
- If the gateway's own reads are fine, but it persistently fails to
  *relay* to one specific other member `RELAY_FAILURE_THRESHOLD` (3)
  consecutive times, a different gateway is also tried — a genuinely
  different, separately-tracked symptom from the gateway's own health,
  confirmed real: a gateway can be perfectly healthy for itself, and for
  relaying to some other members, while persistently failing to reach one
  specific target for a long stretch.

A dedicated, periodic per-tank task (every `TANK_REVALIDATION_INTERVAL`,
1 minute) also: recovers a tank left with no gateway at all, refreshes
every known member's own mesh address and last-seen data opportunistically,
detects a device that's physically moved to a different, already-known
tank and migrates it automatically, and proactively checks whether the
current gateway is still visible in Home Assistant's own Bluetooth cache
(when its connection isn't already open) — requesting a one-shot active
scan if not, rather than only finding out reactively once a poll actually
fails to connect.

Connection *attempts* across the whole integration are throttled by a
shared semaphore (`MAX_CONCURRENT_CONNECTIONS`, currently 1) — even a
handful of these devices showed real BLE connection instability during
`python-mobius`'s own development.

## Connection architecture

**Persistent connections, not connect-per-poll.** Each gateway gets
exactly one shared BLE connection (`MobiusConnectionManager`), reused by
every device relaying through it — connected once, read many times, not
reconnected on every poll.

**Reconnection is always by serial number, never a cached BLE address.**
These devices' addresses aren't guaranteed stable over time (confirmed via
real hardware and via the official app's own device-identity model — see
`python-mobius`'s `documentation/12-device-identity-and-address-stability.md`).
When a reconnect is needed, this integration resolves the device's
*current* address by reading Home Assistant's own already-running
Bluetooth cache (`bluetooth.async_discovered_service_info()`) and matching
on serial number, with a one-shot active-scan request as a fallback if the
device isn't found on the first, cache-only pass — deliberately not via
`python-mobius`'s own `find_device_by_serial()`, since that runs an
independent `BleakScanner`, which would conflict with Home Assistant's
shared one.

**Failure detection is reactive**: a dropped connection is discovered when
a scheduled read actually fails (marking the connection disconnected so
the *next* poll cycle, ~`POLL_INTERVAL` later, reconnects fresh), not via
a bleak disconnect callback. `python-mobius` itself retries a single
relayed round-trip once, transparently, before ever surfacing a timeout —
a relayed read crosses both BLE and the Thread mesh, and a single
dropped/delayed packet somewhere in that longer path turned out to be
common enough in production that the official app was built assuming it,
not treating it as an edge case.

**Discovery is also serial-based**, not just the runtime connection.
Config entry `unique_id` is the device's serial number (falling back to
address only in the rare case manufacturer data genuinely isn't available
yet at discovery time, with a re-check once fuller data arrives). Without
this, a device whose address changed would trigger a fresh "Mobius device
discovered" notification for something you already have configured,
potentially creating a duplicate entry for the same physical device.

## Polling design

Each device is polled every `POLL_INTERVAL` (30s) — the gateway's own
direct read, or a relayed read through it, depending on role. A separate,
much less frequent per-tank task (see "Tank / gateway architecture" above)
handles maintenance that doesn't need to happen every poll cycle.

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
