"""Config flow tests, using real BLE advertisement bytes captured from
actual hardware during python-mobius development (see its
documentation/08-manufacturer-data.md)."""

import time

from bleak.backends.device import BLEDevice
from homeassistant import config_entries
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.const import CONF_ADDRESS
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mobius.const import DOMAIN, CONF_SERIAL

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


async def test_bluetooth_discovery_creates_entry(hass):
    discovery_info = _make_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=discovery_info
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"

    result2 = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert result2["data"][CONF_ADDRESS] == PUMP_ADDRESS
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

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=discovery_info
    )
    result2 = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert "RadionXR15wG6Pro" in result2["title"]


async def test_duplicate_bluetooth_discovery_aborts(hass):
    discovery_info = _make_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)

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
    from unittest.mock import patch

    discovery_info = _make_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)

    with patch(
        "custom_components.mobius.config_flow.async_discovered_service_info",
        return_value=[discovery_info],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] == FlowResultType.FORM

        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_ADDRESS: PUMP_ADDRESS}
        )

    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert result2["result"].unique_id == PUMP_SERIAL


async def test_bluetooth_discovery_aborts_without_manufacturer_data(hass):
    """
    The actual point of the fail-fast fix: if manufacturer data genuinely
    isn't available (neither the initial snapshot nor Home Assistant's own
    cache has it), the flow must abort rather than proceed with an
    address-based identity that could break later if the address changes
    before a serial is ever learned.
    """
    from unittest.mock import patch

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
    from unittest.mock import patch

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
    now.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ADDRESS: PUMP_ADDRESS, CONF_SERIAL: PUMP_SERIAL},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    from unittest.mock import patch

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
    from unittest.mock import patch

    incomplete_info = _make_discovery_info(PUMP_ADDRESS, b"")  # no usable payload yet
    # Real captured payload becomes available by the time we check again.
    complete_info = _make_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)

    with patch(
        "custom_components.mobius.config_flow.async_last_service_info",
        return_value=complete_info,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=incomplete_info
        )
        # The initial title (before refresh) would have been generic --
        # what matters is the confirm step's placeholders after refresh.
        assert "VorTechMP40wG3QD" in result["description_placeholders"]["name"]

        result2 = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
        assert result2["type"] == FlowResultType.CREATE_ENTRY
        assert "VorTechMP40wG3QD" in result2["title"]
