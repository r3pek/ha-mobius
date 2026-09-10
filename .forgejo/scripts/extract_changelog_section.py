"""
Extracts the section of CHANGELOG.md under the heading matching a given
version (e.g. version "0.3.0" -> the content under "## 0.3.0", up to the
next "## " heading or end of file), and writes it to release_notes.md.

Used by .forgejo/workflows/release.yml to source Forgejo release notes
from CHANGELOG.md directly, rather than letting Forgejo auto-generate
notes from commits. Takes the tag name (e.g. "v0.3.0") as its one
argument and strips the leading "v" itself, since tags use that prefix
but CHANGELOG.md's own headings don't.

Pre-release tags (anything with a "-" after the version core, e.g.
"v0.8.0-beta1") fall back to the "## Unreleased" section if there's no
section matching the exact pre-release string -- a beta is normally cut
from whatever's currently drafted there, before it's finalized into its
own numbered heading, so requiring an exact "## 0.8.0-beta1" match would
fail nearly every pre-release for no real reason. A final release tag
(no "-") gets no such fallback: it still must have its own real,
already-written section, exactly as before.

Exits with a clear error (nonzero exit code) if no matching section is
found, rather than silently producing an empty or wrong release.
"""

import re
import sys


def _section(text: str, heading: str) -> str | None:
    pattern = re.compile(
        r"^## " + re.escape(heading) + r"\s*$\n(.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} <tag-name>")

    version = sys.argv[1].lstrip("v")
    text = open("CHANGELOG.md", encoding="utf-8").read()

    content = _section(text, version)
    if content is None and "-" in version:
        content = _section(text, "Unreleased")

    if content is None:
        raise SystemExit(
            f"No CHANGELOG.md section found for version {version!r} "
            f"(looked for a line starting with '## {version}')"
        )

    with open("release_notes.md", "w", encoding="utf-8") as f:
        f.write(content + "\n")


if __name__ == "__main__":
    main()
