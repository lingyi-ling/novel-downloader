#!/usr/bin/env python3
"""
Rule discovery precedence tests.

The loader scans, in order: builtin package, then each user directory.  Later
directories win, so a user rule must be able to shadow a builtin (and the last
user directory shadows earlier ones).  All offline.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.plugins import registrar
from novel_downloader.plugins.rule import (
    ensure_registered,
    iter_rules,
    reload_rules,
    resolve_url,
)


@pytest.fixture(autouse=True)
def _isolated_rule_state() -> Iterator[None]:
    reload_rules()
    yield
    reload_rules()


def _write(directory: Path, name: str, payload: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


def _rule(site_key: str, base_url: str, *, priority: int = 45) -> dict[str, Any]:
    return {
        "site_key": site_key,
        "base_url": base_url,
        "hosts": ["demo.example"],
        "resolve": {"book": r"^/book/(\d+)/$"},
        "search": {
            "url": f"{base_url}/s?q={{keyword}}",
            "item": "div.result",
            "priority": priority,
            "fields": {"title": "h3", "url": {"attr": "href"}},
        },
        "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"},
    }


def _meta() -> dict[str, dict[str, str]]:
    return {key: info for key, _, info in iter_rules()}


def test_user_rule_shadows_a_builtin_with_the_same_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "shadow.json", _rule("biquge5r", "https://user.example"))
    monkeypatch.setenv("NOVEL_RULES_DIR", str(tmp_path))

    reload_rules()

    assert ensure_registered("biquge5r") is True
    assert _meta()["biquge5r"]["origin"] == "user"
    assert _meta()["biquge5r"]["file"] == str(tmp_path / "shadow.json")

    searcher_cls = registrar.get_searcher_class("biquge5r")
    assert searcher_cls.BASE_URL == "https://user.example"


def test_last_user_directory_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write(first, "demo.json", _rule("demosite", "https://first.example"))
    _write(second, "demo.json", _rule("demosite", "https://second.example"))

    # ``_user_dirs`` is consulted at load time, so patching it directly models
    # two ordered search locations deterministically.
    monkeypatch.setattr(
        "novel_downloader.plugins.rule._user_dirs", lambda: [first, second]
    )

    rules = reload_rules()

    assert rules["demosite"].base_url == "https://second.example"


def test_host_mapping_follows_the_winning_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "shadow.json", _rule("biquge5r", "https://user.example"))
    monkeypatch.setenv("NOVEL_RULES_DIR", str(tmp_path))
    reload_rules()

    resolved = resolve_url("demo.example", "/book/7/")

    assert resolved is not None
    assert resolved["site_key"] == "biquge5r"
    assert resolved["book_id"] == "7"


def test_independent_user_rule_does_not_disturb_builtins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "extra.json", _rule("extrasite", "https://extra.example"))
    monkeypatch.setenv("NOVEL_RULES_DIR", str(tmp_path))

    rules = reload_rules()

    assert rules["extrasite"].base_url == "https://extra.example"
    assert rules["biquge5r"].base_url.startswith("https://www.biquge5.com")
    assert _meta()["biquge5r"]["origin"] == "builtin"
