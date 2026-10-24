#!/usr/bin/env python3
"""Consistency tests for :mod:`novel_downloader.plugins.registry`.

These tests intentionally freeze the *current* semantics, including the known
asymmetries between registration helpers and lookup normalization.  A fresh
``PluginRegistry`` is used so the global registrar (and rule discovery) is not
mutated.
"""

from __future__ import annotations

from typing import Any

import pytest

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.common.client import CommonClient
from novel_downloader.plugins.registry import PluginRegistry
from novel_downloader.plugins.rule import _normalize_key as rule_normalize_key
from novel_downloader.schemas import (
    FetcherConfig,
    ParserConfig,
    SearchResult,
)

UNKNOWN = "zz-no-such-site-zz"


class _Fetcher:
    def __init__(self, config: Any) -> None:
        self.config = config


class _Parser:
    def __init__(self, config: Any) -> None:
        self.config = config


class _Searcher(BaseSearcher):
    site_name = "dummy"

    async def _fetch_html(self, keyword: str) -> str:
        return ""

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return []


def _registry() -> PluginRegistry:
    registry = PluginRegistry()
    registry.register_site(
        "alpha", fetcher=_Fetcher, parser=_Parser, searcher=_Searcher
    )
    return registry


# --------------------------------------------------------------------------- #
# _normalize_key
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" Demo ", "demo"),
        ("DEMO", "demo"),
        ("23ddw", "n23ddw"),
        ("n23ddw", "n23ddw"),
        ("biquge_5", "biquge_5"),
        ("站点", "站点"),
    ],
)
def test_normalize_key(raw: str, expected: str) -> None:
    assert PluginRegistry._normalize_key(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
def test_normalize_key_rejects_empty_values(raw: str) -> None:
    with pytest.raises(ValueError, match="Site key cannot be empty"):
        PluginRegistry._normalize_key(raw)


def test_rule_loader_normalizes_empty_keys_differently() -> None:
    # Documented asymmetry: the registry rejects an empty key, while the rule
    # loader's private helper returns "".  This is why ``lookup`` and ``rule``
    # behaviour must not be conflated.
    assert rule_normalize_key("") == ""
    assert rule_normalize_key("   ") == ""
    assert rule_normalize_key("23ddw") == "n23ddw"


# --------------------------------------------------------------------------- #
# registration / lookup
# --------------------------------------------------------------------------- #
def test_decorator_registration_is_case_insensitive() -> None:
    registry = PluginRegistry()

    @registry.register_fetcher("Demo")
    class _F(_Fetcher):
        pass

    @registry.register_parser("Demo")
    class _P(_Parser):
        pass

    assert isinstance(registry.get_fetcher("DEMO", FetcherConfig()), _F)
    assert isinstance(registry.get_parser(" demo ", ParserConfig()), _P)


def test_register_site_normalizes_the_key_like_lookup() -> None:
    registry = PluginRegistry()
    registry.register_site("123abc", fetcher=_Fetcher, parser=_Parser)

    assert isinstance(registry.get_fetcher("123abc", FetcherConfig()), _Fetcher)
    assert isinstance(registry.get_parser("123ABC", ParserConfig()), _Parser)


def test_register_site_keeps_existing_components() -> None:
    registry = _registry()
    registry.register_site("alpha", parser=_Parser)

    assert isinstance(registry.get_fetcher("alpha", FetcherConfig()), _Fetcher)
    assert isinstance(registry.get_parser("alpha", ParserConfig()), _Parser)


def test_decorator_with_digit_prefix_is_not_reachable() -> None:
    # Known asymmetry: ``register_fetcher`` lowercases but does NOT apply the
    # "prefix n when the key starts with a digit" rule, while every lookup does.
    registry = PluginRegistry()
    registry.register_fetcher("123abc")(_Fetcher)

    assert "123abc" in registry._fetchers
    with pytest.raises(ValueError, match="Unsupported site"):
        registry.get_fetcher("123abc", FetcherConfig())


def test_register_searcher_does_not_lowercase_an_explicit_key() -> None:
    # Known asymmetry: ``register_searcher`` stores an explicit key verbatim,
    # whereas the other register_* decorators lowercase it.
    registry = PluginRegistry()
    registry.register_searcher("UPPER")(_Searcher)

    assert "UPPER" in registry._searchers
    with pytest.raises(ValueError, match="Unsupported site"):
        registry.get_searcher_class("UPPER")


# --------------------------------------------------------------------------- #
# unknown sites
# --------------------------------------------------------------------------- #
def test_unknown_fetcher_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unsupported site"):
        _registry().get_fetcher(UNKNOWN, FetcherConfig())


def test_unknown_parser_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unsupported site"):
        _registry().get_parser(UNKNOWN, ParserConfig())


def test_unknown_searcher_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unsupported site"):
        _registry().get_searcher_class(UNKNOWN)


def test_unknown_client_falls_back_to_common_client() -> None:
    # Known asymmetry: clients have a generic fallback, the other kinds do not.
    client = _registry().get_client(UNKNOWN)

    assert isinstance(client, CommonClient)
    assert client._site == UNKNOWN


def test_empty_site_key_raises_before_any_lookup() -> None:
    registry = _registry()

    with pytest.raises(ValueError, match="cannot be empty"):
        registry.get_fetcher("", FetcherConfig())
    with pytest.raises(ValueError, match="cannot be empty"):
        registry.get_parser("   ", ParserConfig())
    with pytest.raises(ValueError, match="cannot be empty"):
        registry.get_searcher_class("")


# --------------------------------------------------------------------------- #
# get_searcher_classes()
# --------------------------------------------------------------------------- #
def test_explicit_known_sites_preserve_order_and_duplicates() -> None:
    registry = _registry()
    other = type("Other", (_Searcher,), {"site_name": "other"})
    registry.register_site("beta", searcher=other)

    classes = registry.get_searcher_classes(["beta", "alpha", "beta"])

    assert classes == [other, _Searcher, other]


def test_unknown_sites_are_silently_skipped() -> None:
    registry = _registry()

    assert registry.get_searcher_classes([UNKNOWN]) == []
    assert registry.get_searcher_classes(["alpha", UNKNOWN]) == [_Searcher]


def test_empty_or_blank_site_entries_are_silently_skipped() -> None:
    registry = _registry()

    assert registry.get_searcher_classes([""]) == []
    assert registry.get_searcher_classes(["   "]) == []
    assert registry.get_searcher_classes(["alpha", ""]) == [_Searcher]


def test_empty_site_list_means_all_not_none() -> None:
    # Documented asymmetry: ``[]`` is falsy and therefore takes the same branch
    # as ``None`` (return every registered searcher), not "search nothing".
    registry = _registry()

    assert registry.get_searcher_classes([], load_all_if_none=False) == [_Searcher]
    assert registry.get_searcher_classes(None, load_all_if_none=False) == [_Searcher]


def test_load_all_if_none_false_does_not_discover_plugins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = PluginRegistry()
    called: list[str] = []
    monkeypatch.setattr(registry, "_load_all_sites", lambda kind: called.append(kind))
    monkeypatch.setattr(
        registry, "_load_all_rule_sites", lambda: called.append("rules")
    )

    assert registry.get_searcher_classes(None, load_all_if_none=False) == []
    assert called == []


def test_load_all_if_none_true_triggers_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = PluginRegistry()
    called: list[str] = []
    monkeypatch.setattr(registry, "_load_all_sites", lambda kind: called.append(kind))
    monkeypatch.setattr(
        registry, "_load_all_rule_sites", lambda: called.append("rules")
    )

    assert registry.get_searcher_classes(None) == []
    assert called == ["searcher", "rules"]


# --------------------------------------------------------------------------- #
# processors (same normalization helpers)
# --------------------------------------------------------------------------- #
def test_processor_key_derivation() -> None:
    assert (
        PluginRegistry._derive_processor_key(
            "novel_downloader.plugins.processors.cleaner"
        )
        == "cleaner"
    )
    assert PluginRegistry._derive_processor_key("pkg.mod.thing") == "thing"


def test_unknown_processor_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported processor"):
        PluginRegistry().get_processor(UNKNOWN, {})


def test_processor_lookup_strips_and_lowercases() -> None:
    registry = PluginRegistry()
    registry.register_processor("Cleaner")(_Parser)

    assert isinstance(registry.get_processor("  CLEANER ", {}), _Parser)
