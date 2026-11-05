"""
Firmware update entities: whether the Mobius cloud has newer firmware for a
device. Home Assistant can't install it; the update is installed with the
Mobius app.

A device can have more than one firmware part (a VorTech's radio and
driver); the entity covers all of them, listing each part's version when
there is more than one ("Radio 2.1.36, Product OS 3.0.35").
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import FIRMWARE_RETRY_INTERVAL
from .coordinator import MobiusDeviceCoordinator
from .entity import async_add_device_entities
from .firmware_check import FirmwareChecker, get_firmware_checker

_LOGGER = logging.getLogger(__name__)

INSTALL_WITH_APP = "Install this update with the Mobius app."


def _format_versions(parts: list[tuple[str, str]]) -> Optional[str]:
    """"3.0.35" for one part, "Radio 2.1.36, Product OS 3.0.35" for more."""
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0][1]
    return ", ".join(f"{label} {version}" for label, version in parts)


class MobiusFirmwareUpdate(CoordinatorEntity[MobiusDeviceCoordinator], UpdateEntity):
    """Newer firmware for a device, from the Mobius cloud (see
    firmware_check.py). Checked when the device's firmware versions are
    first known or change, and then every FIRMWARE_RETRY_INTERVAL (the
    checker only asks the cloud again once its reply has expired)."""

    _attr_has_entity_name = True
    _attr_translation_key = "firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.CONFIG
    _attr_supported_features = UpdateEntityFeature.RELEASE_NOTES

    def __init__(self, coordinator: MobiusDeviceCoordinator, serial: str, device_info: DeviceInfo,
                 checker: FirmwareChecker) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self._checker = checker
        self._attr_unique_id = f"{serial}_firmware_update"
        self._attr_device_info = device_info
        self._checked_images: Optional[tuple] = None
        self._check_task: Optional[asyncio.Task] = None

    def _images(self) -> list[dict[str, Any]]:
        return (self.coordinator.data or {}).get("firmware_images") or []

    def _updates(self) -> list[tuple[dict[str, Any], Any]]:
        """(image, AvailableFirmware or None) per image, None when the
        image hasn't been checked yet."""
        return [
            (image, check.update if (check := self._checker.cached(image["image_id"], image["version"])) else None)
            for image in self._images()
        ]

    @property
    def available(self) -> bool:
        return super().available and bool(self._images())

    @property
    def installed_version(self) -> Optional[str]:
        return _format_versions([(image["label"], image["version"]) for image in self._images()])

    @property
    def latest_version(self) -> Optional[str]:
        images = self._images()
        if not images or all(self._checker.cached(i["image_id"], i["version"]) is None for i in images):
            return None
        return _format_versions([
            (image["label"], update.version if update else image["version"]) for image, update in self._updates()
        ])

    @property
    def release_summary(self) -> Optional[str]:
        return INSTALL_WITH_APP if any(update for _image, update in self._updates()) else None

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        # The cloud only offers newer versions.
        return latest_version != installed_version

    async def async_release_notes(self) -> Optional[str]:
        sections = [INSTALL_WITH_APP]
        multiple = len(self._images()) > 1
        for image, update in self._updates():
            if update is None:
                continue
            heading = f"{image['label']} {update.version}" if multiple else update.version
            sections.append(f"## {heading}")
            changelog = await self._checker.async_changelog(update.changelog_url) if update.changelog_url else None
            entries = changelog.newer_than(image["version"]) if changelog else []
            if not entries:
                sections.append("No changelog available.")
            for entry in entries:
                lines = [f"### {entry.version}"] if len(entries) > 1 or entry.version != update.version else []
                lines += [f"> {alert}" for alert in entry.alerts]
                lines += [f"- {change}" for change in entry.changes]
                if lines:
                    sections.append("\n".join(lines))
        return "\n\n".join(sections) if len(sections) > 1 else None

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Nothing this entity sets up depends on its entity ID, so Home
        Assistant (2026.11+) can rename it in place instead of removing and
        re-adding it."""
        super().async_entity_id_changed(old_entity_id)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_track_time_interval(
            self.hass, self._async_periodic_check, FIRMWARE_RETRY_INTERVAL,
        ))
        self.async_on_remove(self._cancel_check)
        self._schedule_check()

    @callback
    def _cancel_check(self) -> None:
        if self._check_task is not None:
            self._check_task.cancel()

    @callback
    def _handle_coordinator_update(self) -> None:
        images = tuple((i["image_id"], i["version"]) for i in self._images())
        if images != self._checked_images:
            self._schedule_check()
        super()._handle_coordinator_update()

    async def _async_periodic_check(self, _now=None) -> None:
        self._schedule_check()

    @callback
    def _schedule_check(self) -> None:
        if not self._images() or (self._check_task is not None and not self._check_task.done()):
            return
        self._check_task = self.hass.async_create_background_task(
            self._async_check(), f"mobius firmware check {self._serial}",
        )

    async def _async_check(self) -> None:
        images = self._images()
        await asyncio.gather(*(self._checker.async_check(i["image_id"], i["version"]) for i in images))
        self._checked_images = tuple((i["image_id"], i["version"]) for i in images)
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """One firmware update entity per device the app has firmware updates
    for (created once the device has been read)."""
    checker = get_firmware_checker(hass)

    def build(coordinator, serial, device_info, data) -> list[UpdateEntity]:
        if not data.get("firmware_images"):
            return []
        return [MobiusFirmwareUpdate(coordinator, serial, device_info, checker)]

    async_add_device_entities(hass, entry, async_add_entities, build)
