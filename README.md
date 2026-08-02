# ha-mobius

A Home Assistant custom integration for "Mobius"-protocol aquarium
equipment (EcoTech Marine VorTech/Radion, AquaIllumination, NYOS Quantum,
etc.), built on [`python-mobius`](https://pypi.org/project/python-mobius/).

Not affiliated with or endorsed by any of these companies. See
`python-mobius`'s own documentation for the full protocol writeup this is
built on.

## Status: early / read-only

This integration currently does **discovery and status reporting only** —
no control (starting scenes, changing schedules) yet. That's a deliberate
choice, not a limitation of the underlying library: control support is
being kept in lockstep with `python-mobius` itself as it grows write
capabilities, rather than getting ahead of it.

**Tested, not just written against docs.** The config flow, coordinators,
and sensor platform are exercised by a real test suite running against an
actual (test-harness) Home Assistant core — `pytest-homeassistant-custom-component`
— not just syntax-checked or written from documentation alone. Coverage
includes: Bluetooth discovery using real advertisement bytes captured from
actual hardware, config entry setup/unload, both coordinator tiers
(including error/unavailable handling), and full end-to-end entity
creation with correct states for both a pump and a light. See `tests/`.
What *hasn't* been exercised: a real BLE connection during setup (that part
is mocked, since no hardware is available in CI) — so a live run against
real devices is still the next real-world test. If something doesn't
behave as described here, please open an issue.

### Development / running the tests

```bash
git clone https://code.r3pek.org/r3pek/ha-mobius
cd ha-mobius
pip install -r requirements_test.txt
pip install python-mobius bleak-retry-connector
pytest tests/
```

## What it does

- **Autodiscovery**: Home Assistant's own Bluetooth integration triggers
  setup automatically when it sees a device advertising as `MOBIUS` (or
  matching the confirmed manufacturer ID `0x0202`) — no manual scanning
  needed. Manual setup (picking from already-seen-but-unconfigured devices)
  is also available via *Settings → Devices & Services → Add Integration*.
- **One Home Assistant device per physical device**, with model,
  manufacturer, serial number, and firmware version ("Product OS" —
  confirmed via real hardware to be the most meaningful single "what
  firmware is this" answer, ported from the app's own display labels;
  see `python-mobius`'s `get_firmware_versions()`) populated in the
  device registry. Full per-component firmware breakdown (Radio, Radio
  Bootloader, WLAN, etc.) is available via `python-mobius` directly if you
  need it — not surfaced as individual sensors here, to avoid sensor
  sprawl for something that's fundamentally device info, not a changing
  value.
- **Sensors** (all read-only):
  - Every device: support tier (light/pump/pump-experimental/unsupported),
    error state, schedule point count.
  - Pumps: operation state, motor speed (raw), estimated flow (GPH).
  - Lights: one intensity sensor per channel (`%`), reflecting the same
    client-side schedule interpolation `python-mobius` replicates from the
    official app — not a live device read (there isn't one; see
    `python-mobius`'s docs on why). Also a calibration sensor (completed
    True/False, plus last-calibration-date as an attribute) — confirmed
    via real hardware and via the app's own UI gating to be a light
    feature specifically; not added for pumps, which don't support it.

## Connection architecture

**Persistent connections, not connect-per-poll.** Each physical device
gets exactly one shared BLE connection (`MobiusConnectionManager`), used
by both polling tiers below — connected once, read many times, not
reconnected on every poll. Earlier versions of this integration
connected/disconnected on every single poll, which meant real BLE
handshake overhead every ~10 seconds; this was the "proper fix" flagged
as a known gap back then, now implemented.

**Reconnection is always by serial number, never a cached BLE address.**
These devices' addresses aren't guaranteed stable over time (confirmed via
real hardware and via the official app's own device-identity model — see
`python-mobius`'s `documentation/12-device-identity-and-address-stability.md`).
When a reconnect is needed (the first connect, or after a detected drop),
this integration resolves the device's *current* address by reading Home
Assistant's own already-running Bluetooth cache
(`bluetooth.async_discovered_service_info()`) and matching on serial number
— deliberately not via `python-mobius`'s own `find_device_by_serial()`,
since that runs an independent `BleakScanner`, which would conflict with
Home Assistant's shared one.

**Failure detection is reactive**, not via a bleak disconnect callback: a
dropped connection is only discovered when a scheduled read actually
fails, then reconnected once and retried within that same poll cycle.
Given the fast tier polls every ~10s, this is at most ~10s of staleness
in exchange for meaningfully simpler code.

**Discovery is also serial-based**, not just the runtime connection.
Config entry `unique_id` is the device's serial number (falling back to
address only in the rare case manufacturer data genuinely isn't available
yet at discovery time, with a re-check once fuller data arrives). Without
this, a device whose address changed would trigger a fresh "Mobius device
discovered" notification for something you already have configured,
potentially creating a duplicate entry for the same physical device.

## Polling design

Two tiers per device, to avoid hammering BLE more than necessary:

- **Status** (~10s): cheap reads — identity + live pump telemetry. Now
  just a read over an already-open connection in the common case, not a
  full connect/disconnect cycle.
- **Schedule** (~10min): expensive reads — the full programmed schedule,
  which doesn't change minute-to-minute anyway.

Connection *attempts* (the first connect for each device, or a reconnect
after a detected drop) are limited via a shared semaphore across the whole
integration (default: 2 concurrent) — even a handful of these devices
showed real BLE connection instability during `python-mobius`'s own
development. With persistent connections this matters far less often than
it used to, since normal polling no longer connects at all.

## Install

### Manual (works today, no GitHub/HACS needed)

Copy `custom_components/mobius/` into your Home Assistant `config/custom_components/`
directory, then restart Home Assistant.

### HACS

**HACS only works with repositories hosted on GitHub** (it uses GitHub's
API directly -- OAuth auth, GitHub Releases for versioning -- and doesn't
support GitLab/Gitea/Forgejo). This repo's source of truth is
`https://code.r3pek.org/r3pek/ha-mobius` (Forgejo), which HACS can't point
at directly. To use HACS anyway:

1. Set up a push mirror from this Forgejo repo to a public GitHub repo
   (Forgejo: repo Settings → Push Mirror -- syncs automatically on every
   push, no manual work after initial setup).
2. In HACS: Settings → Custom repositories → add the **GitHub mirror's**
   URL (type: Integration).

The repo is already structured correctly for HACS (`hacs.json` at the
root, `custom_components/mobius/` layout, valid `manifest.json`) -- the
only blocker is which git host serves the actual content HACS reads from.

## Dependencies

`python-mobius` and `bleak-retry-connector` are declared in `manifest.json`
and installed automatically by Home Assistant when the integration loads —
no separate `pip install` needed.

## License

GPLv2 — see [`LICENSE`](./LICENSE).
