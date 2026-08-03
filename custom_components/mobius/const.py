"""Constants for the Mobius integration."""

from datetime import timedelta

DOMAIN = "mobius"

# Not a standard homeassistant.const constant -- this integration stores
# the device's serial number in config entry data alongside CONF_ADDRESS,
# since serial (not BLE address) is the actual stable device identity --
# see python-mobius's documentation/12-device-identity-and-address-
# stability.md. CONF_ADDRESS is kept too, for display/debugging, but the
# connection/coordinator layer uses CONF_SERIAL exclusively for resolving
# and reconnecting to the device.
CONF_SERIAL = "serial"

# Not a standard homeassistant.const constant -- the pan_id identifying
# which Thread mesh/"tank" a device belongs to (see the gateway-grouping
# note above). Read from the same BLE advertisement manufacturer data as
# CONF_SERIAL, no connection needed. Not assumed permanently fixed -- a
# device can be physically moved to a different tank -- so this is
# re-checked on every reconnect (see coordinator.py) rather than only
# read once at initial setup.
CONF_PAN_ID = "pan_id"

# Two polling tiers, per the design discussion in documentation/: telemetry
# is cheap (a couple of small GATT reads), schedule fetches are expensive
# (multiple round trips for potentially many points), and schedules don't
# change minute-to-minute anyway.
#
# As of the persistent-connection rework (see coordinator.py's
# MobiusConnectionManager), both tiers share ONE long-lived BLE connection
# per device instead of connecting/disconnecting every poll -- so this 10s
# interval no longer means a full connect/disconnect handshake every time,
# just a read over an already-open connection in the common case.
FAST_POLL_INTERVAL = timedelta(seconds=10)
SLOW_POLL_INTERVAL = timedelta(minutes=10)

# How many devices this integration will attempt to CONNECT (or RECONNECT)
# simultaneously across all config entries. With persistent connections,
# this only actually matters at startup (initial connect for each device)
# and after a detected drop (reconnect) -- normal polling no longer
# connects at all, so this is a much rarer/cheaper thing to throttle than
# it used to be when every poll reconnected.
MAX_CONCURRENT_CONNECTIONS = 2

CONNECT_TIMEOUT = 30.0

# Multiple devices sharing the same pan_id (Thread mesh/"tank", confirmed
# via Tank/CommGroup in the decompiled app -- see python-mobius's
# documentation/09-thread-coap-relay.md) share ONE physical BLE
# connection rather than each holding their own -- see gateway_registry.py.

# How long a newly-forming group waits for other devices to also report
# in before finalizing gateway selection by RSSI. Only affects the very
# first time a group forms (e.g. HA startup with several config entries
# for the same tank loading around the same time) -- an established
# group's gateway is never displaced just because a better-signal device
# joins later, only by GATEWAY_FAILURE_THRESHOLD consecutive failures.
GATEWAY_ELECTION_SETTLE_SECONDS = 3.0

# Consecutive failed gateway poll cycles that trigger promoting a
# different group member to gateway. Deliberately much faster than the
# ~5-minute mark-unavailable threshold every individual device gets
# (MARK_UNAVAILABLE_AFTER below) -- a bad gateway takes its whole group
# down, so it's worth trying to route around quickly rather than waiting
# for the general backstop.
GATEWAY_FAILURE_THRESHOLD = 3

# How long any single device (gateway or relayed) can go without a
# successful read before its entities are marked unavailable. The general
# backstop for every device -- GATEWAY_FAILURE_THRESHOLD above is a
# faster, gateway-specific optimization that tries to avoid ever reaching
# this for an entire group at once.
MARK_UNAVAILABLE_AFTER = timedelta(minutes=5)
