"""Consistency checks for the shipped translation catalogs."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

# Imported through ``importorskip`` so the module is skipped without Babel
# (importing it at the top of the block would raise a collection error).
_babel_po = pytest.importorskip("babel.messages.pofile")
Catalog = _babel_po.Catalog
read_po = _babel_po.read_po

ROOT = Path(__file__).resolve().parents[2]
LOCALES_DIR = ROOT / "src" / "novel_downloader" / "locales"
PO_FILE = LOCALES_DIR / "zh_CN" / "LC_MESSAGES" / "messages.po"

#: msgids that are allowed to ship without a translation. Keep this empty;
#: add an entry only when a translation is genuinely blocked.
ALLOWED_UNTRANSLATED: set[str] = set()


def _load_script(name: str) -> ModuleType:
    """Import a repo script by path (``scripts/`` is not a package)."""
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_repo_script_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    with PO_FILE.open("rb") as f:
        return read_po(f)


@pytest.fixture(scope="module")
def translated_msgids(catalog: Catalog) -> set[str]:
    return {m.id for m in catalog if m.id and m.string and not m.fuzzy}


@pytest.fixture(scope="module")
def extracted_msgids() -> set[str]:
    """Every msgid the extractor finds in the source tree."""
    module = _load_script("update_pot")
    return {msgid for msgid, _loc, _flags in module._collect()}


def test_catalog_is_utf8(catalog: Catalog) -> None:
    assert catalog.charset is not None
    assert catalog.charset.lower() == "utf-8"


def test_every_extracted_msgid_is_in_catalog(
    catalog: Catalog, extracted_msgids: set[str]
) -> None:
    catalog_msgids = {m.id for m in catalog if m.id}
    missing = sorted(extracted_msgids - catalog_msgids)
    assert not missing, f"msgids missing from messages.po: {missing}"


def test_no_obsolete_entries(catalog: Catalog, extracted_msgids: set[str]) -> None:
    extra = sorted(m.id for m in catalog if m.id and m.id not in extracted_msgids)
    assert not extra, f"obsolete msgids in messages.po: {extra}"


def test_untranslated_entries_within_allowlist(
    catalog: Catalog, translated_msgids: set[str]
) -> None:
    untranslated = {m.id for m in catalog if m.id and m.id not in translated_msgids}
    unexpected = sorted(untranslated - ALLOWED_UNTRANSLATED)
    assert not unexpected, f"untranslated msgids: {unexpected}"
