#!/usr/bin/env python3
"""
Create/refresh the per-language ``.po`` files from ``messages.pot``.

Uses Babel instead of GNU ``msgmerge``/``msginit`` so it works on every
platform. Existing translations are preserved; new msgids are appended with an
empty ``msgstr`` and must be filled in by hand.
"""

from __future__ import annotations

from pathlib import Path

from babel.messages import Catalog
from babel.messages.pofile import read_po, write_po

ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = ROOT / "src" / "novel_downloader" / "locales"
POT_FILE = LOCALES_DIR / "messages.pot"

LANGS = ("zh_CN",)


def main() -> int:
    if not POT_FILE.is_file():
        print("No messages.pot found. Run update_pot.py first.")
        return 1

    with POT_FILE.open("rb") as f:
        template = read_po(f)

    for lang in LANGS:
        po_file = LOCALES_DIR / lang / "LC_MESSAGES" / "messages.po"

        if po_file.is_file():
            with po_file.open("rb") as f:
                catalog = read_po(f, locale=lang)
            revision_date = catalog.revision_date
        else:
            print(f"{po_file} not found, creating a new catalog ...")
            catalog = Catalog(locale=lang)
            revision_date = None

        catalog.update(template, update_creation_date=False)
        catalog.creation_date = template.creation_date
        catalog.revision_date = revision_date or template.creation_date

        po_file.parent.mkdir(parents=True, exist_ok=True)
        with po_file.open("wb") as f:
            write_po(f, catalog, ignore_obsolete=True)
        print(f"Updated {po_file}")

    print("All locales updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
