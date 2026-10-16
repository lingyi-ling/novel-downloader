#!/usr/bin/env python3
"""
Compile ``.po`` catalogs into ``.mo`` files.

Uses Babel instead of GNU ``msgfmt`` so it works on every platform. The
resulting ``.mo`` files are build artifacts (git-ignored); they are regenerated
by ``setup.py`` during packaging.
"""

from __future__ import annotations

from pathlib import Path

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po

ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = ROOT / "src" / "novel_downloader" / "locales"


def main() -> int:
    po_files = sorted(LOCALES_DIR.rglob("LC_MESSAGES/*.po"))
    if not po_files:
        print("No .po files found.")
        return 0

    for po_file in po_files:
        mo_file = po_file.with_suffix(".mo")
        with po_file.open("rb") as src:
            catalog = read_po(src)
        with mo_file.open("wb") as dst:
            write_mo(dst, catalog)
        print(f"Compiled {po_file} -> {mo_file}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
