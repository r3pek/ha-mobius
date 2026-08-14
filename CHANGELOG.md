# Changelog

## 0.3.2

- Fixed relayed devices (anything reached through a gateway, not
  directly over BLE) sometimes getting stuck failing every single
  update for extended periods, even though the gateway itself and
  every other device on the tank kept working fine the whole time. If
  a device keeps failing to relay through its current gateway, a
  different gateway now gets tried automatically instead of needing a
  manual reload to recover.
- Fixed a device occasionally going missing from Home Assistant's own
  Bluetooth cache for a long stretch, even while still nearby and
  broadcasting. Mobius now periodically checks its own gateway is
  still visible there and asks Home Assistant to do a quick active
  scan if not, rather than only finding out reactively once something
  actually failed to connect.
- Now requires `python-mobius` 0.4.3 (still auto-installed, nothing to
  do manually) and won't automatically move to its next minor version
  even once one exists, to avoid a `python-mobius` change breaking
  Mobius before a matching update here has actually been tested
  against it.

## 0.3.1

- Fixed the "Discovered at" sensor, which could show confusing,
  out-of-order values. It's now a "last seen" attribute on the mesh
  address sensor instead, updating about every 30 seconds.
- Fixed gateway failover getting stuck bouncing between the same two
  devices forever on a tank with more than two, never trying the
  others.
- A tank now checks and refreshes its own connection info (which
  device to reconnect through, addresses) about once a minute instead
  of every 12 hours, so a device that couldn't be reached comes back
  much sooner once it's reachable again.
- Fixed newly-added devices sometimes never showing up in Home
  Assistant, even though they were clearly nearby and broadcasting.
- Removing a device (or a whole tank) now also tells Home Assistant to
  forget it was seen before, so re-adding the same physical device
  later isn't silently blocked.
- Now requires Home Assistant 2026.7.0 or newer.
- Added a lot more detail to debug logging (Settings > Devices &
  Services > Mobius > Enable debug logging) -- connection attempts,
  gateway elections and failovers, and mesh scans are all covered now,
  to make it much easier to see what's going on if something isn't
  connecting.
- The diagnostics download now also shows whether Home Assistant
  itself currently sees each device over Bluetooth, separately from
  what Mobius has cached -- useful for telling "Mobius's own cached
  info is stale" apart from "Home Assistant hasn't seen this device
  recently" when nothing's connecting.
- Most sensors now have a proper icon instead of the generic default.

## 0.3.0

- Devices can now be added as a full tank instead of one at a time.
  When a new device is found, Mobius checks whether it's part of a
  bigger setup and offers to add every device on that tank at once.
  Devices in the same tank are grouped together under one hub in the
  device list.
- The "add tank" screen now lists the actual devices found, and lets
  you name the tank right there.
- A newly found device that belongs to a tank you've already set up
  gets added to it automatically -- no need to set it up separately.
- Mobius now periodically double-checks which devices are actually on
  each tank. If a device turns up on a different tank you've already
  set up (for example, after moving it to another aquarium), it gets
  moved there automatically. A device that just goes quiet is left
  alone, though -- it's not removed.
- Fixed a real bug where a tank could fail to load at all if even one
  of its devices was temporarily unreachable, even after a restart.
  Now the tank loads as long as at least one device responds; a
  device that's still unreachable just shows unavailable instead of
  blocking the whole tank.
- Setting up a tank now only needs to connect to one device, not one
  per device -- Mobius learns where the rest are from that single
  connection instead. Faster setup, less Bluetooth traffic.
- Added Mesh address, Discovered at, and Mesh prefix diagnostic
  sensors.
- Added a "Gateway device" sensor showing which device is currently
  relaying for the rest of a tank, by name.
- Added support for Home Assistant's built-in diagnostics download
  (Settings > Devices & Services > Mobius > your tank > Download
  diagnostics), useful if you ever need to report an issue.
- Various reliability and under-the-hood fixes.

**After upgrading**: your existing Mobius devices will show as failed.
This is expected -- remove them and Home Assistant will find them again
automatically, this time offering to add each tank as a whole. Do this
after upgrading, not before, so rediscovery happens under the new
version from the start.

## 0.2.2

- **Requires `python-mobius>=0.3.1`** for a real, high-impact fix in
  that library: `Brightness` (the schedule's master dimmer channel) was
  never being applied to any other channel at all -- every channel,
  moonlight included, was being interpolated and returned without ever
  being scaled by `Brightness`. Found via real hardware testing: a light
  showing ~1% in the app's own display came back as ~25% from this
  integration's sensors at the same moment. No code changes needed on
  this side -- `LightChannelIntensitySensor` (and everywhere else that
  reads `current_intensities`) already just displays whatever
  `python-mobius` computes, so this integration picks up the corrected
  values automatically once the dependency is upgraded. Confirmed by
  actually installing the fixed `python-mobius` into this project's own
  test environment and running the full suite against it (98/98 still
  passing), not just assumed compatible from reading the change.

- **Fixed two real hassfest findings, caught once its CI check actually
  started running.** `manifest.json`'s keys weren't in hassfest's
  required order (`domain`, `name`, then alphabetical) -- reordered.
  Added `CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)`:
  hassfest requires any integration implementing `async_setup` to define
  one of `CONFIG_SCHEMA`/`PLATFORM_SCHEMA`/`PLATFORM_SCHEMA_BASE` (or a
  helper equivalent); this integration is config-entry-only (no YAML
  `configuration.yaml` support at all), so
  `config_entry_only_config_schema` is the correct one -- confirmed via
  real behavior testing that it doesn't raise if someone configures it
  via YAML anyway, it logs a clear error and creates a Home Assistant
  Repairs issue instead, which is the actual intended, more
  user-friendly handling. 2 new tests.

## 0.2.1

- **Light channel intensity sensors now round to whole numbers** instead
  of one decimal place (e.g. `100%`, not `100.0%`) -- the underlying raw
  value is itself only ever a coarse permille figure, so a fractional
  percent didn't represent any real additional precision. Also set
  `suggested_display_precision=0` as a frontend display hint. 4 new
  tests confirming actual rounding behavior (not just the string
  formatting difference) -- including that even an evenly-divisible
  value now comes back as a true `int`, not a `float` like `100.0`.

- **Fixed a follow-up bug: the new hardware sensor tried to re-decode
  values `python-mobius>=0.3.0` already decodes.** That release changed
  `get_hardware_info()` to return confirmed label strings for `Color`/
  `ProductType`/`RadioType`/`MotorType` (e.g. `"White"`/`"VorTech"`)
  instead of raw bytes -- `HardwareRevisionSensor.extra_state_attributes`
  and `derive_hw_version()` were both still calling `int.from_bytes()` on
  what's now already a string or plain int, which would have raised on
  the four decoded fields (and silently misbehaved for `Revision`, which
  is now an int, not bytes, so `int.from_bytes()` on it would also fail).
  Both now use the already-decoded values directly rather than
  re-decoding them. Also fixed a latent bug while in there:
  `derive_hw_version()` used a falsy check (`if not raw`) that would
  have incorrectly treated a legitimate `Revision=0` as "missing" --
  now checks `is None` instead. Requires `python-mobius>=0.3.0`.
  `TestDeriveHwVersion`'s 6 tests rewritten for the new input shape
  (including one new test specifically covering the `Revision=0` case),
  plus extended assertions in the existing sensor setup test confirming
  decoded string fields pass through the sensor unmangled.

## 0.2.0

- **Added `Firmware version` and `Hardware revision` diagnostic
  sensors**, each with the full per-component breakdown as attributes
  (e.g. `Radio Firmware`/`Filesystem`/`Radio OS`/`Radio`/`WLAN`/
  `Product OS`/`Product Bootloader` for a light's firmware; `Color`/
  `Revision`/`ProductType`/`RadioType`/`MotorType` for hardware).
  Home Assistant's own built-in device info card already shows the same
  headline values as `sw_version`/`hw_version` (labeled "Firmware"/
  "Hardware" -- fixed labels, not customizable per-integration), but
  only ever the single derived value, never the full breakdown; these
  sensors are the practical way to get the rest of it onto the same
  device page. Applies to both pumps and lights, not gated behind
  device type.

- **Reduced `MAX_CONCURRENT_CONNECTIONS` from 2 to 1**, after the
  previous fix (sharing the connection semaphore between gateway
  connections and mesh address discovery) turned out not to be enough on
  its own. The semaphore only throttles the brief window a connection
  attempt is actually connecting -- once a gateway's connection succeeds,
  it's held open persistently and doesn't occupy a permit anymore,
  invisible to this limit entirely. Confirmed via real-world testing
  against an ESPHome Bluetooth proxy with a hardware ceiling of 3
  simultaneous connections: a value of 2 meant an already-open gateway
  connection (1, invisible to the semaphore) plus 2 more concurrent
  discovery attempts (allowed by the semaphore) could reach exactly the
  proxy's ceiling with zero headroom -- both the gateway flapping and a
  device failing to connect at all during that window are consistent
  with this same exhaustion. Serializing to 1 keeps at most one NEW
  connection attempt in flight on top of any already-open connections,
  rather than trying to guess a value that happens to leave enough
  headroom for a specific proxy's real limit. Doesn't fully solve a
  multi-tank setup (more pan_id groups means more persistent gateway
  connections, each similarly invisible to this semaphore) -- documented
  as a known constraint in `const.py` rather than solved for hardware
  limits this integration has no way to know about.

- **Fixed a real bug found via production logs: gateway connections were
  flapping continuously, promoting back and forth between the same two
  devices every ~70 seconds.** `discover_mesh_address()` (used both for
  proactive discovery at setup and the coordinator's on-demand fallback)
  connected directly to a device without acquiring the shared connection
  semaphore (`MAX_CONCURRENT_CONNECTIONS`) at all -- it's a separate
  connection from any gateway's, but wasn't throttled alongside it. A
  burst of discovery calls (e.g. right after a promotion, when the
  demoted former gateway needs its own mesh address for the first time,
  since it never needed one while it was directly connected) could
  exceed the real BLE adapter's actual concurrent-connection capacity
  even while `MAX_CONCURRENT_CONNECTIONS` appeared respected everywhere
  else, causing the *gateway's own otherwise-healthy connection* to fail
  for reasons unrelated to the gateway itself -- triggering unnecessary
  failover, which needed its own discovery call, which contended for
  the same unthrottled resource, repeating the cycle indefinitely.
  `GatewayRegistry.semaphore` is now a public attribute so
  `discover_mesh_address()` can share the exact same semaphore object
  `MobiusConnectionManager` uses. 3 new tests, including one that
  directly measures concurrent in-flight connection attempts under load
  to confirm the throttling actually holds, not just that the semaphore
  object gets touched.

- **Added hardware revision display.** `get_hardware_info()` (already
  present in `python-mobius`, just not wired in here) is now fetched
  every poll cycle and shown as `hw_version` on the device card,
  alongside `sw_version` -- `derive_hw_version()` picks the `"Revision"`
  field (the only one of `Color`/`Revision`/`ProductType`/`RadioType`/
  `MotorType`/`Segments` that actually corresponds to "hardware
  revision"), shown as a plain integer since no display-formatting
  convention is confirmed for this field. Kept in sync on every poll if
  it changes, same as `sw_version`.

- **Corrected `sw_version`'s label priority: `"Firmware"` now comes
  before `"Product OS"`.** Requires `python-mobius>=0.2.1` (its
  multi-block response fix -- see that project's changelog) to actually
  see the full picture here: once a light's previously-truncated
  firmware response came back complete, direct comparison against what
  the official app displays confirmed it treats `"Firmware"`
  (`FirmwareType.LEDClusterMicro`/`Esp32*Firmware` -- the light's actual
  LED-driver microcontroller) as primary, not `"Product OS"`
  (`FirmwareType.MainMicroOS`) as originally assumed. `sw_version`s that
  were previously showing a `Product OS` value despite a `Firmware` value
  also being present will change on next update.

- **Fixed a real bug: lights' device card showed no firmware version at
  all.** `sw_version` was derived from a single hardcoded lookup of
  `"Product OS"` (`FirmwareType.MainMicroOS`) in the firmware versions
  dict -- confirmed real display label for the app's own "main" firmware,
  but at least some real Radion lights don't report that specific
  `FirmwareType` in their response at all (unlike pumps, which always
  do), silently leaving `sw_version` empty for those devices. Added
  `derive_sw_version()`: falls through a priority list (`Product OS` →
  `Radio Firmware` → `Radio OS` → `Radio`) rather than assuming any one
  label is always present, used consistently at both initial device
  registry creation and the coordinator's ongoing sync. 7 new tests
  covering the fallback chain itself, plus the existing full-setup-flow
  light test's fixture updated to match the actual real-world scenario
  (no `Product OS` reported) rather than the untested assumption it had
  before.

- **Multi-device relay support**: devices sharing a pan_id (Thread mesh/
  "tank", confirmed via `Tank`/`CommGroup` in the decompiled app) now
  share one physical BLE connection instead of each holding its own.
  - `gateway_registry.py`: tracks each pan_id's current gateway and
    members. Gateway selection is RSSI-based among devices joining a
    newly-forming group concurrently (e.g. several config entries loading
    around Home Assistant startup); an established group's gateway is
    never displaced just because a better-signal device joins later.
    Failover promotes another member after `GATEWAY_FAILURE_THRESHOLD`
    (3) consecutive gateway read failures -- deliberately much faster
    than the 5-minute general mark-unavailable grace period, since a bad
    gateway takes its whole group down with it. A device moving to a
    different pan_id (a device physically moved to a different tank) is
    handled as a leave-then-join, re-promoting the old group's gateway if
    the mover was it.
  - `coordinator.py`: one coordinator per device now (previously two,
    fast/slow tiers), one ~30s cycle fetching both status and schedule
    data together. Each cycle checks fresh whether this device is
    currently its group's gateway (direct read) or not (reads through
    `RelayedMobiusDevice`, addressed to a cached or on-demand-discovered
    mesh address) -- a promotion between cycles is picked up automatically
    on the very next read, no separate transition handling needed. A
    single failed read doesn't immediately mark a device unavailable --
    last-known-good data is served for up to 5 minutes of consecutive
    failures first.
  - `config_flow.py`/`const.py`: pan_id is now stored in config entry
    data (`CONF_PAN_ID`) alongside serial/address, read from the same BLE
    advertisement manufacturer data, no connection needed. Entries
    created before this was added fail setup cleanly (asking for
    re-configuration), same handling as the existing missing-serial case.
  - `sensor.py`: device names now include which tank (pan_id) a device
    belongs to, since names otherwise give no indication once more than
    one tank is in play.
  - Relayed devices' mesh addresses are now discovered proactively at
    setup time (before the first poll cycle), rather than relying solely
    on the on-demand fallback baked into the first read. Runs on every
    entry setup -- both a brand-new device and every existing device on
    every Home Assistant restart, not just first-ever setup. A failure
    here isn't fatal; the coordinator's on-demand fallback still covers
    it on a later poll.
  - Requires `python-mobius>=0.2.0` (for `RelayedMobiusDevice`).

- **Fixed a real bug: the manual-setup device list wasn't actually
  excluding already-configured devices.** After switching `unique_id` to
  serial-based, the filter was still comparing `discovery.address` against
  `self._async_current_ids()` -- which now holds serial numbers, not
  addresses. Comparing a MAC address against a set of serials never
  matches anything, so an already-configured device kept showing up in
  the manual "Add Integration" dropdown as if it were new. Fixed to
  compare serial against serial instead. New regression test
  (`test_manual_setup_excludes_already_configured_devices`) reproduces
  the exact scenario and was confirmed to fail without the fix.

## 0.1.3

- **Simplified the integration's display name.** `manifest.json`/`hacs.json`
  `"name"` changed from `"Mobius (VorTech / Radion / etc.)"` to just
  `"Mobius"`.

- **Config entry title no longer includes the MAC address.** `_title_for()`
  (the discovery card / integrations-list title) used
  `f"{model} ({discovery.address})"` -- still address-based, missed in the
  earlier serial-based-identity work. Now uses serial for the same
  disambiguation purpose (`_device_info()` in `sensor.py` already did this
  correctly; this brings the title in line with it), which also means the
  title won't go stale if the device's address changes later, since a
  config entry's title is set once at creation and never auto-updated.
  New assertion in `test_bluetooth_discovery_creates_entry` confirms the
  exact title format and that the address is absent from it.

## 0.1.2

- **Clarified the `no_manufacturer_data` abort message.** Now explains
  that it's normal/temporary and self-resolves automatically within
  seconds (Home Assistant re-triggers discovery on the next fuller
  advertisement, no user action needed), rather than reading like
  something failed. Worth noting where this can actually surface: for
  automatic discovery this abort happens silently, before the confirm
  card ever renders, so most people won't see this message at all; manual
  setup already excludes unidentifiable devices from its dropdown, so this
  now only realistically shows up in the narrow case where a device's
  advertisement data changes between rendering that dropdown and
  submitting it.

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

- **Firmware version now re-fetched periodically, not just once at setup.**
  Corrects the entry below: firmware was assumed to essentially never
  change, so it was fetched once at setup and never checked again -- wrong,
  confirmed by a real OTA update to real pump hardware mid-development of
  this integration. Moved the fetch into the schedule (slow, ~10min) tier
  instead of a one-time setup-only fetch, and added an explicit sync step
  so the device registry's `sw_version` actually updates when it changes,
  rather than silently going stale. New tests
  (`test_schedule_coordinator_syncs_device_registry_sw_version_on_change`,
  `test_schedule_coordinator_does_not_touch_registry_when_unchanged`).

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
