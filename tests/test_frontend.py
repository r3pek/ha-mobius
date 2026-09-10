"""Tests for frontend/__init__.py's own JSModuleRegistration --
registers this integration's two Lovelace card JS files as a static
path Home Assistant serves, and (storage-mode dashboards only)
auto-adds them as Lovelace resources so a person never has to add the
resource by hand. See that module's own docstring for why every step
here is wrapped so a failure can never block the rest of this
integration's own setup."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.mobius.frontend import JSModuleRegistration
from custom_components.mobius.const import JSMODULES, URL_BASE


def _hass_with_lovelace(mode: str | None, resources_loaded: bool = True, existing_items=None):
    hass = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    if mode is None:
        hass.data = {}
    else:
        lovelace = MagicMock()
        lovelace.mode = mode
        lovelace.resources.loaded = resources_loaded
        lovelace.resources.async_items.return_value = existing_items or []
        lovelace.resources.async_create_item = AsyncMock()
        lovelace.resources.async_update_item = AsyncMock()
        hass.data = {"lovelace": lovelace}
    return hass


@pytest.mark.asyncio
async def test_registers_the_static_path():
    hass = _hass_with_lovelace("yaml")
    with patch("custom_components.mobius.frontend.async_setup_component", AsyncMock(return_value=True)):
        await JSModuleRegistration(hass).async_register()

    hass.http.async_register_static_paths.assert_awaited_once()
    call = hass.http.async_register_static_paths.call_args[0][0][0]
    assert call.url_path == URL_BASE


@pytest.mark.asyncio
async def test_static_path_failure_does_not_raise():
    """The core guarantee this module makes -- a failure here must
    never propagate out and block the rest of this integration's own
    setup, since the same async_setup() call also registers websocket
    commands and services this integration actually needs to function."""
    hass = _hass_with_lovelace("yaml")
    hass.http.async_register_static_paths = AsyncMock(side_effect=OSError("disk full"))

    with patch("custom_components.mobius.frontend.async_setup_component", AsyncMock(return_value=True)):
        await JSModuleRegistration(hass).async_register()  # must not raise


@pytest.mark.asyncio
async def test_already_registered_static_path_is_not_an_error():
    """RuntimeError specifically means "already registered" (a normal
    outcome on integration reload within the same running HA instance),
    not a real failure -- must be swallowed, not just caught-and-logged
    the same generic way an actual failure is."""
    hass = _hass_with_lovelace("storage")
    hass.http.async_register_static_paths = AsyncMock(side_effect=RuntimeError("already registered"))

    with patch("custom_components.mobius.frontend.async_setup_component", AsyncMock(return_value=True)):
        await JSModuleRegistration(hass).async_register()  # must not raise

    # Still proceeds to the Lovelace resource step afterward.
    hass.data["lovelace"].resources.async_create_item.assert_awaited()


@pytest.mark.asyncio
async def test_yaml_mode_never_touches_lovelace_resources():
    hass = _hass_with_lovelace("yaml")
    with patch("custom_components.mobius.frontend.async_setup_component", AsyncMock(return_value=True)):
        await JSModuleRegistration(hass).async_register()

    hass.data["lovelace"].resources.async_create_item.assert_not_called()
    hass.data["lovelace"].resources.async_update_item.assert_not_called()


@pytest.mark.asyncio
async def test_storage_mode_registers_every_not_yet_present_module():
    hass = _hass_with_lovelace("storage", existing_items=[])
    with patch("custom_components.mobius.frontend.async_setup_component", AsyncMock(return_value=True)):
        await JSModuleRegistration(hass).async_register()

    created_urls = {
        call.args[0]["url"] for call in hass.data["lovelace"].resources.async_create_item.call_args_list
    }
    assert created_urls == {f"{URL_BASE}/{m['filename']}?v={m['version']}" for m in JSMODULES}


@pytest.mark.asyncio
async def test_storage_mode_skips_already_current_modules():
    existing = [{"id": "r1", "url": f"{URL_BASE}/{m['filename']}?v={m['version']}"} for m in JSMODULES]
    hass = _hass_with_lovelace("storage", existing_items=existing)

    with patch("custom_components.mobius.frontend.async_setup_component", AsyncMock(return_value=True)):
        await JSModuleRegistration(hass).async_register()

    hass.data["lovelace"].resources.async_create_item.assert_not_called()
    hass.data["lovelace"].resources.async_update_item.assert_not_called()


@pytest.mark.asyncio
async def test_storage_mode_updates_a_stale_version():
    stale = [{"id": "r1", "url": f"{URL_BASE}/{JSMODULES[0]['filename']}?v=0.0.1"}]
    hass = _hass_with_lovelace("storage", existing_items=stale)

    with patch("custom_components.mobius.frontend.async_setup_component", AsyncMock(return_value=True)):
        await JSModuleRegistration(hass).async_register()

    hass.data["lovelace"].resources.async_update_item.assert_any_call(
        "r1", {"res_type": "module", "url": f"{URL_BASE}/{JSMODULES[0]['filename']}?v={JSMODULES[0]['version']}"},
    )


@pytest.mark.asyncio
async def test_module_registration_failure_does_not_raise():
    hass = _hass_with_lovelace("storage", existing_items=[])
    hass.data["lovelace"].resources.async_create_item = AsyncMock(side_effect=RuntimeError("store locked"))

    with patch("custom_components.mobius.frontend.async_setup_component", AsyncMock(return_value=True)):
        await JSModuleRegistration(hass).async_register()  # must not raise


@pytest.mark.asyncio
async def test_waits_for_lovelace_resources_to_finish_loading():
    hass = _hass_with_lovelace("storage", resources_loaded=False, existing_items=[])

    with patch("custom_components.mobius.frontend.async_setup_component", AsyncMock(return_value=True)), \
         patch("custom_components.mobius.frontend.async_call_later") as mock_call_later:
        await JSModuleRegistration(hass).async_register()

    mock_call_later.assert_called_once()
    hass.data["lovelace"].resources.async_create_item.assert_not_called()
