"""Home Assistant test helpers from whichever test package is installed:
pytest-homeassistant (HA 2026.11 and later) or the retired
pytest-homeassistant-custom-component (up to HA 2026.10).

Import them from here, not from either package. Drop the fallback once
hacs.json's minimum is 2026.11."""

try:
    from pytest_homeassistant.common import MockConfigEntry, async_get_persistent_notifications
except ImportError:
    from pytest_homeassistant_custom_component.common import (  # type: ignore[no-redef]
        MockConfigEntry, async_get_persistent_notifications,
    )

__all__ = ["MockConfigEntry", "async_get_persistent_notifications"]
