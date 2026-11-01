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


def test_set_restricts_file_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """platform_creds.json holds login secrets (TIDAL tokens, site cookies).

    Like the NetEase session file (weapi.save_cookies) and the login cookie
    store (cookie_io.write_cookie_items), the file must be tightened to
    owner-only before it lands on disk. Windows ignores POSIX permission
    bits, so the guarantee is asserted on the ``chmod`` call itself.
    """
    chmod_calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        creds.os, "chmod", lambda path, mode: chmod_calls.append((str(path), mode))
    )

    creds.set("tidal", {"token": "secret"})

    assert (creds._FILE + ".tmp", 0o600) in chmod_calls
    assert Path(creds._FILE).exists()


def test_set_cleans_tmp_residue_when_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """凭据是明文敏感数据：原子替换失败时不能把半截 .tmp 留在盘上。"""
    creds.set("tidal", {"token": "old"})
    target = Path(creds._FILE)
    before = target.read_text(encoding="utf-8")

    def failing_replace(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(creds.os, "replace", failing_replace)

    with pytest.raises(OSError, match="disk full"):
        creds.set("tidal", {"token": "secret"})

    # 半截明文 .tmp 被清掉，旧文件原样保留
    assert not Path(creds._FILE + ".tmp").exists()
    assert target.read_text(encoding="utf-8") == before


def test_set_cleans_tmp_residue_when_dump_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """json 序列化中途抛错同样不能留下半截 .tmp。"""
    real_dump = creds.json.dump

    def failing_dump(obj: object, fh: object, **kwargs: object) -> None:
        real_dump(obj, fh, **kwargs)  # type: ignore[arg-type]
        raise ValueError("unserialisable credential payload")

    monkeypatch.setattr(creds.json, "dump", failing_dump)

    with pytest.raises(ValueError, match="unserialisable"):
        creds.set("tidal", {"token": "secret"})

    assert not Path(creds._FILE + ".tmp").exists()
    assert not Path(creds._FILE).exists()
