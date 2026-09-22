# Changelog

## Unreleased

- Manual setup now runs an active Bluetooth scan when a Mobius device is
  nearby but its serial number can't be read yet, instead of reporting
  that no devices were found. If the serial still can't be read, it
  explains how to fix it.
- Writes to a device reached through another device (a relayed device) now
  show an error when the device rejects them.
- Added pump battery settings: "Battery backup speed" (up to the pump's
  battery backup max speed) and, on pumps that have them, the boosted
  battery power and on/off times. Boosted battery settings can only be
  changed while the pump runs on battery.
- Added a "Running on battery" sensor for pumps with battery backup.
- Light schedule card: the moon now only shows the current moon phase
  (while lunar phases are on). Lunar phases are turned on and off from the
  schedule editor, for every light in the schedule group.
- Fixed the remaining time of a running scene not counting down: the cards
  now count it down every second (new `ends_at` attribute on the scene
  selection).
- Returning to the normal schedule is now sent to the whole tank in one
  write, like the app, instead of to each device separately.
- A tank with a single device now shows a notification whenever it loads:
  Mobius keeps the Bluetooth connection to it, so the Mobius app can't
  control that device until this entry is disabled or another device of
  the tank is added.
- Internal cleanup. Requires the matching python-mobius release.

## 0.8.0

- Fixed the pump card showing the device name next to the title instead of
  below it.

## 0.8.0-beta8

- **Fix**: the schedule intensity sensor's `lunar_enabled` and
  `moon_phase_icon` attributes showed Unknown during the day. Requires
  python-mobius 0.8.4+.
- The Lunar switch and the moon toggle are only shown on lights that
  support lunar phases.
- The moon toggle's tooltip shows the current phase name (e.g. "Waning
  Gibbous").
- The Lunar switch shows the phase name and phase day (0-29) as
  attributes.

## 0.8.0-beta7

- Added a "Lunar phases" switch for lights, and a moon button on the light
  card to toggle it (shows the current moon phase while on). Requires
  python-mobius 0.8.3+.
- The schedule editor's time field now follows Home Assistant's time
  format (12/24 h).
- Pump schedule points no longer have a day/night/sunrise/sunset option.
- The reverse-rotation hint for MaxSpeed/MinSpeed only appears on pumps
  that support reverse; other pumps don't accept negative values.
- Added icons for the Cloud Cover, Color Cycle and Disco scenes.

## 0.8.0-beta6

- **Fix**: the schedule intensity sensor could show the lunar reduction
  instead of the schedule intensity setting. Requires python-mobius 0.8.2+.
- Activating a scene shows a spinner while it runs and an error message
  if it fails.
- Fixed "0:00 remaining" shown for scenes without a timer (e.g. Feed Mode).
- Fixed an "Invalid configuration" error for the cards on narrow screens
  in sections view.

## 0.8.0-beta5

- Fixed the pump card's current mode: Anti-Sync and the parent pump of a
  Sync mode are shown correctly.
- Fixed the light chart's legend spacing.

## 0.8.0-beta4

- Fixed the gateway role alternating between the same two devices of a
  tank.
- Light chart: taller, larger hour labels, the tooltip no longer covers
  the point, and hovering shows the time.

## 0.8.0-beta3

- The light chart shows the last 24 hours up to now, with a 0/50/100 %
  scale.
- The pump card shows the current mode, including the active scene or the
  parent pump.
- Moved the schedule editor's load/download buttons to the header.

## 0.8.0-beta2

- Added a "Restart all devices" button on the tank device.
- The pump schedule editor shows speeds as whole percentages and Variance
  as None/Low/Medium/High, like the app.
- Light chart: hover tooltip, colour legend, and lines that continue to
  the current time.
- The light schedule editor shows a small bar per channel for each point.
- The pump card rounds readings and opens their history when clicked.
- Schedule editor buttons are now icons; the scene card's tiles are
  centred.
- Fixed a schedule intensity sensor named after its device instead of
  "Schedule intensity".

## 0.8.0-beta1

- Added two dashboard cards: a scene card and a schedule editor card.
- The schedule editor can add, change and delete points of light and pump
  schedules, load and download `.mob` files, and save to the device.
- The pump editor only offers the modes the pump supports.
- The light card shows a channel history chart and an overall intensity
  slider, and uses another light of the group when one is offline.
- The scene selection shows the active scene's remaining time
  (`duration_remaining_seconds`).
- Fixed batched reads staying disabled after a gateway change, or after a
  single failure.

## 0.7.0

- Added a scene selection on each tank device: activates a scene on the
  whole tank; "None" returns to the schedule.
- Added a "Configured scenes" sensor per device.

## 0.6.0

- LED auto-dim timeout and max fan speed show readable labels ("Off",
  "30 seconds", "100%").
- Added a poll interval setting on each tank device (10-300 s, default
  30 s).
- Added a time sync switch on each tank device.
- Each poll now needs a single request per device, making updates faster.

## 0.5.0

- **Breaking**: Local control, LED auto-dim, max fan speed and fan
  shutdown are now switches and selects instead of sensors. Remove the old
  sensors and update automations that used them.
- Updates are faster: device information is read in one request instead
  of five.
- Fixed the gateway role changing repeatedly in some tanks.
- Every entry now has a tank device, including single devices.
- Diagnostics include more gateway and polling information; the firmware
  version sensor lists the device's supported attributes.
- Fixed deprecation errors on newer Home Assistant versions.

## 0.4.1

- Fixed the flow sensor's minimum/maximum attributes not following the
  sensor's display unit.

## 0.4.0

- Added support for AquaIllumination devices.
- Added a reboot button for every device.
- Each tank's clock is set to the current time every hour.
- Added sensors for Local control, LED auto-dim, max fan speed and fan
  shutdown, when the device supports them.
- The flow sensor is only created for pumps with a reliable flow reading,
  and shows the flow range as attributes.
- The README describes Bluetooth proxy hardware, with an example ESPHome
  configuration (`esphome/mobius-bt-proxy.yaml`).

## 0.3.4

- Fixed a device name sometimes staying "Mobius device (SERIAL)".
- Fixed the firmware version showing "Unknown" for AquaIllumination
  devices.
- Requires python-mobius 0.4.4.

## 0.3.3

- Fixed some sensors of one or two devices staying unavailable after a
  reboot.

## 0.3.2

- Fixed relayed devices failing every update for long periods while the
  rest of the tank worked.
- Fixed devices going missing from Home Assistant's Bluetooth cache for
  long periods; Mobius now asks for a scan when its gateway isn't seen.
- Requires python-mobius 0.4.3, and doesn't move to its next minor version
  automatically.

## 0.3.1

- Replaced the "Discovered at" sensor with a "last seen" attribute on the
  mesh address sensor, updated about every 30 seconds.
- Fixed gateway failover alternating between the same two devices.
- Tanks refresh their connection information every minute, so
  unreachable devices come back sooner.
- Fixed new devices sometimes never showing up.
- Removing a device or tank lets Home Assistant discover it again.
- Requires Home Assistant 2026.7.0 or newer.
- More detailed debug logging and diagnostics; most sensors have icons.

## 0.3.0

- Devices are added as a whole tank: Mobius finds the other devices of the
  tank, lists them, and lets you name the tank.
- A new device that belongs to a configured tank is added to it
  automatically, and a device moved to another tank follows it.
- A tank loads as long as one of its devices responds.
- Setting up a tank connects to one device only.
- Added mesh address, mesh prefix and gateway device sensors, and
  diagnostics download.
- **After upgrading**, existing devices show as failed: remove them and
  add them again when Home Assistant finds them.

## 0.2.2

- Light intensities are correct when the Brightness channel is below
  100 %. Requires python-mobius 0.3.1.
- Fixed validation errors in the integration manifest.

## 0.2.1

- Light channel intensities are shown as whole percentages.
- Fixed hardware revision values.

## 0.2.0

- Added firmware version and hardware revision sensors; devices show their
  firmware and hardware version.
- Devices of one tank share a single Bluetooth connection.
- Fixed gateway connections switching back and forth.
- Fixed manual setup listing devices that are already configured.

## 0.1.3

- The integration is now called "Mobius", and entry titles use the serial
  number instead of the Bluetooth address.

## 0.1.2

- Devices are identified by serial number instead of Bluetooth address.
- Devices keep one Bluetooth connection instead of connecting on every
  update.
- Added a calibration sensor for lights; the firmware version is kept up
  to date.
- Clearer message when a device's serial number isn't available yet.

## 0.1.1

- First release: Bluetooth discovery, automatic and manual setup, and
  sensors for support tier, error state, schedule points, pump speed, flow
  and operation state, and light channel intensities.
