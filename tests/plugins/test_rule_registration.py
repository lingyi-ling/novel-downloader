#!/usr/bin/env python3
"""
Registration tests: a rule-based source declared in JSON must become a normal
plugin, discoverable through the shared :data:`registrar`.

Fully offline -- only rule files written under ``tmp_path`` are used.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.plugins import registrar
from novel_downloader.plugins.base.parser import BaseParser
from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.rule import (
    ensure_registered,
    register_all,
    reload_rules,
)
from novel_downloader.plugins.rule.fetcher import RuleFetcher
from novel_downloader.plugins.rule.parser import RuleParser
from novel_downloader.plugins.rule.searcher import RuleSearcher
from novel_downloader.schemas import FetcherConfig, ParserConfig


@pytest.fixture(autouse=True)
def _isolated_rule_state() -> Iterator[None]:
    """Reload rules from the real locations before and after each test."""
    reload_rules()
    yield
    reload_rules()


def _write(directory: Path, name: str, payload: dict[str, Any]) -> None:
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


def _search_rule(site_key: str, *, priority: int = 33) -> dict[str, Any]:
    return {
        "site_key": site_key,
        "base_url": "https://demo.example",
        "hosts": ["demo.example"],
        "search": {
            "url": "https://demo.example/s?q={keyword}",
            "item": "div.result",
            "priority": priority,
            "fields": {"title": "h3", "url": {"attr": "href"}},
        },
        "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"},
    }


# --------------------------------------------------------------------------- #
# on-demand registration of a JSON source
# --------------------------------------------------------------------------- #
def test_registered_rule_searcher_is_found_by_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "rcreg.json", _search_rule("rcreg"))
    monkeypatch.setenv("NOVEL_RULES_DIR", str(tmp_path))
    reload_rules()

    assert ensure_registered("rcreg") is True

    searcher_cls = registrar.get_searcher_class("rcreg")
    assert issubclass(searcher_cls, RuleSearcher)
    assert searcher_cls.site_name == "rcreg"
    assert searcher_cls.BASE_URL == "https://demo.example"
    assert searcher_cls.priority == 33


def test_registry_auto_registers_a_rule_source_on_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No explicit ensure_registered / register_all: the registry must fall back
    # to the rule loader itself.
    _write(tmp_path, "rcauto.json", _search_rule("rcauto"))
    monkeypatch.setenv("NOVEL_RULES_DIR", str(tmp_path))
    reload_rules()

    assert issubclass(registrar.get_searcher_class("rcauto"), RuleSearcher)
    assert isinstance(registrar.get_fetcher("rcauto", FetcherConfig()), RuleFetcher)
    assert isinstance(registrar.get_parser("rcauto", ParserConfig()), RuleParser)


def test_searcher_priority_comes_from_the_search_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "rcprio.json", _search_rule("rcprio", priority=77))
    monkeypatch.setenv("NOVEL_RULES_DIR", str(tmp_path))
    reload_rules()

    assert registrar.get_searcher_class("rcprio").priority == 77


# --------------------------------------------------------------------------- #
# builtin rule registration
# --------------------------------------------------------------------------- #
def test_builtin_rule_searcher_is_discoverable() -> None:
    cls = registrar.get_searcher_class("biquge5r")
    assert issubclass(cls, RuleSearcher)
    assert cls.site_name == "biquge5r"


def test_register_all_makes_every_rule_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "rcall.json", _search_rule("rcall"))
    monkeypatch.setenv("NOVEL_RULES_DIR", str(tmp_path))
    reload_rules()

    register_all()

    classes = registrar.get_searcher_classes(sites=["rcall", "biquge5r"])
    assert [c.site_name for c in classes] == ["rcall", "biquge5r"]


def test_get_searcher_classes_without_sites_includes_rules() -> None:
    classes = registrar.get_searcher_classes(None)
    assert any(issubclass(c, RuleSearcher) for c in classes)
    assert any(getattr(c, "site_name", "") == "biquge5r" for c in classes)


# --------------------------------------------------------------------------- #
# failure modes stay explicit
# --------------------------------------------------------------------------- #
def test_rule_without_search_has_no_searcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(
        tmp_path,
        "rcnosearch.json",
        {
            "site_key": "rcnosearch",
            "base_url": "https://demo.example",
            "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"},
        },
    )
    monkeypatch.setenv("NOVEL_RULES_DIR", str(tmp_path))
    reload_rules()

    assert ensure_registered("rcnosearch") is True
    # Fetcher/parser exist, but no searcher was declared.
    assert isinstance(registrar.get_parser("rcnosearch", ParserConfig()), BaseParser)
    with pytest.raises(ValueError, match="Unsupported site"):
        registrar.get_searcher_class("rcnosearch")


def test_unknown_site_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported site"):
        registrar.get_searcher_class("rc-does-not-exist")


def test_ensure_registered_returns_false_and_registers_nothing() -> None:
    assert ensure_registered("rc-nope") is False


def test_registered_searcher_class_is_a_base_searcher() -> None:
    cls: type[BaseSearcher] = registrar.get_searcher_class("biquge5r")
    assert issubclass(cls, BaseSearcher)
