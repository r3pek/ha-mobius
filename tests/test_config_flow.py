"""Config flow tests, using real BLE advertisement bytes captured from
actual hardware during python-mobius development (see its
documentation/08-manufacturer-data.md)."""

import time

from bleak.backends.device import BLEDevice
from homeassistant import config_entries
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.const import CONF_ADDRESS
from homeassistant.data_entry_flow import FlowResultType

from custom_components.mobius.const import DOMAIN

# Real captured payload for a VorTech MP40QD pump (see python-mobius tests).
REAL_PUMP_PAYLOAD = bytes.fromhex("2a0001000000000f3d3736343935323231303539303139")
REAL_LIGHT_PAYLOAD = bytes.fromhex("b30001000000000f3d3756345a30304631343352424544")
MOBIUS_COMPANY_ID = 0x0202
PUMP_ADDRESS = "E4:89:1D:3C:C5:F1"
LIGHT_ADDRESS = "84:25:3F:AF:F0:C2"


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
    # Real model decoded from the real captured payload -- confirms the
    # config flow's use of mobius.parse_manufacturer_data() actually works.
    assert "VorTechMP40wG3QD" in result2["title"]


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


async def test_user_step_no_devices_found(hass):
    """Manual setup with nothing discovered yet should abort cleanly."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"
