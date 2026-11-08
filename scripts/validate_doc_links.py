#!/usr/bin/env python3
"""Validate relative Markdown links in a docs tree.

Checks that every relative link and image target in the given Markdown files
resolves to a real path on disk. External links (http/https/mailto) and
pure-fragment links are skipped. Anchors are not resolved against headings —
only the file target is checked.

Usage:
    python3 scripts/validate_doc_links.py                 # repo docs (default targets)
    python3 scripts/validate_doc_links.py README.md docs  # explicit paths

Exits 1 if any relative target is missing.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# [text](target) and ![alt](target); ignores titles in "target \"title\"" form.
LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")
SKIP_SCHEMES = ("http://", "https://", "mailto:", "tel:", "#")
DEFAULT_TARGETS = ["README.md", "PLAN.md", "STORIES.md", "docs"]


def md_files(targets: list[str]) -> list[Path]:
    found: list[Path] = []
    for t in targets:
        p = Path(t)
        if p.is_dir():
            found.extend(sorted(p.rglob("*.md")))
        elif p.is_file():
            found.append(p)
        else:
            print(f"  ! target does not exist: {t}")
    return found


def main(argv: list[str]) -> int:
    targets = argv[1:] or DEFAULT_TARGETS
    files = md_files(targets)
    if not files:
        print("no markdown files found")
        return 1

    broken: list[tuple[str, str]] = []
    checked = 0
    for f in files:
        text = f.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            target = match.group(1).strip()
            if not target or target.startswith(SKIP_SCHEMES):
                continue
            rel = target.split("#", 1)[0]
            if not rel:
                continue
            checked += 1
            resolved = (f.parent / rel).resolve()
            if not resolved.exists():
                broken.append((str(f), target))

    print(f"checked {checked} relative link(s) across {len(files)} file(s)")
    if broken:
        print(f"\n{len(broken)} broken link(s):")
        for src, target in broken:
            print(f"  {src} -> {target}")
        return 1

    print("all relative links resolve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
