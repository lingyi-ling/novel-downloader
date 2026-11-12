#!/usr/bin/env python3
"""Robustness tests for plugin discovery, registration and lookup.

The registry is the single place where "which sites exist" is decided, so a
mistake here either crashes start-up (a broken plugin taking the process down)
or silently hides a site (a duplicate key scoring the wrong implementation, a
lazily imported module that is never retried, a rule source shadowing a
built-in).  These tests pin the defensive behaviour:

* a broken plugin / source is isolated and recorded in ``import_failures``;
* discovery order is deterministic (site names are sorted);
* source precedence (``_sources`` order) decides a duplicate key, independent
  of import history;
* duplicate keys are reported instead of silently overwriting;
* path-like site names never reach the importer;
* a rule source never replaces a hand-written plugin for the same key;
* concurrent on-demand registration creates the plugin objects only once.
"""

from __future__ import annotations

import contextlib
import json
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.plugins import registrar
from novel_downloader.plugins import registry as registry_module
from novel_downloader.plugins import rule as rule_module
from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import PluginRegistry
from novel_downloader.plugins.rule.fetcher import RuleFetcher
from novel_downloader.schemas import FetcherConfig

_PROBE = "nd_registry_probe"

_SEARCHER_SOURCE = """\
from novel_downloader.plugins import registry as _registry_module
from novel_downloader.plugins.base.searcher import BaseSearcher

_R = _registry_module.__dict__["_active_test_registry"]


@_R.register_searcher({key!r})
class ProbeSearcher(BaseSearcher):
    site_name = {site_name!r}
    origin = {origin!r}

    async def _fetch_html(self, keyword: str) -> str:
        return ""

    def _parse_html(self, html_str: str, limit: int | None = None):
        return []
"""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _write_pkg(root: Path, pkg: str) -> Path:
    """Create an importable plugin source package and return its sites dir."""
    sites = root / pkg / "sites"
    sites.mkdir(parents=True)
    (root / pkg / "__init__.py").write_text("", encoding="utf-8")
    (sites / "__init__.py").write_text("", encoding="utf-8")
    return sites


def _write_site(
    sites: Path,
    name: str,
    kind: str,
    source: str,
    *,
    init: bool = True,
) -> None:
    site = sites / name
    site.mkdir(parents=True, exist_ok=True)
    if init:
        (site / "__init__.py").write_text("", encoding="utf-8")
    (site / f"{kind}.py").write_text(source, encoding="utf-8")


def _searcher(origin: str, *, key: str | None = None) -> str:
    return _SEARCHER_SOURCE.format(
        key=key or origin,
        site_name=origin,
        origin=origin,
    )


def _drop_probe_modules() -> None:
    # Generated packages are ``nd_registry_probe_<suffix>``; a dotted prefix
    # would miss them (``nd_registry_probe_first`` has no dot after the stem).
    for name in list(sys.modules):
        if name.startswith(_PROBE):
            del sys.modules[name]


@pytest.fixture()
def probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[PluginRegistry]:
    """A fresh registry that generated plugin modules can register into."""
    registry = PluginRegistry()
    monkeypatch.setattr(
        registry_module, "_active_test_registry", registry, raising=False
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    _drop_probe_modules()
    yield registry
    _drop_probe_modules()


# --------------------------------------------------------------------------- #
# deterministic discovery + source precedence
# --------------------------------------------------------------------------- #
def test_aggregate_discovery_matches_explicit_lookup_precedence(
    tmp_path: Path, probe: PluginRegistry
) -> None:
    """Whichever source wins an explicit lookup must also win enumeration."""
    first = _write_pkg(tmp_path, f"{_PROBE}_first")
    second = _write_pkg(tmp_path, f"{_PROBE}_second")
    _write_site(first, "dup", "searcher", _searcher("first", key="dup"))
    _write_site(second, "dup", "searcher", _searcher("second", key="dup"))

    probe._sources = [f"{_PROBE}_first", f"{_PROBE}_second"]

    probe._try_import_site("dup", "searcher")
    explicit = probe._searchers["dup"]
    assert explicit.origin == "first"

    # Fresh modules so the aggregate scan really imports both sources again.
    probe._searchers.clear()
    _drop_probe_modules()
    probe._load_all_sites("searcher")

    # Re-importing recreates the class, so compare the source it came from.
    assert probe._searchers["dup"].origin == explicit.origin == "first"

    # Swapping the source order swaps the winner, in both code paths.
    probe._sources = [f"{_PROBE}_second", f"{_PROBE}_first"]
    probe._searchers.clear()
    _drop_probe_modules()
    probe._try_import_site("dup", "searcher")
    explicit = probe._searchers["dup"]
    assert explicit.origin == "second"

    probe._searchers.clear()
    _drop_probe_modules()
    probe._load_all_sites("searcher")
    assert probe._searchers["dup"].origin == explicit.origin == "second"


def test_site_scan_order_is_sorted_not_filesystem_order(
    tmp_path: Path, probe: PluginRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    sites = _write_pkg(tmp_path, f"{_PROBE}_ordered")
    # Both directories claim the same key; sorted scan means "zzz" is last and
    # therefore wins.
    _write_site(sites, "zzz", "searcher", _searcher("zzz", key="dup"))
    _write_site(sites, "aaa", "searcher", _searcher("aaa", key="dup"))
    probe._sources = [f"{_PROBE}_ordered"]

    monkeypatch.setattr(
        PluginRegistry, "_iter_site_names", staticmethod(lambda pkg: ["zzz", "aaa"])
    )

    probe._load_all_sites("searcher")

    assert probe._searchers["dup"].origin == "zzz"


def test_duplicate_site_key_is_reported(
    tmp_path: Path,
    probe: PluginRegistry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sites = _write_pkg(tmp_path, f"{_PROBE}_dupe")
    _write_site(sites, "aaa", "searcher", _searcher("aaa", key="dup"))
    _write_site(sites, "zzz", "searcher", _searcher("zzz", key="dup"))
    probe._sources = [f"{_PROBE}_dupe"]
    monkeypatch.setattr(
        PluginRegistry, "_iter_site_names", staticmethod(lambda pkg: ["zzz", "aaa"])
    )

    with caplog.at_level("WARNING", logger="novel_downloader.plugins.registry"):
        probe._load_all_sites("searcher")

    assert "'dup'" in caplog.text
    assert "aaa.searcher" in caplog.text
    assert "zzz.searcher" in caplog.text


# --------------------------------------------------------------------------- #
# broken plugins are isolated and diagnosable
# --------------------------------------------------------------------------- #
def test_broken_plugins_are_isolated_and_recorded(
    tmp_path: Path, probe: PluginRegistry
) -> None:
    sites = _write_pkg(tmp_path, f"{_PROBE}_broken")
    # A namespace subpackage (no __init__.py) still imports fine.
    _write_site(sites, "ok", "searcher", _searcher("ok"), init=False)
    # A syntax error must not abort the scan.
    _write_site(sites, "syntax", "searcher", "def broken(:\n")
    # A plugin that raises after importing succeeds must be skipped too.
    _write_site(
        sites,
        "raises",
        "searcher",
        "raise ImportError('missing optional dependency')\n",
    )
    # A stray file in the sites package is not a site.
    (sites / "not_a_site.py").write_text("X = 1\n", encoding="utf-8")
    probe._sources = [f"{_PROBE}_broken"]

    probe._load_all_sites("searcher")

    assert "ok" in probe._searchers
    failures = probe.import_failures
    assert f"{_PROBE}_broken.sites.syntax.searcher" in failures
    assert f"{_PROBE}_broken.sites.raises.searcher" in failures
    assert all("searcher" in mod for mod in failures)


def test_broken_sites_package_does_not_abort_other_sources(
    tmp_path: Path, probe: PluginRegistry
) -> None:
    good = _write_pkg(tmp_path, f"{_PROBE}_goodsrc")
    _write_site(good, "ok", "searcher", _searcher("ok"))

    broken_root = tmp_path / f"{_PROBE}_brokensrc"
    (broken_root / "sites").mkdir(parents=True)
    (broken_root / "__init__.py").write_text("", encoding="utf-8")
    (broken_root / "sites" / "__init__.py").write_text(
        "raise RuntimeError('broken namespace')\n", encoding="utf-8"
    )
    probe._sources = [f"{_PROBE}_brokensrc", f"{_PROBE}_goodsrc"]

    probe._load_all_sites("searcher")  # must not raise

    assert "ok" in probe._searchers


def test_partial_registration_from_a_failing_module_is_rolled_back(
    tmp_path: Path, probe: PluginRegistry
) -> None:
    sites = _write_pkg(tmp_path, f"{_PROBE}_half")
    _write_site(
        sites,
        "half",
        "fetcher",
        """\
from novel_downloader.plugins import registry as _registry_module

_R = _registry_module.__dict__["_active_test_registry"]


@_R.register_fetcher("half")
class HalfFetcher:
    pass


raise ImportError("failed after registering")
""",
    )
    probe._sources = [f"{_PROBE}_half"]

    probe._try_import_site("half", "fetcher")  # must not raise

    assert "half" not in probe._fetchers
    assert f"{_PROBE}_half.sites.half.fetcher" in probe.import_failures


def test_failed_import_is_attempted_only_once(
    tmp_path: Path, probe: PluginRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []
    target = f"{_PROBE}_cache.sites.ghost.searcher"

    def fake_import(name: str) -> Any:
        if name == target:
            attempts.append(name)
            raise ImportError("boom")
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(registry_module, "import_module", fake_import)
    probe._sources = [f"{_PROBE}_cache"]

    probe._try_import_site("ghost", "searcher")
    probe._try_import_site("ghost", "searcher")

    assert attempts == [target]
    assert target in probe.import_failures

    # Explicit reset makes a retry possible after the user fixes the plugin.
    probe.clear_import_failures()
    probe._try_import_site("ghost", "searcher")
    assert attempts == [target, target]


# --------------------------------------------------------------------------- #
# keys: normalization and traversal
# --------------------------------------------------------------------------- #
def test_decorator_keys_are_normalized_like_lookup() -> None:
    registry = PluginRegistry()

    @registry.register_fetcher("  1Foo ")
    class _F:
        def __init__(self, config: Any) -> None:
            self.config = config

    @registry.register_searcher(" UPPER ")
    class _S(BaseSearcher):
        async def _fetch_html(self, keyword: str) -> str:
            return ""

        def _parse_html(self, html_str: str, limit: int | None = None) -> list[Any]:
            return []

    assert isinstance(registry.get_fetcher("1foo", FetcherConfig()), _F)
    assert registry.get_searcher_class("upper") is _S


@pytest.mark.parametrize(
    "site_key",
    ["../evil", "..", "foo/bar", "foo\\bar", "foo.bar", "a b"],
)
def test_path_like_site_keys_are_rejected(site_key: str) -> None:
    with pytest.raises(ValueError, match="Invalid site key"):
        PluginRegistry._validate_site_key(site_key)


def test_normalize_key_stays_lenient_for_the_search_cache() -> None:
    # ``build_key`` shares ``_normalize_key``; a bad site selection must not
    # make the search API raise, it simply resolves to no searcher.
    from novel_downloader.plugins.search_cache import build_key

    assert build_key("k", ["../evil"]).sites == ("../evil",)
    assert PluginRegistry._normalize_key("../evil") == "../evil"


def test_path_like_site_key_never_reaches_the_importer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = PluginRegistry()
    imported: list[str] = []
    monkeypatch.setattr(
        registry_module, "import_module", lambda name: imported.append(name)
    )

    with pytest.raises(ValueError, match="Invalid site key"):
        registry.get_fetcher("../evil", FetcherConfig())

    assert imported == []


# --------------------------------------------------------------------------- #
# rule sources must never shadow a hand-written plugin
# --------------------------------------------------------------------------- #
def _rule(site_key: str, base_url: str = "https://rule.example") -> dict[str, Any]:
    return {
        "site_key": site_key,
        "base_url": base_url,
        "hosts": ["rule.example"],
        "search": {
            "url": f"{base_url}/s?q={{keyword}}",
            "item": "div.result",
            "fields": {"title": "h3", "url": {"attr": "href"}},
        },
        "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"},
    }


def _write_rule(directory: Path, name: str, payload: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture()
def rule_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[PluginRegistry]:
    """Rule registration targeted at a private registry and directory."""
    registry = PluginRegistry()
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    monkeypatch.setattr(rule_module, "registrar", registry)
    monkeypatch.setattr(rule_module, "_user_dirs", lambda: [rules_dir])
    rule_module.reload_rules()
    yield registry
    rule_module.reload_rules()


def test_rule_source_cannot_replace_a_hand_written_plugin(
    rule_registry: PluginRegistry,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BuiltinFetcher:
        pass

    rule_registry.register_site("shared", fetcher=BuiltinFetcher)
    _write_rule(tmp_path / "rules", "shadow.json", _rule("shared"))

    rule_module.reload_rules()
    assert rule_module.ensure_registered("shared") is True

    assert rule_registry._fetchers["shared"] is BuiltinFetcher
    assert not issubclass(rule_registry._fetchers["shared"], RuleFetcher)
    assert "collides" in caplog.text
    assert "shared" in caplog.text


def test_one_bad_rule_key_does_not_block_the_other_rules(
    rule_registry: PluginRegistry,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_rule(tmp_path / "rules", "a_bad.json", _rule("../evil"))
    _write_rule(tmp_path / "rules", "z_good.json", _rule("goodrule"))

    rule_module.reload_rules()
    rule_module.register_all()  # must not raise

    assert "goodrule" in rule_registry._fetchers
    assert "../evil" not in rule_registry._fetchers
    assert "evil" in caplog.text


# --------------------------------------------------------------------------- #
# concurrent on-demand registration
# --------------------------------------------------------------------------- #
def test_concurrent_ensure_registered_builds_the_plugin_once(
    rule_registry: PluginRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_rule(tmp_path / "rules", "concurrent.json", _rule("concurrent"))
    rule_module.reload_rules()

    calls: list[str] = []
    barrier = threading.Barrier(2)
    original = rule_module._make_fetcher

    def blocking(rule: Any, key: str) -> Any:
        calls.append(key)
        # The fixed code only lets one thread reach this point; the timeout
        # keeps the test from hanging if the second one never arrives.
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait(timeout=0.5)
        return original(rule, key)

    monkeypatch.setattr(rule_module, "_make_fetcher", blocking)

    errors: list[BaseException] = []

    def worker() -> None:
        try:
            rule_module.ensure_registered("concurrent")
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert calls == ["concurrent"]


# --------------------------------------------------------------------------- #
# diagnostics surface stays a plain copy
# --------------------------------------------------------------------------- #
def test_import_failures_is_a_copy(probe: PluginRegistry) -> None:
    probe._import_failures["x"] = "boom"

    snapshot = probe.import_failures
    snapshot["x"] = "changed"

    assert probe.import_failures == {"x": "boom"}


# --------------------------------------------------------------------------- #
# baseline: the global registrar still exposes the documented API
# --------------------------------------------------------------------------- #
def test_get_registered_is_a_read_only_probe() -> None:
    # ``get_registered`` never imports a plugin and rejects unknown kinds.
    assert isinstance(registrar, PluginRegistry)
    assert registrar.get_registered("zz-no-such-site-zz", "fetcher") is None
    assert registrar.get_registered("zz-no-such-site-zz", "searcher") is None
    with pytest.raises(ValueError, match="Unknown plugin kind"):
        registrar.get_registered("demo", "nope")
