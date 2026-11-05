"""Home Assistant 2026.11+ renames an entity in place when its entity ID
changes, unless a custom integration's class has its own add/remove code
and hasn't opted in with async_entity_id_changed: those are removed and
added again."""

import importlib
import pkgutil

import pytest
from homeassistant.helpers import entity as entity_helper
from homeassistant.helpers.entity import Entity

import custom_components.mobius as mobius

requires_readd = getattr(entity_helper, "_entity_class_requires_readd", None)


def _entity_classes() -> list[type]:
    for module in pkgutil.iter_modules(mobius.__path__):
        importlib.import_module(f"{mobius.__name__}.{module.name}")
    found, todo = [], [Entity]
    while todo:
        for sub in todo.pop().__subclasses__():
            todo.append(sub)
            if sub.__module__.startswith(mobius.__name__ + "."):
                found.append(sub)
    return found


@pytest.mark.skipif(requires_readd is None, reason="Home Assistant before 2026.11")
def test_every_entity_is_renamed_in_place():
    classes = _entity_classes()
    assert len(classes) > 20
    assert [cls.__qualname__ for cls in classes if requires_readd(cls)] == []
