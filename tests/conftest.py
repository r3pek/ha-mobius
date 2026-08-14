"""Shared pytest fixtures for ha-mobius tests."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


def _ensure_component_symlinked() -> None:
    """pytest-homeassistant-custom-component looks for custom integrations
    inside its own installed package's testing_config/custom_components/
    directory (see its common.py::get_test_config_dir()), not this repo.
    Symlink ours in automatically so `pytest` just works for any
    contributor without a manual setup step."""
    import pytest_homeassistant_custom_component as phacc

    plugin_dir = Path(phacc.__file__).parent
    target_dir = plugin_dir / "testing_config" / "custom_components"
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
    """Make Home Assistant's test harness discover custom_components/mobius."""
    yield
