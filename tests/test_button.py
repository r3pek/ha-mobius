"""
End-to-end test: full config entry setup -> coordinators -> button
platform -> real Home Assistant button entity, plus direct async_press()
behavior tests (success and failure).

reboot() itself is python-mobius's own responsibility and already tested
there -- these tests confirm THIS integration wires it up correctly:
the entity exists with the right identity, async_press() resolves a
connection via the coordinator's own async_get_connected_device() (not
gated on the coordinator's own last-poll availability -- see
RebootButton.async_press()'s own docstring for why), and a failure
surfaces as HomeAssistantError rather than failing silently or raising
something a user wouldn't understand from the UI.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_ADDRESS
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from mobius import PrimitiveType, Tank, MetadataSnapshot

from custom_components.mobius.const import DOMAIN, CONF_SERIAL, CONF_PAN_ID, CONF_DEVICES
from custom_components.mobius.button import RebootButton

PAN_ID = 0x3D0F
PUMP_ADDRESS = "AA:AA:AA:AA:AA:01"
PUMP_SERIAL = "00000000000001"


def _fake_pump_device():
    device = MagicMock()
    device.serial = PUMP_SERIAL
    device.get_device_info = AsyncMock(return_value={
        "model_raw": 42, "model": "VorTechMP40wG3QD", "manufacturer": "EcoTech Marine",
        "name": "MP40QD Right", "serial": PUMP_SERIAL,
        "primitive_type": "VorTechV1", "error_state": "NoError", "mac_address": None,
    })
    device.get_pump_telemetry = AsyncMock(return_value={"speed": 447, "speed_percent": 44.7, "gph": 2272})
    device.get_operation_state = AsyncMock()
    device.get_operation_state.return_value.name = "Schedule"
    device.get_advanced_features = AsyncMock(return_value=None)
    device.identify_device_type = AsyncMock(return_value=PrimitiveType.VorTechV1)
    device.reboot = AsyncMock()

    fake_point = MagicMock()
    fake_point.pump.mode.name = "TidalSwell"
    fake_point.pump.params = {}
    device.get_pump_schedule = AsyncMock(return_value=[fake_point] * 11)
    device.get_current_pump_block = AsyncMock(return_value=fake_point)
    device.get_metadata_batch = AsyncMock(return_value=MetadataSnapshot(
        advanced_features=None,
        calibration=None,
        hardware_info={
            "Color": "Black", "Revision": 2, "ProductType": "VorTech", "RadioType": "QCA4020",
        },
        firmware_versions={
            "Radio": "4.0.21", "Radio Bootloader": "1.2",
            "Product OS": "2.1.5", "Product Bootloader": "1.0",
        },
        supported_channels=[], error_state=None, epoch=None, local_time=None, tz_offset=None,
    ))
    device.get_own_mesh_address = AsyncMock(
        return_value=bytes.fromhex("fdaaaaaaaaaaaaaa000000fffe001234")
    )
    return device


async def test_pump_entry_setup_creates_a_reboot_button(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID,
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL, CONF_ADDRESS: PUMP_ADDRESS}],
        },
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=_fake_pump_device()),
    ), patch(
        "custom_components.mobius.discover_tank_for_serial",
        AsyncMock(return_value=Tank(prefix=None, peers=[])),
    ), patch(
        "custom_components.mobius.discover_mesh_address",
        AsyncMock(return_value=bytes.fromhex("fdaaaaaaaaaaaaaa000000fffe001234")),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("button.mp40qd_right_reboot")
    assert state is not None
    assert state.attributes.get("device_class") == "restart"


async def test_async_press_calls_reboot_via_the_resolved_connection(hass):
    fake_device = _fake_pump_device()
    fake_coordinator = MagicMock()
    fake_coordinator.async_get_connected_device = AsyncMock(return_value=fake_device)
    fake_coordinator.hass = hass
    fake_coordinator.last_update_success = True

    button = RebootButton(fake_coordinator, PUMP_SERIAL, device_info=MagicMock())

    await button.async_press()

    fake_coordinator.async_get_connected_device.assert_awaited_once()
    fake_device.reboot.assert_awaited_once()


async def test_async_press_raises_homeassistanterror_on_connection_failure(hass):
    """No gateway currently available -- async_get_connected_device()
    itself already raises HomeAssistantError (see coordinator.py's own
    tests); confirms async_press() lets that through directly rather
    than wrapping it in a second, redundant error."""
    fake_coordinator = MagicMock()
    fake_coordinator.async_get_connected_device = AsyncMock(
        side_effect=HomeAssistantError("No gateway currently available for pan_id 0x3d0f")
    )
    fake_coordinator.hass = hass
    fake_coordinator.last_update_success = True

    button = RebootButton(fake_coordinator, PUMP_SERIAL, device_info=MagicMock())

    with pytest.raises(HomeAssistantError, match="No gateway currently available"):
        await button.async_press()


async def test_async_press_raises_homeassistanterror_on_reboot_failure(hass):
    """The device itself rejecting the write (or a connection drop mid-
    write) is a plain Exception from python-mobius's own reboot() --
    confirms this gets wrapped in a HomeAssistantError so it surfaces
    clearly through Home Assistant's own button-press UI, rather than
    an unhandled, unexplained exception."""
    fake_device = _fake_pump_device()
    fake_device.reboot = AsyncMock(side_effect=IOError("device returned FSCI status Failed setting attribute 6"))
    fake_coordinator = MagicMock()
    fake_coordinator.async_get_connected_device = AsyncMock(return_value=fake_device)
    fake_coordinator.hass = hass
    fake_coordinator.last_update_success = True

    button = RebootButton(fake_coordinator, PUMP_SERIAL, device_info=MagicMock())

    with pytest.raises(HomeAssistantError, match="Failed to reboot"):
        await button.async_press()
