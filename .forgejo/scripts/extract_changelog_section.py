"""
Extracts the section of CHANGELOG.md under the heading matching a given
version (e.g. version "0.3.0" -> the content under "## 0.3.0", up to the
next "## " heading or end of file), and writes it to release_notes.md.

Used by .forgejo/workflows/release.yml to source Forgejo release notes
from CHANGELOG.md directly, rather than letting Forgejo auto-generate
notes from commits. Takes the tag name (e.g. "v0.3.0") as its one
argument and strips the leading "v" itself, since tags use that prefix
but CHANGELOG.md's own headings don't.

Exits with a clear error (nonzero exit code) if no matching section is
found, rather than silently producing an empty or wrong release.
"""

import re
import sys


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} <tag-name>")

    version = sys.argv[1].lstrip("v")
    text = open("CHANGELOG.md", encoding="utf-8").read()
    pattern = re.compile(
        r"^## " + re.escape(version) + r"\s*$\n(.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        raise SystemExit(
            f"No CHANGELOG.md section found for version {version!r} "
            f"(looked for a line starting with '## {version}')"
        )

    with open("release_notes.md", "w", encoding="utf-8") as f:
        f.write(match.group(1).strip() + "\n")


if __name__ == "__main__":
    main()
