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
  manufacturer, and serial number populated in the device registry.
- **Sensors** (all read-only):
  - Every device: support tier (light/pump/pump-experimental/unsupported),
    error state, schedule point count.
  - Pumps: operation state, motor speed (raw), estimated flow (GPH).
  - Lights: one intensity sensor per channel (`%`), reflecting the same
    client-side schedule interpolation `python-mobius` replicates from the
    official app — not a live device read (there isn't one; see
    `python-mobius`'s docs on why).

## Polling design

Two tiers per device, to avoid hammering BLE more than necessary:

- **Status** (~60s): cheap reads — identity + live pump telemetry.
- **Schedule** (~10min): expensive reads — the full programmed schedule,
  which doesn't change minute-to-minute anyway.

Connection attempts across the whole integration are also limited via a
shared semaphore (default: 2 concurrent) — even a handful of these devices
showed real BLE connection instability during `python-mobius`'s own
development, so this deliberately throttles rather than firing off N
simultaneous connections.

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
