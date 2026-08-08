"""Config flow tests, using real BLE advertisement bytes captured from
actual hardware during python-mobius development (see its
documentation/08-manufacturer-data.md)."""

import time
from unittest.mock import patch, AsyncMock

import pytest
from bleak.backends.device import BLEDevice
from homeassistant import config_entries
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mobius.const import DOMAIN, CONF_SERIAL, CONF_PAN_ID, CONF_DEVICES, CONF_MLPREFIX, CONF_AGE
from mobius import Tank, MeshPeer, Model

# Real captured payload for a VorTech MP40QD pump (see python-mobius tests).
REAL_PUMP_PAYLOAD = bytes.fromhex("2a0001000000000f3d3736343935323231303539303139")
REAL_LIGHT_PAYLOAD = bytes.fromhex("b30001000000000f3d3756345a30304631343352424544")
MOBIUS_COMPANY_ID = 0x0202
PUMP_ADDRESS = "E4:89:1D:3C:C5:F1"
LIGHT_ADDRESS = "84:25:3F:AF:F0:C2"
# Serials decoded from the payloads above -- confirms unique_id ends up
# serial-based, not address-based.
PUMP_SERIAL = "76495221059019"
LIGHT_SERIAL = "7V4Z00F143RBED"
# Decoded pan_id from REAL_PUMP_PAYLOAD/REAL_LIGHT_PAYLOAD above -- both
# real captures share the same pan_id (same physical tank) -- deliberately
# reused for the multi-device tank tests below, not a coincidence.
PAN_ID = 0x3D0F

# A confirmed real 8-byte Thread mesh-local prefix (see python-mobius's own
# NetworkedThreadDevices real-hardware capture) -- used as this tank's
# CONF_MLPREFIX/unique_id in the tests below.
MLPREFIX = bytes.fromhex("fd1c5ec780e35c01")


def _make_discovery_info(address: str, payload: bytes) -> BluetoothServiceInfoBleak:
    device = BLEDevice(address, "MOBIUS", {})
    return BluetoothServiceInfoBleak(
        name="MOBIUS",
        address=address,
        rssi=-60,
        manufacturer_data={MOBIUS_COMPANY_ID: payload},
        service_data={},
        service_uuids=["01ff0100-ba5e-f4ee-5ca1-eb1e5e4b1ce0"],
        source="local",
        device=device,
        advertisement=None,
        connectable=True,
        time=time.monotonic(),
        tx_power=None,
    )


def _no_tank():
    """The common "connected fine, but this device isn't part of any
    provisioned Thread network" result -- falls back to the ad-hoc,
    single-device confirm flow."""
    return Tank(prefix=None, peers=[])


def _multi_device_tank():
    """A genuine 2-device tank -- the pump and light share PAN_ID, so
    this represents what a real discover_tank() call would find if
    asked from either one."""
    return Tank(
        prefix=MLPREFIX,
        peers=[
            MeshPeer(
                serial=PUMP_SERIAL, model_raw=42, model=Model.VorTechMP40wG3QD,
                short_address=0x1234, address=MLPREFIX + bytes.fromhex("000000fffe001234"),
            ),
            MeshPeer(
                serial=LIGHT_SERIAL, model_raw=179, model=Model.RadionXR15wG6Pro,
                short_address=0x5678, address=MLPREFIX + bytes.fromhex("000000fffe005678"),
            ),
        ],
    )


def _mock_tank_discovery(tank_or_none):
    """Patches discover_tank_for_serial() at its config_flow.py import
    location -- the actual connection attempt this whole flow now makes
    before it can show any confirm screen. Every test that reaches
    async_step_scan_tank() needs this, or it hits a real (test-
    environment-blocked) socket connection attempt."""
    return patch(
        "custom_components.mobius.config_flow.discover_tank_for_serial",
        AsyncMock(return_value=tank_or_none),
    )


async def test_bluetooth_discovery_creates_entry(hass):
    discovery_info = _make_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)

    with _mock_tank_discovery(_no_tank()):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=discovery_info
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "bluetooth_confirm"

        result2 = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
    assert result2["type"] == FlowResultType.CREATE_ENTRY
    # The actual point of moving to CONF_DEVICES: an ad-hoc entry still
    # stores address/serial, just nested under CONF_DEVICES now (one
    # entry, uniform shape with a real multi-device tank entry) instead
    # of flat top-level CONF_ADDRESS/CONF_SERIAL keys.
    assert result2["data"][CONF_DEVICES] == [{CONF_SERIAL: PUMP_SERIAL, CONF_ADDRESS: PUMP_ADDRESS}]
    # pan_id must still be stored -- decoded from the same real captured
    # advertisement, needed for later merge detection even for an ad-hoc
    # entry (a second device from the same tank showing up later should
    # still be able to find and merge into this entry).
    assert result2["data"][CONF_PAN_ID] == PAN_ID
    # No CONF_MLPREFIX for an ad-hoc entry -- there's no confirmed tank
    # prefix to store (see config_flow.py's own docstring).
    assert CONF_MLPREFIX not in result2["data"]
    # The actual point of the serial-based identity fix: unique_id is the
    # serial, not the address.
    assert result2["result"].unique_id == PUMP_SERIAL
    # Real model decoded from the real captured payload -- confirms the
    # config flow's use of mobius.parse_manufacturer_data() actually works.
    assert "VorTechMP40wG3QD" in result2["title"]
    # The actual point of this test: title uses serial for disambiguation,
    # not the (potentially unstable, see documentation/12-device-identity-
    # and-address-stability.md) MAC address.
    assert result2["title"] == f"VorTechMP40wG3QD ({PUMP_SERIAL})"
    assert PUMP_ADDRESS not in result2["title"]


async def test_bluetooth_discovery_light_title(hass):
    discovery_info = _make_discovery_info(LIGHT_ADDRESS, REAL_LIGHT_PAYLOAD)

    with _mock_tank_discovery(_no_tank()):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=discovery_info
        )
        result2 = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert "RadionXR15wG6Pro" in result2["title"]


async def test_duplicate_bluetooth_discovery_aborts(hass):
    discovery_info = _make_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)

    with _mock_tank_discovery(_no_tank()):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=discovery_info
        )
        await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})

        # Same address discovered again should abort, not create a duplicate entry.
        result2 = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=discovery_info
        )
    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "already_configured"


async def test_address_change_is_recognized_as_the_same_device(hass):
    """
    The actual regression this fix is for: a device already configured
    gets rediscovered under a DIFFERENT BLE address (simulating a real
    address change/rotation, confirmed to happen on real hardware during
    this project's development) but the SAME serial. Before this fix,
    unique_id was address-based, so this would have created a duplicate
    entry for what's actually the same physical device. Must abort
    instead.
    """
    original = _make_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)
    with _mock_tank_discovery(_no_tank()):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=original
        )
        await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})

        new_address = "F0:0D:BE:EF:CA:FE"  # a different address entirely
        rediscovered = _make_discovery_info(new_address, REAL_PUMP_PAYLOAD)  # same serial
        result2 = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=rediscovered
        )
    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "already_configured"


async def test_manual_setup_also_uses_serial_for_unique_id(hass):
    discovery_info = _make_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)

    with patch(
        "custom_components.mobius.config_flow.async_discovered_service_info",
        return_value=[discovery_info],
    ), _mock_tank_discovery(_no_tank()):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] == FlowResultType.FORM

        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_ADDRESS: PUMP_ADDRESS}
        )
        result3 = await hass.config_entries.flow.async_configure(result2["flow_id"], user_input={})

    assert result3["type"] == FlowResultType.CREATE_ENTRY
    assert result3["result"].unique_id == PUMP_SERIAL
    assert result3["data"][CONF_PAN_ID] == PAN_ID


async def test_bluetooth_discovery_aborts_without_manufacturer_data(hass):
    """
    The actual point of the fail-fast fix: if manufacturer data genuinely
    isn't available (neither the initial snapshot nor Home Assistant's own
    cache has it), the flow must abort rather than proceed with an
    address-based identity that could break later if the address changes
    before a serial is ever learned.
    """
    incomplete_info = _make_discovery_info(PUMP_ADDRESS, b"")

    with patch(
        "custom_components.mobius.config_flow.async_last_service_info",
        return_value=None,  # HA's cache doesn't have anything better either
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=incomplete_info
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "no_manufacturer_data"
    # No entry should exist at all -- not even an address-only one.
    assert len(hass.config_entries.async_entries(DOMAIN)) == 0


async def test_manual_setup_excludes_unidentifiable_devices(hass):
    """A device whose manufacturer data can't be parsed shouldn't even be
    offered in the manual-setup dropdown, rather than being offered and
    then failing on selection."""
    good_discovery = _make_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)
    unidentifiable_discovery = _make_discovery_info("FF:FF:FF:FF:FF:FF", b"")

    with patch(
        "custom_components.mobius.config_flow.async_discovered_service_info",
        return_value=[good_discovery, unidentifiable_discovery],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] == FlowResultType.FORM
    offered_addresses = list(result["data_schema"].schema[CONF_ADDRESS].container)
    assert PUMP_ADDRESS in offered_addresses
    assert "FF:FF:FF:FF:FF:FF" not in offered_addresses


async def test_manual_setup_excludes_already_configured_devices(hass):
    """
    The actual regression this fix is for: after switching unique_id to
    serial-based, the manual-setup filter was still comparing
    discovery.address against a set of unique_ids -- which now holds
    SERIAL numbers, not addresses. Comparing a MAC against a set of
    serials never matches, so an already-configured device kept showing
    up in the dropdown as if it were new. Confirm it's actually excluded
    now -- against the current CONF_DEVICES-based entry shape.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PAN_ID: PAN_ID, CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL, CONF_ADDRESS: PUMP_ADDRESS}]},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    already_configured = _make_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)
    new_device = _make_discovery_info(LIGHT_ADDRESS, REAL_LIGHT_PAYLOAD)

    with patch(
        "custom_components.mobius.config_flow.async_discovered_service_info",
        return_value=[already_configured, new_device],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] == FlowResultType.FORM
    offered_addresses = list(result["data_schema"].schema[CONF_ADDRESS].container)
    assert PUMP_ADDRESS not in offered_addresses  # already configured -- must not appear
    assert LIGHT_ADDRESS in offered_addresses  # genuinely new -- must appear


async def test_user_step_no_devices_found(hass):
    """Manual setup with nothing discovered yet should abort cleanly."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_stale_initial_discovery_refreshes_to_show_real_model(hass):
    """Reproduces a real reported bug: the initial BluetoothServiceInfoBleak
    passed to async_step_bluetooth can have empty/incomplete manufacturer
    data (e.g. matched via the local_name matcher before a scan-response
    merge completed), showing a generic "Mobius device (address)" title
    instead of the real model. Confirms the confirm-step refresh picks up
    fuller data once it's available in HA's Bluetooth manager cache."""
    incomplete_info = _make_discovery_info(PUMP_ADDRESS, b"")  # no usable payload yet
    # Real captured payload becomes available by the time we check again.
    complete_info = _make_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)

    with patch(
        "custom_components.mobius.config_flow.async_last_service_info",
        return_value=complete_info,
    ), _mock_tank_discovery(_no_tank()):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=incomplete_info
        )
        # The initial title (before refresh) would have been generic --
        # what matters is the confirm step's placeholders after refresh.
        assert "VorTechMP40wG3QD" in result["description_placeholders"]["name"]

        result2 = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
        assert result2["type"] == FlowResultType.CREATE_ENTRY
        assert "VorTechMP40wG3QD" in result2["title"]


async def test_confirm_refresh_never_downgrades_a_good_initial_snapshot(hass):
    """
    The actual real-hardware bug this guards against, the reverse
    direction of the test above: async_step_bluetooth() already
    guaranteed the initial self._discovery_info has real, parseable
    manufacturer data before ever reaching the confirm screen. But by
    the time that screen renders, a connection attempt to check for a
    tank has already happened (see async_step_scan_tank()), giving the
    device's own advertisement real time to rotate to a DIFFERENT
    payload -- and a real BLE device can legitimately send several
    different advertisement/scan-response payloads in rotation, not all
    of which necessarily carry manufacturer data every time. An earlier
    version of _refresh_discovery_info() would blindly overwrite
    self._discovery_info with whatever HA's Bluetooth cache most
    recently had for that address, even if that happened to be a
    worse, data-less snapshot -- silently downgrading a perfectly good
    title to the generic "Mobius device" fallback. A real screenshot
    showed exactly this: a discovered card and confirm dialog both
    showing bare "Mobius"/"Mobius device" for a device whose real model
    and serial were already known moments earlier.
    """
    complete_info = _make_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)
    # What HA's Bluetooth cache happens to have most recently for this
    # same address by the time the confirm screen renders -- a real,
    # legitimate advertisement from the same device, just one of its
    # other payload variants that doesn't carry manufacturer data.
    later_but_worse_info = _make_discovery_info(PUMP_ADDRESS, b"")

    with patch(
        "custom_components.mobius.config_flow.async_last_service_info",
        return_value=later_but_worse_info,
    ), _mock_tank_discovery(_no_tank()):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=complete_info
        )
        # The actual point: still shows the real model, not "Mobius device".
        assert "VorTechMP40wG3QD" in result["description_placeholders"]["name"]

        result2 = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
        assert result2["type"] == FlowResultType.CREATE_ENTRY
        assert "VorTechMP40wG3QD" in result2["title"]
        assert result2["result"].unique_id == PUMP_SERIAL


# --------------------------------------------------------------------------
# Tank-aware discovery -- see config_flow.py's own module docstring for
# the full merge/tank/ad-hoc decision tree these tests confirm.
# --------------------------------------------------------------------------

async def test_bluetooth_discovery_of_tank_shows_tank_confirm(hass):
    """The core new behavior: discovering more than one device on the
    same Thread mesh shows ONE "add tank with N devices" confirm, not a
    single-device confirm -- listing the actual devices found, not just
    a bare count."""
    discovery_info = _make_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)

    with _mock_tank_discovery(_multi_device_tank()):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=discovery_info
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "tank_confirm"
    assert result["description_placeholders"]["count"] == "2"
    # The actual point of this fix: the real devices found are listed,
    # not just asserted as a bare count.
    devices_text = result["description_placeholders"]["devices"]
    assert PUMP_SERIAL in devices_text
    assert LIGHT_SERIAL in devices_text
    assert "VorTechMP40wG3QD" in devices_text
    # A REAL regression this specific assertion guards against: "name"
    # is required in title_placeholders for Home Assistant's own
    # "Discovered" card (shown in Settings > Devices & Services BEFORE
    # this form is even opened) to show anything meaningful at all --
    # confirmed via HA's own developer docs that title_placeholders is
    # silently ignored entirely if it doesn't include "name", falling
    # back to the bare integration name. An earlier version of this
    # change dropped "name" while adding "count"/"devices", breaking
    # that card without touching this form's own description text at
    # all -- a real screenshot showed the resulting "Mobius"/"Mobius"
    # fallback before this was caught.
    assert "name" in result["description_placeholders"]
    assert "RadionXR15wG6Pro" in devices_text
    # The form itself offers a name field, pre-filled with a suggested
    # default -- not a bare confirm-only Yes/No.
    name_marker = next(k for k in result["data_schema"].schema if k == CONF_NAME)
    assert name_marker.default() == "Mobius Tank (2 devices)"


async def test_tank_confirm_uses_the_typed_name_as_the_entry_title(hass):
    """The actual point of adding a name field: a custom name typed on
    the tank_confirm form becomes the entry's own title, not always the
    auto-generated "Mobius Tank (N devices)" default."""
    discovery_info = _make_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)

    with _mock_tank_discovery(_multi_device_tank()):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=discovery_info
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_NAME: "Living Room Reef"}
        )

    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert result2["title"] == "Living Room Reef"


async def test_tank_confirm_creates_multi_device_entry(hass):
    """Confirming a tank creates ONE entry with every discovered peer's
    serial in CONF_DEVICES, CONF_MLPREFIX set to the tank's real prefix,
    and unique_id based on that prefix (not any single device's serial --
    a tank entry doesn't "belong" to whichever device happened to be
    discovered first)."""
    discovery_info = _make_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)

    with _mock_tank_discovery(_multi_device_tank()):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=discovery_info
        )
        result2 = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})

    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert result2["data"][CONF_PAN_ID] == PAN_ID
    assert result2["data"][CONF_MLPREFIX] == MLPREFIX.hex()
    stored_serials = {d[CONF_SERIAL] for d in result2["data"][CONF_DEVICES]}
    assert stored_serials == {PUMP_SERIAL, LIGHT_SERIAL}
    assert result2["result"].unique_id == MLPREFIX.hex()
    assert result2["title"] == "Mobius Tank (2 devices)"


async def test_tank_confirm_stores_age_per_peer_when_present(hass):
    """Each peer's own discovery-time age snapshot (see const.py's own
    CONF_AGE docstring) is stored alongside its serial when the
    underlying MeshPeer actually had one -- confirms the age isn't
    dropped, and isn't accidentally shared/mixed up between peers."""
    tank_with_ages = Tank(
        prefix=MLPREFIX,
        peers=[
            MeshPeer(
                serial=PUMP_SERIAL, model_raw=42, model=Model.VorTechMP40wG3QD,
                short_address=0x1234, address=MLPREFIX + bytes.fromhex("000000fffe001234"),
                age=373,
            ),
            MeshPeer(
                serial=LIGHT_SERIAL, model_raw=179, model=Model.RadionXR15wG6Pro,
                short_address=0x5678, address=MLPREFIX + bytes.fromhex("000000fffe005678"),
                age=8490,
            ),
        ],
    )
    discovery_info = _make_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)

    with _mock_tank_discovery(tank_with_ages):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=discovery_info
        )
        result2 = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})

    ages_by_serial = {d[CONF_SERIAL]: d.get(CONF_AGE) for d in result2["data"][CONF_DEVICES]}
    assert ages_by_serial[PUMP_SERIAL] == 373
    assert ages_by_serial[LIGHT_SERIAL] == 8490


async def test_single_peer_tank_falls_back_to_adhoc(hass):
    """A device connected fine and reported a real, valid tank prefix,
    but is the ONLY device on it -- functionally identical to "no tank"
    from the user's perspective (no via_device hub needed for just one
    device), so this falls back to the plain single-device confirm, not
    tank_confirm."""
    solo_tank = Tank(
        prefix=MLPREFIX,
        peers=[MeshPeer(
            serial=PUMP_SERIAL, model_raw=42, model=Model.VorTechMP40wG3QD,
            short_address=0x1234, address=MLPREFIX + bytes.fromhex("000000fffe001234"),
        )],
    )
    discovery_info = _make_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)

    with _mock_tank_discovery(solo_tank):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=discovery_info
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"


async def test_tank_discovery_connection_failure_falls_back_to_adhoc(hass):
    """discover_tank_for_serial() returning None (couldn't even reach the
    device right now, distinct from Tank(prefix=None, ...) -- see its own
    docstring) must still let setup proceed as ad-hoc, not get stuck or
    error out just because the tank-discovery connection attempt itself
    failed."""
    discovery_info = _make_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)

    with _mock_tank_discovery(None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=discovery_info
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"


async def test_merge_case_no_prompt_and_reloads_existing_entry(hass):
    """The actual point of the merge design: a device belonging to a
    pan_id that already has a configured tank entry gets silently added
    to it (no prompt at all) and that entry gets reloaded -- confirmed
    both that the flow itself aborts cleanly, and that the entry's own
    data was actually updated with the new device."""
    existing_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID, CONF_MLPREFIX: MLPREFIX.hex(),
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}],
        },
        unique_id=MLPREFIX.hex(),
    )
    existing_entry.add_to_hass(hass)

    new_device_discovery = _make_discovery_info(LIGHT_ADDRESS, REAL_LIGHT_PAYLOAD)  # same PAN_ID

    with patch(
        "custom_components.mobius.config_flow.discover_tank_for_serial",
        AsyncMock(side_effect=AssertionError("merge case must not attempt tank discovery")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=new_device_discovery
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "merged_into_tank"

    # The existing entry's own data was actually updated with the new device.
    updated_entry = hass.config_entries.async_get_entry(existing_entry.entry_id)
    stored_serials = {d[CONF_SERIAL] for d in updated_entry.data[CONF_DEVICES]}
    assert stored_serials == {PUMP_SERIAL, LIGHT_SERIAL}


async def test_already_configured_serial_does_not_trigger_merge(hass):
    """A device whose serial is ALREADY in an entry's device list must
    abort as already_configured, not attempt (or need) a merge --
    confirms the "already configured" check runs before, and takes
    priority over, the pan_id-based merge check."""
    existing_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID, CONF_MLPREFIX: MLPREFIX.hex(),
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}],
        },
        unique_id=MLPREFIX.hex(),
    )
    existing_entry.add_to_hass(hass)

    rediscovered = _make_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)  # already-configured serial
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=rediscovered
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_manual_setup_of_device_belonging_to_existing_tank_also_merges(hass):
    """The manual (async_step_user) path needs the same merge check as
    automatic discovery -- a device visible in the dropdown (its own
    serial isn't configured yet) can still belong to an already-tracked
    tank if a DIFFERENT device from that same tank was the one that
    originally triggered its automatic discovery."""
    existing_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID, CONF_MLPREFIX: MLPREFIX.hex(),
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}],
        },
        unique_id=MLPREFIX.hex(),
    )
    existing_entry.add_to_hass(hass)

    new_device_discovery = _make_discovery_info(LIGHT_ADDRESS, REAL_LIGHT_PAYLOAD)  # same PAN_ID

    with patch(
        "custom_components.mobius.config_flow.async_discovered_service_info",
        return_value=[new_device_discovery],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_ADDRESS: LIGHT_ADDRESS}
        )

    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "merged_into_tank"
    updated_entry = hass.config_entries.async_get_entry(existing_entry.entry_id)
    stored_serials = {d[CONF_SERIAL] for d in updated_entry.data[CONF_DEVICES]}
    assert stored_serials == {PUMP_SERIAL, LIGHT_SERIAL}


async def test_concurrent_discovery_of_same_new_tank_is_deduplicated(hass):
    """Two devices from the same brand-new (not-yet-configured) tank both
    triggering async_step_bluetooth() must not produce two competing
    "add tank" prompts -- the second flow aborts against the first one's
    still-in-progress provisional unique_id. Tested by parking the first
    flow at its confirm form (not yet submitted, but already registered
    as "in progress" under its provisional pan-scoped unique_id) rather
    than racing two genuinely concurrent tasks against a hanging mock --
    the actual mechanism under test (async_set_unique_id's own
    raise_on_progress=True) only cares that a flow is currently
    in-progress when the second one starts, not the precise interleaving
    that got it there."""
    pump_discovery = _make_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)
    light_discovery = _make_discovery_info(LIGHT_ADDRESS, REAL_LIGHT_PAYLOAD)

    with _mock_tank_discovery(_multi_device_tank()):
        result1 = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=pump_discovery
        )
        # Parked at the tank_confirm form -- not yet submitted, but this
        # flow instance is now genuinely "in progress" under its
        # provisional pan-scoped unique_id.
        assert result1["type"] == FlowResultType.FORM
        assert result1["step_id"] == "tank_confirm"

        result2 = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=light_discovery
        )

    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "already_in_progress"
