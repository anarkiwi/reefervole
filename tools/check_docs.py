"""Verify that relative Markdown links in the repository's own docs resolve.

Only git-tracked files are checked: cached upstream material under gitignored paths is
not ours to validate.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

LINK = re.compile(r"\[[^\]]*\]\((?!https?://|#)([^)#]+)(?:#[^)]*)?\)")


def tracked_markdown(root: pathlib.Path) -> list[pathlib.Path]:
    """Every git-tracked Markdown file, as paths relative to the repository root."""
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "*.md"],
        capture_output=True,
        check=False,
        text=True,
    )
    if listing.returncode:
        raise SystemExit(f"git ls-files failed in {root}: {listing.stderr.strip()}")
    return [pathlib.Path(name) for name in listing.stdout.split("\0") if name]


def main() -> int:
    """Return 1 if any relative link is broken, else 0."""
    root = pathlib.Path(__file__).resolve().parent.parent
    broken = [
        (doc, target)
        for doc in tracked_markdown(root)
        for target in LINK.findall((root / doc).read_text(encoding="utf-8"))
        if not (root / doc.parent / target).exists()
    ]
    for doc, target in broken:
        print(f"{doc}: broken link -> {target}", file=sys.stderr)
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
