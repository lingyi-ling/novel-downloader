#!/usr/bin/env python3
"""A single broken plugin must never disable the other sites."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from novel_downloader.plugins.registry import PluginRegistry

_PKG = "novel_downloader_test_plugins"


def _write_package(root: Path) -> None:
    sites = root / _PKG / "sites"
    for name in ("ok", "bad"):
        (sites / name).mkdir(parents=True, exist_ok=True)
        (sites / name / "__init__.py").write_text("", encoding="utf-8")
    (sites / "__init__.py").write_text("", encoding="utf-8")
    (sites / "ok" / "searcher.py").write_text("OK = True\n", encoding="utf-8")
    # A plugin with a missing dependency / syntax problem at import time.
    (sites / "bad" / "searcher.py").write_text(
        "raise ImportError('missing optional dependency')\n", encoding="utf-8"
    )


@pytest.fixture()
def broken_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _write_package(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    yield tmp_path
    for name in list(sys.modules):
        if name.startswith(_PKG):
            del sys.modules[name]


def test_enumeration_skips_a_broken_plugin(broken_package: Path) -> None:
    """``search(sites=None)`` loads every site; one bad one is skipped."""
    registry = PluginRegistry()
    registry._sources = [_PKG]

    registry._load_all_sites("searcher")  # must not raise

    assert f"{_PKG}.sites.ok.searcher" in sys.modules
    assert f"{_PKG}.sites.bad.searcher" not in sys.modules


def test_explicit_lookup_skips_a_broken_plugin(broken_package: Path) -> None:
    """A broken module for an explicitly requested site is not fatal either."""
    registry = PluginRegistry()
    registry._sources = [_PKG]

    registry._try_import_site("bad", "searcher")  # must not raise

    assert f"{_PKG}.sites.bad.searcher" not in sys.modules


def test_missing_module_for_another_kind_still_looks_further(
    broken_package: Path,
) -> None:
    """No ``client.py`` for a site is a normal miss, not a plugin failure."""
    registry = PluginRegistry()
    registry._sources = [_PKG]

    registry._try_import_site("ok", "client")  # must not raise
