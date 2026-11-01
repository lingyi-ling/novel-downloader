#!/usr/bin/env python3
"""
Rebuild ``src/novel_downloader/locales/messages.pot`` from the source tree.

Uses Babel instead of GNU ``xgettext`` so the same command works on every
platform. The creation date of an existing template is preserved so repeated
runs are reproducible (safe for CI freshness checks).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from babel.messages import Catalog
from babel.messages.extract import extract_from_dir
from babel.messages.pofile import read_po, write_po

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = ROOT / "src" / "novel_downloader"
SCRIPTS_DIR = ROOT / "scripts"
OUT_FILE = PACKAGE_DIR / "locales" / "messages.pot"

_KEYWORDS = {"t": None}
_OPTIONS = {"encoding": "utf-8"}
_OPTIONS_MAP = {"**.py": _OPTIONS, "i18n_strings.py": _OPTIONS}
_SOURCES = (
    (PACKAGE_DIR, [("**.py", "python")], "src/novel_downloader"),
    (SCRIPTS_DIR, [("i18n_strings.py", "python")], "scripts"),
)
_FALLBACK_DATE = datetime(2025, 1, 1, tzinfo=UTC)

#: Matches the header line a template must carry to declare its creation date.
_DATE_DECLARED_RE = re.compile(r'^"POT-Creation-Date:', re.MULTILINE)


def _previous_creation_date() -> datetime:
    """Reuse the creation date the template itself declares, if any.

    Babel substitutes ``datetime.now()`` when the header block carries no
    ``POT-Creation-Date`` at all (e.g. hand-made templates), raises on a
    malformed one, and may store other non-datetime values as-is. Only a date
    the file actually declares, parsed into a real ``datetime``, is reused;
    everything else falls back so repeated runs stay byte-identical.
    """
    if not OUT_FILE.is_file():
        return _FALLBACK_DATE
    try:
        declared = _DATE_DECLARED_RE.search(
            OUT_FILE.read_text("utf-8", errors="replace")
        )
        with OUT_FILE.open("rb") as f:
            raw = read_po(f).creation_date
    except Exception:
        return _FALLBACK_DATE
    if declared and isinstance(raw, datetime):
        return raw
    return _FALLBACK_DATE


def _location(prefix: str, filename: str, lineno: int) -> tuple[str, int]:
    """Build a repo-relative, forward-slashed ``(file, line)`` reference."""
    return (f"{prefix}/{filename.lstrip('/')}", lineno)


def _flags(msgid: str) -> list[str]:
    """Flag brace-formatted strings, matching ``xgettext`` behaviour."""
    if "{" in msgid and "}" in msgid:
        return ["python-brace-format"]
    return []


def _collect() -> list[tuple[str, tuple[str, int], list[str]]]:
    """Extract ``(msgid, location, flags)`` triples, ordered by location."""
    entries: list[tuple[str, tuple[str, int], list[str]]] = []

    for dirname, method_map, prefix in _SOURCES:
        extracted = extract_from_dir(
            dirname,
            method_map=method_map,
            options_map=_OPTIONS_MAP,
            keywords=_KEYWORDS,
        )
        for filename, lineno, message, _comments, _context in extracted:
            msgid = message[0] if isinstance(message, tuple) else message
            if not msgid:
                continue
            loc = _location(prefix, filename, lineno)
            entries.append((msgid, loc, _flags(msgid)))

    entries.sort(key=lambda entry: entry[1])
    return entries


def main() -> int:
    catalog = Catalog(
        project="novel-downloader",
        msgid_bugs_address="https://github.com/lingyi-ling/novel-downloader/issues",
        copyright_holder="Novel Downloader contributors",
        charset="UTF-8",
    )
    creation_date = _previous_creation_date()
    catalog.creation_date = creation_date
    catalog.revision_date = creation_date

    for msgid, loc, flags in _collect():
        catalog.add(msgid, locations=[loc], flags=flags)

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUT_FILE.open("wb") as f:
        write_po(f, catalog, ignore_obsolete=True)

    print(f"Updated template: {OUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
