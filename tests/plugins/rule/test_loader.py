#!/usr/bin/env python3
"""Loader tests: rule discovery, registration and URL resolution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.plugins import registrar
from novel_downloader.plugins.rule import (
    RuleError,
    ensure_registered,
    install_rule,
    iter_rules,
    load_rules,
    reload_rules,
    remove_rule,
    resolve_url,
)
from novel_downloader.plugins.rule.parser import RuleParser
from novel_downloader.schemas import ParserConfig


@pytest.fixture(autouse=True)
def _clean_rule_state() -> Any:
    """Every test starts and ends with rules loaded from the real locations."""
    reload_rules()
    yield
    reload_rules()


def _write_rule(directory: Path, filename: str, payload: dict[str, Any]) -> None:
    (directory / filename).write_text(json.dumps(payload), encoding="utf-8")


def _demo_rule(site_key: str) -> dict[str, Any]:
    return {
        "site_key": site_key,
        "base_url": "https://demo.example",
        "hosts": ["demo.example"],
        "resolve": {"book": r"^/book/(\d+)/$"},
        "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"},
    }


# --------------------------------------------------------------------------- #
# builtin rules
# --------------------------------------------------------------------------- #
def test_builtin_rule_is_loaded_with_every_section() -> None:
    rules = load_rules()

    assert "biquge5r" in rules
    rule = rules["biquge5r"]
    assert rule.base_url.startswith("https://")
    assert rule.search is not None
    assert rule.book is not None
    assert rule.chapter is not None
    assert rule.hosts


def test_iter_rules_reports_the_loading_origin() -> None:
    meta = {key: info for key, _, info in iter_rules()}

    assert meta["biquge5r"]["origin"] == "builtin"
    assert meta["biquge5r"]["file"] == "biquge5r.json"


def test_ensure_registered_wires_a_usable_parser() -> None:
    assert ensure_registered("biquge5r") is True

    parser = registrar.get_parser("biquge5r", ParserConfig())
    assert isinstance(parser, RuleParser)
    assert parser.site_name == "biquge5r"
    assert parser._rule.site_key == "biquge5r"


def test_ensure_registered_normalizes_the_key() -> None:
    assert ensure_registered("BIQUGE5R") is True
    assert ensure_registered("biquge5r") is True


def test_ensure_registered_returns_false_for_unknown_site() -> None:
    assert ensure_registered("no-such-source") is False


# --------------------------------------------------------------------------- #
# user rules
# --------------------------------------------------------------------------- #
def test_user_rule_is_loaded_from_novel_rules_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_rule(tmp_path, "demo.json", _demo_rule("demo"))
    monkeypatch.setenv("NOVEL_RULES_DIR", str(tmp_path))

    rules = reload_rules()

    assert rules["demo"].base_url == "https://demo.example"
    assert ensure_registered("demo") is True


def test_user_rule_overrides_a_builtin_with_the_same_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_rule(tmp_path, "override.json", _demo_rule("biquge5r"))
    monkeypatch.setenv("NOVEL_RULES_DIR", str(tmp_path))

    rules = reload_rules()
    meta = {key: info for key, _, info in iter_rules()}

    assert rules["biquge5r"].base_url == "https://demo.example"
    assert meta["biquge5r"]["origin"] == "user"


def test_numeric_site_key_gets_a_letter_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_rule(tmp_path, "123.json", _demo_rule("123"))
    monkeypatch.setenv("NOVEL_RULES_DIR", str(tmp_path))

    reload_rules()

    assert ensure_registered("123") is True
    assert ensure_registered("n123") is True


def test_underscore_prefixed_files_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_rule(tmp_path, "_draft.json", _demo_rule("draft"))
    _write_rule(tmp_path, "live.json", _demo_rule("live"))
    monkeypatch.setenv("NOVEL_RULES_DIR", str(tmp_path))

    rules = reload_rules()

    assert "live" in rules
    assert "draft" not in rules


def test_invalid_rule_files_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "broken.json").write_text("{ not json", encoding="utf-8")
    _write_rule(tmp_path, "incomplete.json", {"base_url": "https://demo.example"})
    _write_rule(tmp_path, "valid.json", _demo_rule("demo"))
    monkeypatch.setenv("NOVEL_RULES_DIR", str(tmp_path))

    rules = reload_rules()

    assert "demo" in rules
    assert "broken" not in rules
    assert "incomplete" not in rules


def test_rule_file_with_duplicate_keys_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``json.loads`` silently keeps the last duplicate; the loader must treat a
    # duplicate key as a malformed file instead of loading a surprise config.
    (tmp_path / "dup.json").write_text(
        '{"site_key": "dup", "site_key": "dup2",'
        ' "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"},'
        ' "base_url": "https://demo.example"}',
        encoding="utf-8",
    )
    _write_rule(tmp_path, "ok.json", _demo_rule("ok"))
    monkeypatch.setenv("NOVEL_RULES_DIR", str(tmp_path))

    rules = reload_rules()

    assert "dup" not in rules
    assert "dup2" not in rules  # last-wins merging must not sneak the rule in
    assert "ok" in rules


def test_install_rule_rejects_duplicate_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_dir = tmp_path / "installed"
    monkeypatch.setenv("NOVEL_RULES_DIR", str(install_dir))
    dup = tmp_path / "dup.json"
    dup.write_text(
        '{"site_key": "dup", "site_key": "dup2",'
        ' "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"},'
        ' "base_url": "https://demo.example"}',
        encoding="utf-8",
    )

    with pytest.raises(RuleError, match=r"duplicate key 'site_key'"):
        install_rule(dup)

    assert not (install_dir / "dup.json").exists()


# --------------------------------------------------------------------------- #
# installing / removing user rules
# --------------------------------------------------------------------------- #
def test_install_rule_copies_into_the_user_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "src"
    install_dir = tmp_path / "installed"
    source_dir.mkdir()
    _write_rule(source_dir, "demo.json", _demo_rule("demo"))
    monkeypatch.setenv("NOVEL_RULES_DIR", str(install_dir))

    target = install_rule(source_dir / "demo.json")

    assert target == install_dir / "demo.json"
    assert target.is_file()
    assert ensure_registered("demo") is True


def test_install_rule_refuses_to_overwrite_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "src"
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    source_dir.mkdir()
    _write_rule(source_dir, "demo.json", _demo_rule("demo"))
    (install_dir / "demo.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("NOVEL_RULES_DIR", str(install_dir))

    with pytest.raises(RuleError, match="already exists"):
        install_rule(source_dir / "demo.json")

    assert install_rule(source_dir / "demo.json", force=True).is_file()


def test_install_rule_rejects_malformed_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_dir = tmp_path / "installed"
    monkeypatch.setenv("NOVEL_RULES_DIR", str(install_dir))
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")

    with pytest.raises(RuleError, match="invalid JSON"):
        install_rule(broken)


def test_remove_rule_deletes_a_user_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_rule(tmp_path, "demo.json", _demo_rule("demo"))
    monkeypatch.setenv("NOVEL_RULES_DIR", str(tmp_path))
    reload_rules()

    removed = remove_rule("demo")

    assert removed == tmp_path / "demo.json"
    assert not removed.exists()
    assert "demo" not in load_rules()


def test_remove_rule_protects_builtin_and_unknown_keys() -> None:
    with pytest.raises(RuleError, match="built-in"):
        remove_rule("biquge5r")
    with pytest.raises(RuleError, match="unknown rule source"):
        remove_rule("no-such-source")


# --------------------------------------------------------------------------- #
# url resolution
# --------------------------------------------------------------------------- #
def test_resolve_url_matches_a_chapter_link() -> None:
    assert resolve_url("www.biquge5.com", "/0_143/155832.html") == {
        "site_key": "biquge5r",
        "book_id": "0_143",
        "chapter_id": "155832",
    }


def test_resolve_url_matches_a_book_link() -> None:
    assert resolve_url("www.biquge5.com", "/0_143/") == {
        "site_key": "biquge5r",
        "book_id": "0_143",
        "chapter_id": None,
    }


def test_resolve_url_is_host_case_insensitive() -> None:
    resolved = resolve_url("WWW.Biquge5.com", "/0_143/")
    assert resolved is not None
    assert resolved["site_key"] == "biquge5r"


def test_resolve_url_returns_none_for_unknown_host_or_path() -> None:
    assert resolve_url("unknown.example", "/0_143/") is None
    assert resolve_url("www.biquge5.com", "/not-a-book") is None
