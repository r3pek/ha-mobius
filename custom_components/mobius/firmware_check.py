"""
Firmware checks against the Mobius cloud, shared by every device of every
entry (see update.py). A reply is kept per image and installed version for
FIRMWARE_CHECK_INTERVAL (or longer if the cloud asks for it); a failed
check keeps the last reply and is retried after FIRMWARE_RETRY_INTERVAL.
Changelogs are kept for as long as Home Assistant runs (their URLs include
the version).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from mobius import Changelog, FirmwareCheck, ImageId, firmware_check_url, parse_changelog, parse_firmware_check

from .const import DOMAIN, FIRMWARE_CHECK_INTERVAL, FIRMWARE_RETRY_INTERVAL

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15  # seconds


@dataclass
class _CachedCheck:
    check: Optional[FirmwareCheck]
    expires: float


class FirmwareChecker:
    """Asks the Mobius cloud whether newer firmware exists."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._checks: dict[tuple[int, str], _CachedCheck] = {}
        self._in_flight: dict[tuple[int, str], asyncio.Task] = {}
        self._changelogs: dict[str, Changelog] = {}

    def cached(self, image_id: int, version: str) -> Optional[FirmwareCheck]:
        """The last reply for this image and version, None if never checked."""
        cached = self._checks.get((image_id, version))
        return cached.check if cached else None

    async def async_check(self, image_id: int, version: str) -> Optional[FirmwareCheck]:
        """The reply for this image and version, from the cache while it is
        fresh. None if the check failed and there is no earlier reply."""
        key = (image_id, version)
        cached = self._checks.get(key)
        if cached is not None and time.monotonic() < cached.expires:
            return cached.check
        task = self._in_flight.get(key)
        if task is None:
            task = self._hass.async_create_task(self._async_fetch(key), eager_start=False)
            self._in_flight[key] = task
            task.add_done_callback(lambda _task: self._in_flight.pop(key, None))
        return await asyncio.shield(task)

    async def _async_fetch(self, key: tuple[int, str]) -> Optional[FirmwareCheck]:
        image_id, version = key
        previous = self._checks.get(key)
        url = firmware_check_url(ImageId(image_id), version)
        try:
            data = await self._async_get_json(url)
            check = parse_firmware_check(data)
        except Exception as err:
            _LOGGER.debug("Firmware check of image %s version %s failed: %s", image_id, version, err)
            self._checks[key] = _CachedCheck(
                previous.check if previous else None,
                time.monotonic() + FIRMWARE_RETRY_INTERVAL.total_seconds(),
            )
            return previous.check if previous else None
        interval = max(FIRMWARE_CHECK_INTERVAL.total_seconds(), check.interval or 0)
        self._checks[key] = _CachedCheck(check, time.monotonic() + interval)
        _LOGGER.debug(
            "Firmware check of image %s version %s: %s", image_id, version,
            f"{check.update.version} available" if check.update else "up to date",
        )
        return check

    async def async_changelog(self, url: str) -> Optional[Changelog]:
        """The changelog at `url`, None if it can't be read."""
        if url in self._changelogs:
            return self._changelogs[url]
        try:
            changelog = parse_changelog(await self._async_get_json(url))
        except Exception as err:
            _LOGGER.debug("Reading the firmware changelog %s failed: %s", url, err)
            return None
        self._changelogs[url] = changelog
        return changelog

    async def _async_get_json(self, url: str):
        session = async_get_clientsession(self._hass)
        async with asyncio.timeout(REQUEST_TIMEOUT):
            async with session.get(url) as response:
                response.raise_for_status()
                # The cloud doesn't always send a JSON content type.
                return await response.json(content_type=None)


def get_firmware_checker(hass: HomeAssistant) -> FirmwareChecker:
    """The integration-wide FirmwareChecker, created on first use."""
    data = hass.data.setdefault(DOMAIN, {})
    checker = data.get("firmware_checker")
    if checker is None:
        checker = data["firmware_checker"] = FirmwareChecker(hass)
    return checker
