"""Tests for .forgejo/scripts/extract_changelog_section.py -- loaded by
file path via importlib rather than a normal import, since a
dot-prefixed directory (.forgejo/) isn't a valid Python package path.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).parent.parent / ".forgejo" / "scripts" / "extract_changelog_section.py"
_spec = importlib.util.spec_from_file_location("extract_changelog_section", _SCRIPT_PATH)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
_section = _module._section


CHANGELOG = """\
# Changelog

## Unreleased

- Something not yet released.
- Another unreleased item.

## 0.7.0

- A released feature.

## 0.6.0

- An older feature.
"""


def test_finds_an_exact_matching_section():
    assert _section(CHANGELOG, "0.7.0") == "- A released feature."


def test_finds_unreleased_section():
    result = _section(CHANGELOG, "Unreleased")
    assert "Something not yet released" in result
    assert "Another unreleased item" in result


def test_returns_none_for_a_version_with_no_section():
    assert _section(CHANGELOG, "0.99.0") is None


def test_section_stops_at_the_next_heading():
    result = _section(CHANGELOG, "0.7.0")
    assert "An older feature" not in result


@pytest.mark.parametrize(
    "tag,expected_snippet",
    [
        ("v0.7.0", "A released feature"),  # exact match, no fallback needed
        ("v0.8.0-beta1", "Something not yet released"),  # pre-release -> falls back to Unreleased
        ("v0.8.0-rc.1", "Something not yet released"),  # pre-release -> falls back to Unreleased
    ],
)
def test_main_resolves_the_right_section_for_a_tag(tmp_path, monkeypatch, tag, expected_snippet):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["extract_changelog_section.py", tag])

    _module.main()

    notes = (tmp_path / "release_notes.md").read_text(encoding="utf-8")
    assert expected_snippet in notes


def test_main_does_not_fall_back_for_a_final_release_tag_with_no_section(tmp_path, monkeypatch):
    """The safety net a real, numbered release still gets: no fallback
    to Unreleased just because the tag has no "-" in it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["extract_changelog_section.py", "v0.99.0"])

    with pytest.raises(SystemExit, match="No CHANGELOG.md section found"):
        _module.main()
