"""Sanity check: the integration's manifest, imports, and basic module
structure are valid inside a real (test-harness) Home Assistant core."""

from homeassistant.loader import async_get_integration


async def test_manifest_loads(hass):
    integration = await async_get_integration(hass, "mobius")
    assert integration.domain == "mobius"
    assert integration.config_flow is True
    assert "bluetooth_adapters" in integration.dependencies
    assert any("python-mobius" in r for r in integration.requirements)


async def test_integration_modules_import(hass):
    integration = await async_get_integration(hass, "mobius")
    component = await hass.async_add_executor_job(integration.get_component)
    assert hasattr(component, "async_setup")
    assert hasattr(component, "async_setup_entry")
    assert hasattr(component, "async_unload_entry")
