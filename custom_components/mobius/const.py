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

# One unified polling tier -- with a
# persistent connection (direct for the gateway, relayed-but-still-
# persistent for everyone else) and cached mesh addresses, a single read
# covering both status and schedule data each cycle is fast enough not to
# need splitting; schedule data changing rarely doesn't by itself justify
# a separate slower tier if fetching it isn't meaningfully more costly.
POLL_INTERVAL = timedelta(seconds=30)

# How many NEW connection attempts (gateway connect/reconnect, or a
# relayed device's mesh address discovery) this integration will allow
# in flight at once, across all config entries.
#
# Deliberately serialized to 1, not a larger number. The semaphore only
# throttles the brief window an attempt is actually connecting -- once a
# gateway's connection succeeds, it's held open persistently and doesn't
# occupy a permit anymore, so it isn't visible to this limit at all. That
# matters because some Bluetooth transports have their own hard ceiling
# on truly simultaneous connections independent of this integration's own
# throttling -- confirmed via real-world testing against an ESPHome
# Bluetooth proxy (a 3-connection hardware limit) that gateway
# connections kept flapping because a value of 2 here meant an already-
# open gateway connection (1, invisible to the semaphore) plus 2 more
# concurrent discovery attempts (allowed by the semaphore) could reach
# exactly the proxy's ceiling with zero headroom, causing spurious
# failures unrelated to the gateway's own health. Serializing to 1 keeps
# at most one NEW attempt in flight on top of any already-open
# connections, rather than trying to guess a larger number that happens
# to leave enough headroom for a specific proxy's real limit.
#
# Note this doesn't fully solve a multi-tank setup, where more than one
# pan_id group means more than one persistent gateway connection held
# open at once, each invisible to this semaphore the same way -- with
# enough simultaneous tanks, even one new discovery attempt on top of
# several already-open gateways could still reach a small proxy's limit.
# Not addressed here; flagging as a known constraint rather than solving
# for hardware limits this integration doesn't know about.
MAX_CONCURRENT_CONNECTIONS = 1

CONNECT_TIMEOUT = 30.0

# Multiple devices sharing the same pan_id (Thread mesh/"tank", confirmed
# via reverse engineering the app's own tank-grouping model -- see
# python-mobius's documentation/09-thread-coap-relay.md) share ONE
# physical BLE connection rather than each holding their own -- see
# gateway_registry.py.

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

# Consecutive failed RELAYED reads to one specific target -- through an
# otherwise-healthy gateway -- that trigger forcing a different gateway,
# even though GATEWAY_FAILURE_THRESHOLD above hasn't been reached (the
# gateway's own direct reads may be succeeding the whole time). A real,
# confirmed production incident is what this addresses: a gateway can be
# perfectly healthy for its own reads, and for relaying to SOME other
# group members, while persistently failing to relay to one specific
# target for 40+ minutes straight -- something wrong with the gateway's
# own route to that one target specifically, at the Thread mesh level,
# not the gateway's own health in the way this integration could
# otherwise detect. Confirmed, not assumed: reverse-engineered the real
# app's own decompiled source specifically looking for a mesh-rebuild or
# network-reset command it could fall back to for exactly this situation
# -- there isn't one. The only Thread-network-creation code in the whole
# app is a one-time, destructive initial-provisioning sequence (starting
# with an actual factory reset) run once when a tank is first set up,
# never something triggered at runtime to recover a misbehaving mesh.
# Forcing a different gateway -- which might have its own, different,
# working route to the same target -- is the best lever actually
# available, not a first choice among several.
#
# A separate counter from GATEWAY_FAILURE_THRESHOLD's own, deliberately:
# conflating "the gateway's own reads are failing" with "the gateway
# can't relay to one specific member" would muddy which of two, genuinely
# different problems actually happened when read back from logs later.
RELAY_FAILURE_THRESHOLD = 3

# How long any single device (gateway or relayed) can go without a
# successful read before its entities are marked unavailable. The general
# backstop for every device -- GATEWAY_FAILURE_THRESHOLD above is a
# faster, gateway-specific optimization that tries to avoid ever reaching
# this for an entire group at once.
MARK_UNAVAILABLE_AFTER = timedelta(minutes=5)

# For a relayed (non-gateway) device's own soft, non-blocking refresh at
# setup -- see __init__.py's own async_setup_entry() for why that's soft
# in the first place. A single attempt turned out to fail transiently
# often enough in real production use (especially right after a Home
# Assistant restart, when the Bluetooth cache itself may not be warm yet)
# that the device's own type-specific sensor entities (which sensor.py's
# own async_setup_entry() decides whether to create from THAT ONE
# attempt's own data) never got created for the whole session -- see
# _async_ensure_sensors_exist()'s own docstring for the self-healing
# half of this fix. This constant is the OTHER half: giving that first
# attempt a real, but bounded, chance to actually succeed before setup
# moves on, so the self-healing check has as little to actually clean up
# afterward as possible.
#
# Deliberately just 1 -- not unlimited, and specifically NOT enough to
# reach RELAY_FAILURE_THRESHOLD (3) on its own: this setup-time retry
# burst calls the same coordinator.async_refresh() a normal poll cycle
# does, which counts toward that same threshold underneath. Confirmed
# via a real test failure that 3 retries here (matching that threshold
# exactly) can trigger a gateway re-election during setup itself, from
# nothing more than this retry burst -- a few failures within several
# seconds of each other isn't the same kind of evidence as 3 genuinely
# independent, time-separated poll cycles noticing the same thing, and
# shouldn't count the same way. The self-healing check is the real,
# primary fix for the actual problem this addresses -- this is cheap,
# bounded insurance on top of it, not a second mechanism trying to fully
# solve the same thing on its own.
SOFT_REFRESH_RETRY_ATTEMPTS = 1  # on top of the first attempt, so 2 total
SOFT_REFRESH_RETRY_DELAY = 3.0  # seconds between attempts

# How often each tank re-checks who's actually on its own Thread mesh
# AND refreshes what every member's own connection info looks like right
# now (mesh address, mesh last-seen -- see __init__.py's own
# _async_revalidate_tank()) -- reusing its existing gateway connection
# rather than opening a new one where one's already open. Used to be
# much less frequent and purely about migration detection -- confirmed
# via a real production issue that a much shorter interval matters for a
# second, at-least-as-important reason: a device whose own mesh address
# was never successfully discovered (or a tank that's lost its gateway
# entirely) has no way back in without this task actively retrying, and
# the whole reason a device relays through a gateway at all is to
# recover from exactly this kind of connectivity hiccup -- a recovery
# path that only checks once every 12 hours isn't really a recovery path
# in practice. Still can't be arbitrarily fast (each check costs a real,
# if usually already-open, BLE connection, and gateway migration itself
# is a rare, deliberate, physical event), so this is a floor: frequent
# enough that "stuck for hours" isn't a realistic outcome, without
# hammering the connection on every single poll cycle. A single failed
# check isn't itself actionable (see the same reasoning
# GATEWAY_FAILURE_THRESHOLD's own docstring gives for why one bad read
# shouldn't trigger anything) -- it's just skipped, with the next
# scheduled run acting as its own retry, same as before.
TANK_REVALIDATION_INTERVAL = timedelta(minutes=1)

# How often each tank's own clock gets nudged back to the current time --
# see _async_sync_tank_time()'s own docstring for the full reasoning and
# confirmation behind this feature. An hour is frequent enough that a
# device's own drift never has room to become the kind of multi-minute
# mismatch that visibly desyncs schedule behavior (lights turning on at
# the wrong time relative to the rest of a tank), without writing to
# every device's own flash needlessly often for a problem that, once
# fixed, doesn't recur quickly on its own.
TANK_TIME_SYNC_INTERVAL = timedelta(hours=1)

# --------------------------------------------------------------------------
# Tank-level config entries -- one config entry per Thread mesh/"tank"
# (see gateway_registry.py's own docstring for why pan_id is the
# established local proxy for this), not one per device. A config
# entry's data looks like:
#   {CONF_PAN_ID: 0x1234, CONF_MLPREFIX: "fd1122...", CONF_DEVICES: [
#       {CONF_SERIAL: "765...", CONF_ADDRESS: "AA:BB:..."}, ...
#   ]}
# CONF_DEVICES is a list even for a single, tank-less ("ad-hoc") device --
# uniform shape rather than two different entry types to support.
# --------------------------------------------------------------------------

# Not a standard homeassistant.const constant -- each entry in
# CONF_DEVICES is itself a dict with CONF_SERIAL/CONF_ADDRESS keys (reuses
# the same two constants a single device's own data already used before
# this integration moved to tank-level entries). Deliberately does NOT
# carry python-mobius's own MeshPeer.age: real hardware testing (two
# consecutive discover_tank() scans against the same gateway, nothing
# else changing) showed values that both increased AND decreased between
# runs for the same physical device, disproving any time-since-last-seen
# interpretation. Combined with no actual evidence anywhere for what the
# field really represents (the name itself is just a plausible
# guess, never confirmed against real app/decompiled source), there's
# nothing honest left to display -- excluded entirely rather than shown unused.
CONF_DEVICES = "devices"

# Not a standard homeassistant.const constant -- the tank's own confirmed,
# stable identity (see python-mobius's mobius.discovery.discover_tank()):
# an 8-byte Thread mesh-local prefix, stored here as its hex string. Used
# as the tank config entry's unique_id, and as the synthetic tank
# device's own identifier (see __init__.py's _tank_device_identifier())
# for via_device grouping -- more stable than pan_id for this purpose,
# since pan_id is only ever meant to disambiguate at the BLE-advertisement
# level, not serve as a long-term stable identity. None (not present in
# entry.data at all) for an ad-hoc, tank-less entry, where there's no
# prefix to have discovered in the first place.
CONF_MLPREFIX = "mlprefix"

