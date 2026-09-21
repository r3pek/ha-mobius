"""
Serves the Lovelace cards (schedule editor, scene selection) and registers
them as dashboard resources, so they don't have to be added by hand.

Optional: every step is wrapped so a failure never affects the rest of the
integration. "http" is only an after_dependency in manifest.json, so a
problem there can't block the integration's setup.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.setup import async_setup_component

from ..const import JSMODULES, URL_BASE

_LOGGER = logging.getLogger(__name__)


class JSModuleRegistration:
    """Registers the integration's Lovelace card modules."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.lovelace = self.hass.data.get("lovelace")

    async def async_register(self) -> None:
        """Serves the card files and, for storage-mode dashboards, adds
        them as resources. Never raises; the served files can still be
        added as resources by hand."""
        try:
            await self._async_register_path()
        except Exception:
            _LOGGER.exception("Failed to register Mobius frontend static path")
            return

        # Only storage-mode resources can be added; YAML-mode resources are
        # configured by hand.
        if getattr(self.lovelace, "mode", getattr(self.lovelace, "resource_mode", "yaml")) == "storage":
            try:
                await self._async_wait_for_lovelace_resources()
            except Exception:
                _LOGGER.exception("Failed to register Mobius Lovelace card resources")

    async def _async_register_path(self) -> None:
        # A no-op when http is already set up.
        if not await async_setup_component(self.hass, "http", {}):
            raise RuntimeError("http component could not be set up")
        try:
            await self.hass.http.async_register_static_paths(
                [StaticPathConfig(URL_BASE, str(Path(__file__).parent / "dist"), False)]
            )
            _LOGGER.debug("Path registered: %s -> %s", URL_BASE, Path(__file__).parent / "dist")
        except RuntimeError:
            # Already registered (integration reload).
            _LOGGER.debug("Path already registered: %s", URL_BASE)

    async def _async_wait_for_lovelace_resources(self) -> None:
        """Registers the modules once the Lovelace resource store has
        loaded, checking every 5 s (registering earlier has no effect)."""
        async def _check_loaded(_now: Any) -> None:
            if self.lovelace is None:
                return
            if self.lovelace.resources.loaded:
                await self._async_register_modules()
            else:
                async_call_later(self.hass, 5, _check_loaded)

        await _check_loaded(None)

    async def _async_register_modules(self) -> None:
        _LOGGER.debug("Installing Mobius Lovelace card modules")
        existing_resources = [
            r for r in self.lovelace.resources.async_items()
            if r["url"].startswith(URL_BASE)
        ]

        for module in JSMODULES:
            url = f"{URL_BASE}/{module['filename']}"
            registered = False

            for resource in existing_resources:
                if self._path_only(resource["url"]) == url:
                    registered = True
                    if self._version_of(resource["url"]) != module["version"]:
                        _LOGGER.info("Updating %s to version %s", module["name"], module["version"])
                        await self.lovelace.resources.async_update_item(
                            resource["id"], {"res_type": "module", "url": f"{url}?v={module['version']}"},
                        )
                    break

            if not registered:
                _LOGGER.info("Registering %s version %s", module["name"], module["version"])
                await self.lovelace.resources.async_create_item(
                    {"res_type": "module", "url": f"{url}?v={module['version']}"}
                )

    @staticmethod
    def _path_only(url: str) -> str:
        return url.split("?")[0]

    @staticmethod
    def _version_of(url: str) -> str:
        parts = url.split("?")
        if len(parts) > 1 and parts[1].startswith("v="):
            return parts[1].removeprefix("v=")
        return "0"
