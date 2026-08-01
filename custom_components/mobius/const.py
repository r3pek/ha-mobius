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
