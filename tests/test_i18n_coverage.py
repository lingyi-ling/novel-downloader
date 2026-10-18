#!/usr/bin/env python3
"""
tests.test_i18n_coverage
------------------------

Static guard against message ids that are handed to ``t("...")`` in the source
tree but never translated in the shipped ``zh_CN`` catalog.

The check is a pure, host-independent static scan: it re-extracts the very same
msgids as ``scripts/update_pot.py`` (same extractor, keyword and source roots)
and compares them against the entries of the ``.po`` file that actually carry a
usable translation. Nothing is imported from the package and no language is
resolved at runtime, so the result does not depend on ``NOVEL_LANG`` or on the
compiled ``.mo`` files.

This closes the three historical failure modes:

* a literal that the extractor stopped seeing (regression is caught by the
  extraction canary below),
* a msgid parked in a data structure so no ``t()`` call reaches it (covered
  because ``scripts/i18n_strings.py`` is part of the scan, exactly as in the
  template build),
* freshly added copy whose ``msgstr`` is still empty (or was marked fuzzy and
  therefore silently falls back to English).

Obsolete (extracted-nowhere) entries are intentionally *not* checked here: the
``.po`` maintenance flow (``update_pot.py`` + ``update_po.py``) owns their
cleanup and such a check would fight an in-progress sync.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Imported through ``importorskip`` so the module is skipped without Babel.
_babel_extract = pytest.importorskip("babel.messages.extract")
_babel_pofile = pytest.importorskip("babel.messages.pofile")
Catalog = _babel_pofile.Catalog
read_po = _babel_pofile.read_po
extract_from_dir = _babel_extract.extract_from_dir

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "src" / "novel_downloader"
SCRIPTS_DIR = ROOT / "scripts"
I18N_STRINGS_FILE = SCRIPTS_DIR / "i18n_strings.py"
PO_FILE = PACKAGE_DIR / "locales" / "zh_CN" / "LC_MESSAGES" / "messages.po"

# Mirror ``scripts/update_pot.py`` so the guard sees exactly what the template
# build sees; any drift there is surfaced by the canary test below.
_KEYWORDS = {"t": None}
_OPTIONS = {"encoding": "utf-8"}
_OPTIONS_MAP = {"**.py": _OPTIONS, "i18n_strings.py": _OPTIONS}
_SOURCES: tuple[tuple[Path, list[tuple[str, str]]], ...] = (
    (PACKAGE_DIR, [("**.py", "python")]),
    (SCRIPTS_DIR, [("i18n_strings.py", "python")]),
)

#: A msgid from ``scripts/i18n_strings.py``. It only lands in the extracted set
#: when both the extractor and the hand-maintained string list are picked up,
#: so it doubles as a "literals are still being extracted" canary.
_CANARY_MSGID = "Username"

_PO_REL = PO_FILE.relative_to(ROOT).as_posix()


def _bulleted(items: list[str]) -> str:
    """Render findings as an indented list for a readable assertion message."""
    return "\n".join(f"  - {item}" for item in items)


@pytest.fixture(scope="module")
def source_msgids() -> set[str]:
    """Every literal msgid the extractor finds in the source tree."""
    msgids: set[str] = set()
    for directory, method_map in _SOURCES:
        if not directory.is_dir():
            continue
        extracted = extract_from_dir(
            directory,
            method_map=method_map,
            options_map=_OPTIONS_MAP,
            keywords=_KEYWORDS,
        )
        for _filename, _lineno, message, _comments, _context in extracted:
            msgid = message[0] if isinstance(message, tuple) else message
            if msgid:
                msgids.add(msgid)
    assert msgids, "the extractor found no msgids; check the extraction config"
    return msgids


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    with PO_FILE.open("rb") as f:
        return read_po(f)


@pytest.fixture(scope="module")
def translated_msgids(catalog: Catalog) -> set[str]:
    """msgids with a non-empty, non-fuzzy ``msgstr`` (i.e. usable at runtime)."""
    return {m.id for m in catalog if m.id and m.string and not m.fuzzy}


def test_extractor_sees_hand_listed_strings(source_msgids: set[str]) -> None:
    assert _CANARY_MSGID in source_msgids, (
        f"{_CANARY_MSGID!r} (declared in scripts/i18n_strings.py) was not "
        "extracted. The extraction config or keyword set no longer matches "
        "scripts/update_pot.py."
    )


def test_every_source_msgid_is_translated(
    source_msgids: set[str], translated_msgids: set[str]
) -> None:
    missing = sorted(source_msgids - translated_msgids)
    assert not missing, (
        f"{len(missing)} msgid(s) passed to t() have no usable translation in "
        f"{_PO_REL} (missing, empty, or fuzzy msgstr):\n{_bulleted(missing)}"
    )


def test_catalog_has_no_fuzzy_entries(catalog: Catalog) -> None:
    fuzzy = sorted(str(m.id) for m in catalog if m.id and m.fuzzy)
    assert not fuzzy, (
        f"{len(fuzzy)} fuzzy entr(ies) in {_PO_REL}; a fuzzy msgstr is ignored "
        f"at runtime, causing a silent fallback to English:\n{_bulleted(fuzzy)}"
    )
