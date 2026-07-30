# Changelog

## 0.1.0 — initial release (updated)

- Device naming now disambiguates by serial number when the device's own
  configured name is blank (confirmed real scenario: identical-model
  devices, e.g. two XR15 lights, would otherwise collide on device name).
  Also produces much cleaner entity IDs now that `python-mobius` 0.1.2
  decodes serials as readable ASCII instead of hex.
- Flow rate sensor: restored `device_class=SensorDeviceClass.VOLUME_FLOW_RATE`
  (confirmed `gal/h` is a valid unit on HA 2026.06) with `native_unit_of_measurement`
  staying `"gal/h"` -- the actual native protocol value, not a display
  preference. HA automatically converts the *displayed* value to the
  instance's configured unit system (e.g. `L/h` for Metric), so no manual
  unit conversion or hardcoded "default" is needed.
- Requires `python-mobius>=0.1.1`.
- Bluetooth autodiscovery via manifest.json matchers (local_name="MOBIUS",
  manufacturer_id=0x0202).
- Config flow: automatic (bluetooth-triggered) and manual setup.
- Two-tier DataUpdateCoordinator polling (fast status / slow schedule),
  with a shared connection-count semaphore across the integration.
- Read-only sensor platform: support tier, error state, schedule point
  count for all devices; operation state/motor speed/flow rate for pumps;
  per-channel intensity sensors for lights.
- **Tested against a real (test-harness) Home Assistant core**
  (`pytest-homeassistant-custom-component`), not just written from docs.
  14 tests covering manifest/dependency loading, config flow (using real
  captured advertisement bytes), both coordinator tiers, full end-to-end
  entity setup for both a pump and a light, and serial-based device-naming
  disambiguation. Real BLE hardware itself is still untested (mocked in
  CI) -- that's the next real-world test.
- Bugs found and fixed via that testing (not caught by syntax-checking
  alone):
  - `SensorDeviceClass.VOLUME_FLOW_RATE` rejected the `gal/h` unit on the
    HA version the test harness pins (2025.1.4) -- resolved by confirming
    the target deployment version (2026.06) supports it; the underlying
    version-dependency is now documented in code rather than silently
    assumed.
  - One diagnostic sensor (`SchedulePointCountSensor`) still had a leftover
    plain string `"diagnostic"` instead of the `EntityCategory.DIAGNOSTIC`
    enum from an earlier pass -- fixed, and all diagnostic entities now set
    `_attr_entity_category` directly (a path Home Assistant's own
    `Entity.entity_category` property checks before falling back to
    `entity_description`, avoiding a dataclass-field quirk observed on the
    pinned test-harness HA version).
