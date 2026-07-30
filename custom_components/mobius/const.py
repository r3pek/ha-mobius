"""Constants for the Mobius integration."""

from datetime import timedelta

DOMAIN = "mobius"

# Two polling tiers, per the design discussion in documentation/: telemetry
# is cheap (a couple of small GATT reads), schedule fetches are expensive
# (multiple round trips for potentially many points), and schedules don't
# change minute-to-minute anyway.
FAST_POLL_INTERVAL = timedelta(seconds=60)
SLOW_POLL_INTERVAL = timedelta(minutes=10)

# How many devices this integration will connect to simultaneously across
# all config entries. Even 4 devices showed real BLE connection instability
# during development of the underlying python-mobius library -- this caps
# concurrent connection attempts rather than firing them all at once.
MAX_CONCURRENT_CONNECTIONS = 2

CONNECT_TIMEOUT = 30.0
