#!/usr/bin/env python3
"""Offline unit tests for :mod:`novel_downloader.apps.music.providers.creds`.

The module resolves its storage path at import time, so every test redirects
the module-level ``_FILE`` into ``tmp_path``.

The target module is deliberately untyped (see the ``[tool.mypy.overrides]``
relaxation in ``pyproject.toml``), so calls into it skip that check here too.
"""

# mypy: disallow-untyped-calls=False

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_downloader.apps.music.providers import creds


@pytest.fixture(autouse=True)
def _redirect_creds_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "platform_creds.json"
    monkeypatch.setattr(creds, "_FILE", str(target))
    return target


def test_load_returns_empty_when_file_missing() -> None:
    assert creds.load() == {}
    assert creds.get("netease") == {}


def test_set_and_get_roundtrip() -> None:
    creds.set("netease", {"MUSIC_U": "abc"})
    assert creds.get("netease") == {"MUSIC_U": "abc"}
    assert creds.get("other") == {}


def test_set_overwrites_only_target_key() -> None:
    creds.set("a", {"x": 1})
    creds.set("b", {"y": 2})
    creds.set("a", {"x": 9})
    data = creds.load()
    assert data == {"a": {"x": 9}, "b": {"y": 2}}


def test_set_with_empty_data_clears_key() -> None:
    creds.set("a", {"x": 1})
    creds.set("a", {})
    assert creds.load() == {}


def test_clear_removes_key() -> None:
    creds.set("a", {"x": 1})
    creds.clear("a")
    assert creds.load() == {}


def test_load_ignores_non_dict_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = Path(creds._FILE)
    target.write_text("[1, 2, 3]", encoding="utf-8")
    assert creds.load() == {}


def test_load_ignores_corrupt_json(tmp_path: Path) -> None:
    target = Path(creds._FILE)
    target.write_text("{broken", encoding="utf-8")
    assert creds.load() == {}


def test_file_contents_are_valid_json_with_utf8() -> None:
    creds.set("netease", {"token": "令牌"})
    target = Path(creds._FILE)
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "netease": {"token": "令牌"}
    }
