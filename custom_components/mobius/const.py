"""Constants for the Mobius integration."""

from datetime import timedelta

DOMAIN = "mobius"

# Two polling tiers, per the design discussion in documentation/: telemetry
# is cheap (a couple of small GATT reads), schedule fetches are expensive
# (multiple round trips for potentially many points), and schedules don't
# change minute-to-minute anyway.
#
# NOTE on FAST_POLL_INTERVAL: this coordinator does a full connect -> read ->
# disconnect cycle EVERY poll (see coordinator.py) rather than holding a
# persistent connection open, unlike the official Mobius app (which likely
# keeps one connection open while actively viewing and just re-reads over
# it, hence its ~2s refresh). At 10s, that means real BLE connect/disconnect
# handshake overhead 6x more often than the previous 60s default. If this
# reintroduces connection instability, a persistent-connection architecture
# (keep one MobiusDevice connected per device across poll cycles, only
# reconnecting on failure) would be the proper fix rather than backing this
# off again -- not implemented yet.
FAST_POLL_INTERVAL = timedelta(seconds=10)
SLOW_POLL_INTERVAL = timedelta(minutes=10)

# How many devices this integration will connect to simultaneously across
# all config entries. Even 4 devices showed real BLE connection instability
# during development of the underlying python-mobius library -- this caps
# concurrent connection attempts rather than firing them all at once.
# (Was briefly lowered to 1 while diagnosing "no available connection slot"
# errors on real hardware -- that turned out to be a real connection leak in
# python-mobius's connect() on a start_notify() failure path, now fixed,
# not slot contention from concurrency. Reverted back to 2.)
MAX_CONCURRENT_CONNECTIONS = 2

CONNECT_TIMEOUT = 30.0
