"""Constants for the Mobius integration."""

import json as _json
from datetime import timedelta
from pathlib import Path as _Path

DOMAIN = "mobius"

# Device serial number. The serial, not the BLE address, identifies a device
# (see python-mobius documentation/12-device-identity-and-address-stability.md);
# CONF_ADDRESS is stored for display only.
CONF_SERIAL = "serial"

# Thread mesh ("tank") id from the BLE advertisement. A device can be moved
# to another tank, so it is re-checked on every reconnect.
CONF_PAN_ID = "pan_id"

# Poll interval for every device (status and schedule in one read).
POLL_INTERVAL = timedelta(seconds=30)

# Connection attempts (gateway connects/reconnects and mesh address
# discovery) allowed in flight at once, across all config entries. Open
# gateway connections don't hold a permit, so with 1 there is at most one new
# attempt on top of the open connections. Bluetooth proxies have a small
# connection limit (3 on ESPHome); with several tanks, each holding a gateway
# connection, that limit can still be reached.
MAX_CONCURRENT_CONNECTIONS = 1

CONNECT_TIMEOUT = 30.0

# Devices sharing a pan_id share one BLE connection (the gateway); the others
# are reached through it. See gateway_registry.py.

# How long a newly forming group waits for other members before choosing the
# gateway by RSSI. An established gateway is only replaced after
# GATEWAY_FAILURE_THRESHOLD failures, not when a better device appears.
GATEWAY_ELECTION_SETTLE_SECONDS = 3.0

# Consecutive failed gateway polls before another member is promoted to
# gateway. Much shorter than MARK_UNAVAILABLE_AFTER because a failing gateway
# takes its whole group down.
GATEWAY_FAILURE_THRESHOLD = 3

# Consecutive failed relayed reads to one target, through a gateway whose own
# reads succeed, before the target is restarted (see "Automatic restarts" in
# gateway_registry.py), or, with automatic restarts disabled, before another
# gateway is forced. A gateway can lose its mesh route to a single target
# while everything else works. Counted separately from gateway failures so
# the logs show which problem occurred.
RELAY_FAILURE_THRESHOLD = 3

# After an automatic restart, failed reads of the restarted devices are not
# counted for this long. A soft reboot takes about 10-20 s; the rest is for
# the device to rejoin the mesh.
RESTART_RECOVERY_WINDOW = timedelta(seconds=60)

# Time without any failed relayed read after which the restart escalation
# starts over from restarting single devices.
RESTART_ESCALATION_RESET_AFTER = timedelta(minutes=10)

# No automatic restart of any kind for this long after a tank restart.
TANK_RESTART_LOCKOUT = timedelta(hours=1)

# Consecutive polls in which the batched metadata read fails while the
# individual-reads fallback succeeds, before batching is disabled for that
# device. Re-enabled when the group's gateway changes, since the failure may
# be specific to the relay path.
BATCH_FAILURE_THRESHOLD = 2

# Time without a successful read before a device's entities become
# unavailable.
MARK_UNAVAILABLE_AFTER = timedelta(minutes=5)

# Extra attempts (and delay between them) for a relayed device's first,
# non-blocking refresh at setup. Kept below RELAY_FAILURE_THRESHOLD so setup
# alone can't trigger an automatic restart or a gateway re-election. Missing entities are created
# later anyway (see sensor.py's _async_ensure_sensors_exist()).
SOFT_REFRESH_RETRY_ATTEMPTS = 1  # in addition to the first attempt
SOFT_REFRESH_RETRY_DELAY = 3.0  # seconds

# How often each tank re-checks its mesh membership and member connection
# info (see __init__.py's _async_revalidate_tank()). This is also how devices
# without a known mesh address, or a tank without a gateway, recover. A failed
# check is retried on the next run.
TANK_REVALIDATION_INTERVAL = timedelta(minutes=1)

# A tank's clock is set when any of its devices drifts more than this many
# seconds from Home Assistant's clock, or reports a different time zone
# (checked after every poll, see __init__.py's _async_check_tank_time()).
CLOCK_DRIFT_THRESHOLD = 60

# Minimum time between two automatic clock syncs of a tank, so a device that
# doesn't take the time can't cause a write on every poll.
TIME_SYNC_COOLDOWN = timedelta(minutes=5)

# --------------------------------------------------------------------------
# Tank config entries: one per Thread mesh ("tank"), not one per device:
#   {CONF_PAN_ID: 0x1234, CONF_MLPREFIX: "fd1122...", CONF_DEVICES: [
#       {CONF_SERIAL: "765...", CONF_ADDRESS: "AA:BB:..."}, ...
#   ]}
# A device without a tank ("ad-hoc") still uses a one-element CONF_DEVICES.
# --------------------------------------------------------------------------

# List of {CONF_SERIAL, CONF_ADDRESS} dicts.
CONF_DEVICES = "devices"

# The tank's 8-byte mesh-local prefix as hex (see python-mobius
# discover_tank()). Used as the config entry unique_id and the tank device
# identifier (tank_device_identifier()). Absent for ad-hoc entries.
CONF_MLPREFIX = "mlprefix"

# --------------------------------------------------------------------------
# Frontend (Lovelace cards), see frontend/__init__.py.
# --------------------------------------------------------------------------

_MANIFEST_PATH = _Path(__file__).parent / "manifest.json"
with open(_MANIFEST_PATH, encoding="utf-8") as _f:
    INTEGRATION_VERSION: str = _json.load(_f).get("version", "0.0.0")

# URL path the card files are served from.
URL_BASE = "/mobius_frontend"

# One entry per compiled card. The version is appended to the URL so browsers
# reload the cards after an update.
JSMODULES: list[dict[str, str]] = [
    {"name": "Mobius Schedule Card", "filename": "mobius-schedule-card.js", "version": INTEGRATION_VERSION},
    {"name": "Mobius Scene Card", "filename": "mobius-scene-card.js", "version": INTEGRATION_VERSION},
]
