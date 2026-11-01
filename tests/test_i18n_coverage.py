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

A second guard section covers the desktop front end: every i18n key the
embedded HTML/JS can look up (``data-i18n`` attributes and literal ``t()``
calls in the boot script) must be published by the pywebview bridge's
``_i18n_catalog()``, because a key missing there falls back to English even
with a complete ``.po`` file.
"""

from __future__ import annotations

import ast
import re
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


# ---------------------------------------------------------------------------
# Front-end catalogue coverage (desktop pywebview bridge)
# ---------------------------------------------------------------------------
_ASSETS_FILE = PACKAGE_DIR / "apps" / "desktop" / "web_assets.py"
_BRIDGE_FILE = PACKAGE_DIR / "apps" / "desktop" / "webview_app.py"

#: ``<attr>="key"`` marks an element whose text/placeholder/title is localized
#: at boot by looking the key up in the injected catalogue.
_FRONTEND_ATTRS = ("data-i18n", "data-i18n-ph", "data-i18n-title")

#: A literal ``t("...")`` lookup in the boot script. The lookbehind rejects
#: identifier/method names ending in ``t`` (``closest(...)``, ``Text(...)``,
#: ...) so only the i18n helper matches.
_JS_T_CALL = re.compile(
    r'(?<![A-Za-z0-9_$.])t\("([^"]+)"\)'
    r"|(?<![A-Za-z0-9_$.])t\('([^']+)'\)"
)


def _frontend_keys() -> set[str]:
    """Every i18n key the embedded front end can ask the bridge to translate."""
    html = _ASSETS_FILE.read_text("utf-8")
    keys: set[str] = set()
    for attr in _FRONTEND_ATTRS:
        keys.update(re.findall(rf'{attr}="([^"]+)"', html))
    for double_quoted, single_quoted in _JS_T_CALL.findall(html):
        keys.add(double_quoted or single_quoted)
    return keys


def _bridge_catalog_keys() -> set[str]:
    """The keys published by the desktop bridge's ``_i18n_catalog``."""
    tree = ast.parse(_BRIDGE_FILE.read_text("utf-8"))
    catalog_fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_i18n_catalog"
    )
    ret = catalog_fn.body[-1]
    assert isinstance(ret, ast.Return) and isinstance(ret.value, ast.Dict)
    keys: set[str] = set()
    for key_node, value_node in zip(ret.value.keys, ret.value.values, strict=True):
        assert isinstance(key_node, ast.Constant), "catalog keys must be literals"
        assert (
            isinstance(value_node, ast.Call)
            and isinstance(value_node.func, ast.Name)
            and value_node.func.id == "t"
            and isinstance(value_node.args[0], ast.Constant)
        ), "catalog values must be literal t() calls so the extractor sees them"
        assert key_node.value == value_node.args[0].value, (
            f"catalog key {key_node.value!r} does not match its t() msgid "
            f"{value_node.args[0].value!r}; the front end would look up the "
            "key but the .po file would only translate the msgid"
        )
        keys.add(key_node.value)
    return keys


def test_frontend_keys_are_covered_by_bridge_catalog() -> None:
    frontend = _frontend_keys()
    assert frontend, "no front-end i18n keys found; check the web_assets scan"
    missing = sorted(frontend - _bridge_catalog_keys())
    assert not missing, (
        f"{len(missing)} front-end i18n key(s) are not published by "
        f"_i18n_catalog() in {_BRIDGE_FILE.name}, so the desktop UI shows "
        f"English even with a complete catalog:\n{_bulleted(missing)}"
    )
