"""Shared pytest fixtures for ha-mobius tests."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

try:
    # HA 2026.11 and later: pytest-homeassistant loads itself and finds
    # custom_components/ in the repo root (pytest.ini's pythonpath).
    import pytest_homeassistant  # noqa: F401
except ImportError:
    # Up to HA 2026.10: the retired pytest-homeassistant-custom-component.
    # Remove this branch with the fallback in tests/common.py.
    pytest_plugins = "pytest_homeassistant_custom_component"

    def _ensure_component_symlinked() -> None:
        """The old plugin looks for custom integrations inside its own
        package's testing_config/custom_components/, not this repo, so
        symlink ours in."""
        import pytest_homeassistant_custom_component as phacc

        target_dir = Path(phacc.__file__).parent / "testing_config" / "custom_components"
        link = target_dir / "mobius"
        source = Path(__file__).parent.parent / "custom_components" / "mobius"

        if link.is_symlink() and os.path.realpath(link) == os.path.realpath(source):
            return
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(source)

    _ensure_component_symlinked()

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        """The old plugin only loads custom integrations when asked."""
        yield


@pytest.fixture(autouse=True)
def every_device_advertising(request):
    """Tests run without Bluetooth advertisements. Unless a test is marked
    `no_advertisements` (or patches is_advertising itself), every device
    counts as advertising, so none is ever treated as absent."""
    if request.node.get_closest_marker("no_advertisements"):
        yield
        return
    with patch("custom_components.mobius.coordinator.is_advertising", return_value=True):
        yield
