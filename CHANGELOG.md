# Changelog

## Unreleased

- **Config flow: fail fast rather than fall back to an address-based
  identity.** Corrects the previous entry below (already amended here
  rather than left describing since-changed behavior) -- discovery
  originally fell back to an address-based `unique_id` when manufacturer
  data wasn't immediately available, upgrading to serial-based later if
  possible. Per explicit preference (better to wait and retry than risk
  adding a device whose identity could break later), both the automatic
  and manual setup flows now abort immediately (`no_manufacturer_data`) if
  a serial can't be determined, rather than proceeding with anything
  address-based. No special in-flow retry loop needed: Home Assistant's
  own Bluetooth integration naturally re-triggers discovery on a later,
  more complete advertisement (typically within seconds). The manual setup
  dropdown also now excludes any device it can't identify a serial for,
  rather than offering it and failing on selection. New tests
  (`test_bluetooth_discovery_aborts_without_manufacturer_data`,
  `test_manual_setup_excludes_unidentifiable_devices`).

- **Fixed a real gap alongside the persistent-connection work: discovery
  was still address-based.** Config entry `unique_id` was
  `discovery_info.address`/the raw address in both the automatic
  (Bluetooth-triggered) and manual setup flows -- meaning a device whose
  address changed (see the persistent-connection entry below) would
  trigger a fresh "Mobius device discovered" notification for something
  already configured, and could create a duplicate entry if clicked
  through. Now uses the device's serial number instead. New regression test
  (`test_address_change_is_recognized_as_the_same_device`) reproduces the
  exact scenario: a device already configured gets rediscovered under a
  different address but the same serial, and correctly aborts instead of
  duplicating.

- **Added firmware version and calibration status.** Device registry
  `sw_version` is now populated from the confirmed "Product OS" firmware
  label (`python-mobius`'s `get_firmware_versions()`). New calibration
  sensor for lights (completed True/False, last-calibration-date as an
  attribute) -- confirmed via real hardware and the app's own UI gating to
  be a light-only feature; not added for pumps, and not added at all if a
  light doesn't report calibration data. Fetched on the slow (schedule)
  tier, not the fast one, since calibration status essentially never
  changes during normal operation (unlike firmware -- see the entry above).

- **Major architecture change: persistent connections, not connect-per-poll.**
  Each device now gets exactly one shared BLE connection
  (`MobiusConnectionManager`), used by both polling tiers -- connected
  once, read many times, not reconnected on every single poll like before.
  This was flagged as the "proper fix" for BLE connection overhead back
  when the fast-poll interval was lowered to 10s; now implemented.
  - Reconnection (first connect, or after a detected drop) always
    resolves the device's CURRENT address by serial number, matching how
    the official app itself identifies devices -- confirmed via real
    hardware and via `python-mobius`'s `documentation/
    12-device-identity-and-address-stability.md`. Deliberately reads Home
    Assistant's own already-running Bluetooth cache
    (`bluetooth.async_discovered_service_info()`) to do this, NOT
    `python-mobius`'s own `find_device_by_serial()` -- that function runs
    an independent `BleakScanner`, which would conflict with Home
    Assistant's shared one (the exact anti-pattern this integration has
    avoided from the start).
  - New `CONF_SERIAL` stored in config entry data (alongside the existing
    `CONF_ADDRESS`, kept for display/debugging) -- the config flow now
    aborts cleanly if a device's serial can't be determined, since it's
    required for reliable connections.
  - Failure detection is reactive (a dropped connection is only noticed
    when a scheduled read actually fails, then reconnected once and
    retried within the same poll cycle), not via a bleak disconnect
    callback -- simpler, and at most ~10s of staleness given the fast
    tier's polling interval.
  - `async_unload_entry` now properly closes the persistent connection,
    which didn't exist to close under the old per-poll-connect design.
  - **Requires `python-mobius>=0.1.4`** (needs the new `is_connected`
    property, not yet in the released `0.1.3`).
  - Rewrote `tests/test_coordinator.py` (12 tests covering the connection
    manager directly: serial resolution against HA's cache, connect-once-
    and-reuse, reconnect-after-failure, concurrent-connect-only-happens-
    once via the internal lock, and the retry-within-one-cycle behavior)
    and updated `tests/test_sensor.py` for the new architecture. 25 tests
    total, all passing.

## 0.1.1

- Bluetooth autodiscovery via manifest.json matchers (local_name="MOBIUS",
  manufacturer_id=0x0202).
- Config flow: automatic (bluetooth-triggered) and manual setup.
- Two-tier DataUpdateCoordinator polling (fast status ~10s / slow schedule
  ~10min), with a shared connection-count semaphore across the integration.
  Fast polling does a full connect/read/disconnect cycle every time (not a
  persistent connection) -- worth knowing if you ever see connection
  instability; see `const.py` for the tradeoff notes.
- Read-only sensor platform: support tier, error state, schedule point
  count for all devices; operation state, motor speed (%), estimated flow
  for pumps; per-channel intensity sensors for lights.
- Device naming disambiguates by serial number when the device's own
  configured name is blank (confirmed real scenario: identical-model
  devices, e.g. two XR15 lights, would otherwise collide on device name).
  Clean entity IDs thanks to `python-mobius` 0.1.1 decoding serials as
  readable ASCII instead of hex.
- Motor speed sensor shows the confirmed percentage (`speed_percent` from
  `python-mobius` 0.1.1+) with a proper `%` unit. `MotorSpeed` is confirmed
  NOT to be RPM -- it's a percentage of max pump power in tenths of a
  percent, verified against the decompiled app's own display code. The raw
  signed value (sign encodes reverse-rotation direction) is available as a
  `raw_signed_value` / `reverse_rotation` attribute.
- Flow rate sensor uses `device_class=SensorDeviceClass.VOLUME_FLOW_RATE`
  with `native_unit_of_measurement="gal/h"` -- the actual native protocol
  value. This device class is NOT tied to HA's system-wide Metric/US
  toggle (verified against HA source), so displaying it in another unit
  (e.g. L/h) is a per-entity override via Settings -> Devices & Services ->
  Entities -> this entity -> "Unit of measurement".
- Custom brand icon (HA-house + Mobius-infinity mark), bundled inline via
  the HA 2026.3+ brands-proxy mechanism -- no GitHub PR to the centralized
  `home-assistant/brands` repo needed.
- Requires `python-mobius>=0.1.2`.

### Tested, including against real hardware

Exercised against a real (test-harness) Home Assistant core
(`pytest-homeassistant-custom-component`), not just written from docs --
15 tests covering manifest/dependency loading, config flow (using real
captured advertisement bytes), both coordinator tiers, full end-to-end
entity setup for pump and light, and serial-based naming disambiguation.

Issues found and fixed via real deployment feedback:

- **Discovery card showing a generic "Mobius device (address)" instead of
  the real model**: root cause was the initial `BluetoothServiceInfoBleak`
  snapshot passed to `async_step_bluetooth` having incomplete manufacturer
  data (e.g. matched via the `local_name` matcher before a scan-response
  merge completed), with the confirm step never refreshing it. Now
  re-fetches the latest cached advertisement via
  `bluetooth.async_last_service_info()` before rendering the confirm card
  and before creating the entry.
- **Connection failures after adding a device**
  (`"No backend with an available connection slot"`, device never becomes
  available): confirmed this is NOT a range/signal problem -- the error
  itself reports fresh advertisement reception. Root cause was a real
  connection leak in `python-mobius`'s `connect()` (see that project's
  changelog), fixed there.
- `SensorDeviceClass.VOLUME_FLOW_RATE` rejected the `gal/h` unit on an
  older pinned HA test-harness version -- resolved by confirming the
  target deployment version (2026.06) supports it.
- A diagnostic sensor had a leftover plain string `"diagnostic"` instead
  of the `EntityCategory.DIAGNOSTIC` enum -- fixed, and all diagnostic
  entities now set `_attr_entity_category` directly.

### Known gaps

- No control yet (scenes, schedule writes) -- deliberately kept read-only
  in lockstep with what `python-mobius` supports, rather than getting
  ahead of it.
- Real BLE hardware connection during config-entry *setup* is mocked in
  the test suite (no hardware available in CI) -- coordinator behavior
  against real connections has been validated manually, not via the
  automated suite.
