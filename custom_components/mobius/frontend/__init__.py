"""
Registers the two Lovelace cards designed this session (schedule
editor, scene selection) as JavaScript modules Home Assistant serves
and auto-loads, without requiring a person to add a Lovelace resource
by hand -- see custom-card development guide at
https://developers.home-assistant.io/docs/frontend/custom-ui/custom-card
and the "embedding a card in an integration" pattern this follows.

Deliberately NOT declared as manifest.json dependencies ("frontend",
"http") -- both are core components almost always already loaded by
the time a custom integration's own async_setup() runs in a real HA
instance, but declaring them as hard dependencies forces them to be
FULLY set up (including their own component-level async_setup, not
just importable) before this integration's own setup can proceed at
all, and a failure there would then block this integration's entire
setup -- device control, sensors, everything -- over what is, for
this integration, a purely cosmetic, optional feature (a nicer
Lovelace card; the same devices and data are otherwise fully usable
through generic entities and cards without it at all). async_setup_component()
below is called directly and defensively instead, and every step here
is wrapped so a failure never propagates out to block the rest of
this integration's own setup.
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
    """Registers this integration's own Lovelace card JS modules."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.lovelace = self.hass.data.get("lovelace")

    async def async_register(self) -> None:
        """
        Best-effort only -- see this module's own docstring for why a
        failure anywhere in here must never propagate. A person who
        hits this (e.g. "http" itself somehow unavailable, which
        would be unusual) still gets a fully working integration,
        just without the nicer card auto-registered; the static path
        registered below still serves the file regardless, so a
        manual Lovelace resource (YAML mode, or storage mode where
        this auto-registration step itself failed) still works.
        """
        try:
            await self._async_register_path()
        except Exception:
            _LOGGER.exception("Failed to register Mobius frontend static path")
            return

        # Auto-adding the Lovelace resource itself only works in
        # storage mode -- YAML-mode dashboards require a person to
        # add the resource by hand (see this integration's own
        # documentation), since YAML-mode resources live in a config
        # file this integration has no business editing.
        if getattr(self.lovelace, "mode", getattr(self.lovelace, "resource_mode", "yaml")) == "storage":
            try:
                await self._async_wait_for_lovelace_resources()
            except Exception:
                _LOGGER.exception("Failed to register Mobius Lovelace card resources")

    async def _async_register_path(self) -> None:
        # "http" is a core component almost always already loaded by
        # this point in a real HA instance (see this module's own
        # docstring) -- async_setup_component() itself is a no-op if
        # so, and only actually does anything on the rarer path where
        # it genuinely isn't yet.
        if not await async_setup_component(self.hass, "http", {}):
            raise RuntimeError("http component could not be set up")
        try:
            await self.hass.http.async_register_static_paths(
                [StaticPathConfig(URL_BASE, str(Path(__file__).parent), False)]
            )
            _LOGGER.debug("Path registered: %s -> %s", URL_BASE, Path(__file__).parent)
        except RuntimeError:
            # Already registered -- happens on integration reload
            # within the same running HA instance, not an error.
            _LOGGER.debug("Path already registered: %s", URL_BASE)

    async def _async_wait_for_lovelace_resources(self) -> None:
        """
        Lovelace's own resource store may not have finished loading
        yet at the point this integration's own async_setup() runs
        (both happen early in HA's own startup, in an order this
        integration doesn't control) -- retries every 5s rather than
        registering against a not-yet-loaded store, which would
        silently do nothing.
        """
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
