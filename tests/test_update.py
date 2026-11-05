"""Firmware update entity (update.py) and the cloud checker
(firmware_check.py). The check replies are real ones from cloud.mobius.app."""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.helpers import entity_registry as er
from tests.common import MockConfigEntry

from mobius import FirmwareImage, FirmwareType, ImageId, MetadataSnapshot, Tank

from tests.fake_full_poll import full_poll_from_reads
from custom_components.mobius.const import DOMAIN, CONF_SERIAL, CONF_PAN_ID, CONF_DEVICES
from custom_components.mobius.firmware_check import FirmwareChecker, get_firmware_checker

PAN_ID = 0x3D0F
PUMP_SERIAL = "00000000000001"
FIRMWARE = "update.mp40_right_firmware_update"

DRIVER_URL = "https://cloud.mobius.app/firmware/mobius/check?version=3.0.34&image=2&format=json"
RADIO_URL = "https://cloud.mobius.app/firmware/mobius/check?version=2.1.36&image=10&format=json"
CHANGELOG_URL = (
    "https://cloud.mobius.app/firmware/mobius/download/2/0/1683/1.0.0/2026-07-17_14.17.56/3.0.35/changelog.json"
)
UP_TO_DATE = {"interval": 60}
DRIVER_UPDATE = {
    "url": "https://cloud.mobius.app/firmware/mobius/download/2/0/1683/1.0.0/2026-07-17_14.17.56/3.0.35/QuietDrive_Driver_3_0_35.otap",
    "manifest": "",
    "id": 1683,
    "version": "3.0.35",
    "md5": "46a8dcc54e0ef1718765ff6a99141d88",
    "release": "Public",
    "changelog": CHANGELOG_URL,
    "install": False,
    "reset": False,
    "interval": 60,
}
CHANGELOG = {
    "format_version": 1,
    "current_version": "3.0.35",
    "change_list": [
        {"version": "3.0.35", "changes": [{"text": "Quieter ramp-up"}], "alerts": [{"text": "Recalibrate after updating"}]},
        {"version": "3.0.34", "changes": [{"text": "Older fix"}], "alerts": []},
    ],
    "alerts": [],
}

PUMP_IMAGES = [
    FirmwareImage(FirmwareType.OS, ImageId.RfModuleK32w, "2.1.36"),
    FirmwareImage(FirmwareType.MainMicroOS, ImageId.QuietDriveDriver, "3.0.34"),
]


def _metadata(images):
    return MetadataSnapshot(
        advanced_features=None, calibration=None, hardware_info={}, firmware_versions={},
        supported_channels=[], error_state=None, epoch=None, local_time=None, tz_offset=None,
        firmware_images=images,
    )


def _pump(images=PUMP_IMAGES):
    device = MagicMock()
    device.serial = PUMP_SERIAL
    device.get_device_info = AsyncMock(return_value={
        "model_raw": 42, "model": "VorTechMP40wG3QD", "manufacturer": "EcoTech Marine",
        "name": "MP40 Right", "serial": PUMP_SERIAL,
        "primitive_type": "VorTechV1", "error_state": "NoError", "mac_address": None,
    })
    device.get_pump_telemetry = AsyncMock(return_value={
        "speed": 447, "speed_percent": 44.7, "gph": 2272, "gph_reliable": True,
        "minimum_gph": 200, "maximum_gph": 2500, "operation_mode": "Reefcrest",
    })
    device.get_battery_backup_info = AsyncMock(return_value=None)
    device.get_boosted_battery_info = AsyncMock(return_value=None)
    device.get_operation_state = AsyncMock()
    device.get_operation_state.return_value.name = "Schedule"
    point = MagicMock()
    point.time_minutes = 0
    point.pump.mode.name = "ReefCrest"
    point.pump.params = {}
    device.get_pump_schedule = AsyncMock(return_value=[point])
    device.get_current_pump_block = AsyncMock(return_value=point)
    device.get_metadata_batch = AsyncMock(return_value=_metadata(images))
    full_poll_from_reads(device)
    return device


def _cloud(replies):
    """Fake _async_get_json: `replies` maps URL to a reply, or to an
    exception to raise."""
    calls = []

    async def get_json(self, url):
        calls.append(url)
        reply = replies[url]
        if isinstance(reply, Exception):
            raise reply
        return reply

    return get_json, calls


@contextlib.asynccontextmanager
async def _setup(hass, device, replies):
    get_json, calls = _cloud(replies)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PAN_ID: PAN_ID, CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL, "address": "AA:AA:AA:AA:AA:01"}]},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=device),
    ), patch(
        "custom_components.mobius.discover_tank_for_serial", AsyncMock(return_value=Tank(prefix=None, peers=[])),
    ), patch(
        "custom_components.mobius.discover_mesh_address",
        AsyncMock(return_value=bytes.fromhex("fdaaaaaaaaaaaaaa000000fffe001234")),
    ), patch.object(FirmwareChecker, "_async_get_json", get_json):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        yield entry, calls


@pytest.mark.asyncio
async def test_newer_driver_firmware_is_shown_as_an_update(hass, hass_ws_client):
    replies = {RADIO_URL: UP_TO_DATE, DRIVER_URL: DRIVER_UPDATE, CHANGELOG_URL: CHANGELOG}
    async with _setup(hass, _pump(), replies):
        state = hass.states.get(FIRMWARE)
        assert state.state == STATE_ON
        assert state.attributes["installed_version"] == "Radio 2.1.36, Product OS 3.0.34"
        assert state.attributes["latest_version"] == "Radio 2.1.36, Product OS 3.0.35"
        assert state.attributes["release_summary"] == "Install this update with the Mobius app."
        assert state.attributes["device_class"] == "firmware"

        client = await hass_ws_client(hass)
        await client.send_json({"id": 1, "type": "update/release_notes", "entity_id": FIRMWARE})
        notes = (await client.receive_json())["result"]
        assert notes.startswith("Install this update with the Mobius app.")
        assert "## Product OS 3.0.35" in notes
        assert "> Recalibrate after updating" in notes
        assert "- Quieter ramp-up" in notes
        assert "Older fix" not in notes


@pytest.mark.asyncio
async def test_up_to_date_firmware(hass):
    async with _setup(hass, _pump(), {RADIO_URL: UP_TO_DATE, DRIVER_URL: UP_TO_DATE}):
        state = hass.states.get(FIRMWARE)
        assert state.state == STATE_OFF
        assert state.attributes["latest_version"] == state.attributes["installed_version"]
        assert state.attributes["release_summary"] is None


@pytest.mark.asyncio
async def test_failed_check_leaves_the_latest_version_unknown(hass):
    async with _setup(hass, _pump(), {RADIO_URL: OSError("offline"), DRIVER_URL: OSError("offline")}):
        assert hass.states.get(FIRMWARE).state == STATE_UNKNOWN


@pytest.mark.asyncio
async def test_single_part_device_shows_plain_versions(hass):
    images = [FirmwareImage(FirmwareType.MainMicroOS, ImageId.QuietDriveDriver, "3.0.34")]
    async with _setup(hass, _pump(images), {DRIVER_URL: DRIVER_UPDATE, CHANGELOG_URL: CHANGELOG}):
        state = hass.states.get(FIRMWARE)
        assert state.attributes["installed_version"] == "3.0.34"
        assert state.attributes["latest_version"] == "3.0.35"


@pytest.mark.asyncio
async def test_no_entity_for_models_without_firmware_updates(hass):
    async with _setup(hass, _pump(images=[]), {}):
        assert hass.states.get(FIRMWARE) is None


@pytest.mark.asyncio
async def test_updated_firmware_is_checked_again(hass):
    device = _pump()
    newer_driver_url = DRIVER_URL.replace("3.0.34", "3.0.35")
    replies = {RADIO_URL: UP_TO_DATE, DRIVER_URL: DRIVER_UPDATE, newer_driver_url: UP_TO_DATE}
    async with _setup(hass, device, replies) as (entry, calls):
        assert hass.states.get(FIRMWARE).state == STATE_ON

        # The driver was updated in the app: the next poll reports it.
        device.get_metadata_batch.return_value = _metadata([
            PUMP_IMAGES[0], FirmwareImage(FirmwareType.MainMicroOS, ImageId.QuietDriveDriver, "3.0.35"),
        ])
        await entry.runtime_data.coordinators[PUMP_SERIAL].async_refresh()
        await hass.async_block_till_done(wait_background_tasks=True)

        state = hass.states.get(FIRMWARE)
        assert state.state == STATE_OFF
        assert state.attributes["installed_version"] == "Radio 2.1.36, Product OS 3.0.35"
        assert calls.count(newer_driver_url) == 1


@pytest.mark.asyncio
async def test_renamed_entity_keeps_updating(hass):
    device = _pump()
    newer_driver_url = DRIVER_URL.replace("3.0.34", "3.0.35")
    replies = {RADIO_URL: UP_TO_DATE, DRIVER_URL: DRIVER_UPDATE, newer_driver_url: UP_TO_DATE}
    async with _setup(hass, device, replies) as (entry, calls):
        renamed = "update.pump_firmware"
        er.async_get(hass).async_update_entity(FIRMWARE, new_entity_id=renamed)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert hass.states.get(FIRMWARE) is None
        assert hass.states.get(renamed).state == STATE_ON

        device.get_metadata_batch.return_value = _metadata([
            PUMP_IMAGES[0], FirmwareImage(FirmwareType.MainMicroOS, ImageId.QuietDriveDriver, "3.0.35"),
        ])
        await entry.runtime_data.coordinators[PUMP_SERIAL].async_refresh()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert hass.states.get(renamed).state == STATE_OFF
        assert calls.count(newer_driver_url) == 1


@pytest.mark.asyncio
async def test_checker_shares_replies_between_devices(hass):
    get_json, calls = _cloud({DRIVER_URL: DRIVER_UPDATE})
    with patch.object(FirmwareChecker, "_async_get_json", get_json):
        checker = get_firmware_checker(hass)
        assert get_firmware_checker(hass) is checker
        first = await checker.async_check(int(ImageId.QuietDriveDriver), "3.0.34")
        second = await checker.async_check(int(ImageId.QuietDriveDriver), "3.0.34")
    assert first is second
    assert first.update.version == "3.0.35"
    assert calls == [DRIVER_URL]


@pytest.mark.asyncio
async def test_failed_check_keeps_the_last_reply(hass):
    replies = {DRIVER_URL: DRIVER_UPDATE}
    get_json, calls = _cloud(replies)
    with patch.object(FirmwareChecker, "_async_get_json", get_json):
        checker = FirmwareChecker(hass)
        good = await checker.async_check(int(ImageId.QuietDriveDriver), "3.0.34")
        checker._checks[(int(ImageId.QuietDriveDriver), "3.0.34")].expires = 0
        replies[DRIVER_URL] = OSError("offline")
        assert await checker.async_check(int(ImageId.QuietDriveDriver), "3.0.34") is good
    assert len(calls) == 2
