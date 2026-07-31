# Changelog

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
