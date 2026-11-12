#!/usr/bin/env python3
"""平台能力矩阵：单一来源、缺依赖状态与 JSON 稳定性。

这些测试不联网、不 import 任何重型平台依赖：

* 每个已知平台（含未注册的重档平台）都有一份描述
* ``available()`` / ``available_labels()`` / ``platform_note()`` 与描述一致
* 缺 Python 包 / 外部工具 / 未注册时的状态与原因稳定
* 描述可 JSON 序列化，且版本矩阵与 ``LIGHT_PLATFORMS`` / ``HEAVY_PLATFORMS``
  / ``PLATFORM_DEPS`` 不漂移
* 只列平台不会把 pywidevine / ytmusicapi / vendored 平台模块拖进内存
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from novel_downloader.apps.music import providers
from novel_downloader.apps.music.providers import musicdl
from novel_downloader.apps.music.providers.base import (
    CAPABILITY_MISSING_DEPENDENCY,
    CAPABILITY_MISSING_TOOL,
    CAPABILITY_READY,
    CAPABILITY_UNAVAILABLE,
    DOWNLOAD_MODES,
    PlatformCapability,
    Requirement,
)


def _registered_keys() -> set[str]:
    """当前环境下实际注册的平台（与 LIGHT/heavy 的判定保持一致）。"""
    return (
        {"netease"}
        | set(musicdl.LIGHT_PLATFORMS)
        | {key for key, _label in musicdl.heavy_available()}
    )


def _by_key(
    capabilities: list[PlatformCapability],
) -> dict[str, PlatformCapability]:
    return {item.key: item for item in capabilities}


# ---------------------------------------------------------------------------
# 矩阵完整性
# ---------------------------------------------------------------------------


def test_every_known_platform_has_a_capability() -> None:
    capabilities = providers.all_capabilities()

    assert {item.key for item in capabilities} == {"netease"} | set(
        musicdl.ALL_PLATFORMS
    )
    for item in capabilities:
        assert item.label
        assert item.group
        assert item.search is True
        assert item.download_mode in DOWNLOAD_MODES
        assert item.download is (item.download_mode != "none")
        assert isinstance(item.credential_fields, tuple)


def test_limitation_and_requirement_vocabulary_is_stable() -> None:
    for item in providers.all_capabilities():
        assert set(item.limitations) <= providers.LIMITATIONS
        for requirement in item.requirements:
            assert requirement.kind in providers.REQUIREMENT_KINDS


def test_registered_capabilities_match_light_and_heavy_lists() -> None:
    expected = _registered_keys()

    assert providers.platform_capabilities() == providers.capabilities()
    assert (
        providers.platform_capabilities(include_unavailable=True)
        == providers.all_capabilities()
    )
    assert {item.key for item in providers.capabilities()} == expected
    assert [item.key for item in providers.capabilities()] == [
        key for key, _label in providers.available()
    ]
    # 未注册的重档平台仍然保留描述，只是 registered=False。
    assert {item.key for item in providers.all_capabilities()} >= expected


def test_available_labels_and_notes_derive_from_capabilities() -> None:
    registered = providers.capabilities()

    assert providers.available() == [(item.key, item.label) for item in registered]
    assert providers.available_labels() == [
        (item.key, f"{item.group} · {item.label}") for item in registered
    ]
    for item in providers.all_capabilities():
        assert providers.platform_note(item.key) == item.note


def test_supports_credentials_matches_credential_fields() -> None:
    for item in providers.all_capabilities():
        assert providers.supports_credentials(item.key) is bool(item.credential_fields)

    assert providers.supports_credentials("netease") is False
    assert providers.supports_credentials("tidal") is True
    assert providers.supports_credentials("migu") is False


def test_capability_lookup_for_unregistered_platform() -> None:
    caps = providers.build_capabilities(registered={"netease"}, probe=lambda req: True)
    by_key = _by_key(caps)

    assert by_key["tidal"].registered is False
    assert providers.capability("tidal").key == "tidal"

    with pytest.raises(KeyError, match="未知平台"):
        providers.capability("no-such-platform")
    assert providers.get_capability("no-such-platform") is None


# ---------------------------------------------------------------------------
# 与 vendor / 注册表依赖表一致
# ---------------------------------------------------------------------------


def test_vendor_heavy_dependencies_are_described() -> None:
    for key, (_label, _module, _cls, dependencies) in musicdl.HEAVY_PLATFORMS.items():
        capability = providers.capability(key)
        python_requirements = {
            item.name for item in capability.requirements if item.kind == "python"
        }
        assert set(dependencies) <= python_requirements

    for key, dependencies in musicdl.PLATFORM_DEPS.items():
        capability = providers.capability(key)
        python_requirements = {
            item.name for item in capability.requirements if item.kind == "python"
        }
        assert set(dependencies) <= python_requirements


def test_tidal_needs_account_tools_and_cdm() -> None:
    capability = providers.capability("tidal")

    assert capability.search is True
    assert capability.download is True
    assert capability.download_mode == "external"
    assert capability.credential_required is True
    assert capability.credential_fields == (
        "access_token",
        "refresh_token",
        "client_id",
        "client_secret",
    )
    names = {item.name for item in capability.requirements}
    assert {"aigpy", "N_m3u8DL-RE", "ffmpeg", "widevine"} <= names
    assert {"requires_credentials", "requires_external_tool"} <= set(
        capability.limitations
    )


def test_apple_and_soundcloud_require_cdm_but_not_mandatory_credentials() -> None:
    for key in ("apple", "soundcloud"):
        capability = providers.capability(key)

        assert capability.search is True
        assert capability.download_mode == "external"
        assert capability.credential_required is False
        assert capability.credential_fields
        assert any(item.kind == "cdm" for item in capability.requirements)
        assert "requires_cdm" in capability.limitations
        assert "drm_protected" in capability.limitations


def test_youtube_is_third_party_and_uses_ytmusicapi() -> None:
    capability = providers.capability("youtube")

    assert capability.download_mode == "third_party"
    assert capability.credential_fields == ()
    assert ("python", "ytmusicapi") in {
        (item.kind, item.name) for item in capability.requirements
    }
    assert "third_party_service" in capability.limitations


def test_netease_requires_login_but_not_platform_credentials() -> None:
    capability = providers.capability("netease")

    assert capability.login_required is True
    assert capability.credential_fields == ()
    assert capability.credential_required is False
    assert capability.download_mode == "direct"
    assert capability.lyrics is True


def test_platforms_without_lyrics_are_explicit() -> None:
    no_lyrics = {
        "audius",
        "bilibili",
        "ccmixter",
        "jiosaavn",
        "opengameart",
        "wikimediacommons",
    }

    for item in providers.all_capabilities():
        assert item.lyrics is (item.key not in no_lyrics)


def test_unknown_credential_field_names_are_never_values() -> None:
    """能力描述只能说字段名，绝不能带出凭据内容。"""
    for item in providers.all_capabilities():
        for field in item.credential_fields:
            assert isinstance(field, str) and field
            assert len(field) < 64


# ---------------------------------------------------------------------------
# 缺依赖 / 缺工具 / 不可用：状态与理由稳定
# ---------------------------------------------------------------------------


def test_missing_python_dependency_is_reported() -> None:
    caps = providers.build_capabilities(
        registered={"netease"}, probe=lambda requirement: False
    )
    by_key = _by_key(caps)

    tidal = by_key["tidal"]
    assert tidal.status == CAPABILITY_MISSING_DEPENDENCY
    assert "aigpy" in tidal.missing
    assert by_key["netease"].status == CAPABILITY_READY


def test_missing_external_tool_is_reported() -> None:
    caps = providers.build_capabilities(
        registered=_registered_keys(),
        probe=lambda requirement: requirement.kind != "tool",
    )
    by_key = _by_key(caps)

    tidal = by_key["tidal"]
    assert tidal.status == CAPABILITY_MISSING_TOOL
    # Order is the declaration order, so the reason list is stable.
    assert tidal.missing == ("N_m3u8DL-RE", "ffmpeg")
    assert by_key["netease"].status == CAPABILITY_READY


def test_unregistered_but_requirements_satisfied_is_unavailable() -> None:
    caps = providers.build_capabilities(
        registered={"netease"}, probe=lambda requirement: True
    )
    by_key = _by_key(caps)

    tidal = by_key["tidal"]
    assert tidal.status == CAPABILITY_UNAVAILABLE
    assert tidal.missing == ()


def test_missing_reasons_are_stable_and_actionable() -> None:
    first = providers.build_capabilities(registered=set(), probe=lambda req: False)
    second = providers.build_capabilities(registered=set(), probe=lambda req: False)

    assert [item.as_dict() for item in first] == [item.as_dict() for item in second]

    tidal = _by_key(first)["tidal"]
    python_req = next(
        item
        for item in tidal.requirements
        if item.name == "aigpy" and item.kind == "python"
    )
    tool_req = next(item for item in tidal.requirements if item.name == "ffmpeg")
    assert python_req.as_dict()["reason"] == "缺少 Python 依赖：aigpy"
    assert tool_req.as_dict()["reason"] == "缺少外部工具：ffmpeg"
    assert "缺少 Python 依赖：aigpy" in {
        item["reason"] for item in tidal.as_dict()["requirements"]
    }


def test_optional_requirement_never_blocks_readiness(monkeypatch) -> None:
    monkeypatch.setitem(
        providers._CAPABILITY_OVERRIDES,
        "migu",
        {"requirements": (Requirement("tool", "optional-tool", optional=True),)},
    )

    caps = providers.build_capabilities(
        registered={"netease"}, probe=lambda requirement: False
    )
    migu = _by_key(caps)["migu"]

    assert migu.missing == ()
    assert migu.status == CAPABILITY_UNAVAILABLE


def test_cdm_requirement_is_not_probed() -> None:
    assert Requirement("cdm", "widevine").probe() is None


# ---------------------------------------------------------------------------
# JSON 序列化
# ---------------------------------------------------------------------------


def test_capabilities_json_is_serializable_and_complete() -> None:
    payload = providers.capabilities_json(include_unavailable=True)

    encoded = json.dumps(payload, ensure_ascii=False)
    assert encoded

    expected_fields = {
        "key",
        "label",
        "group",
        "search",
        "download",
        "download_mode",
        "lyrics",
        "login_required",
        "credential_fields",
        "credential_required",
        "requirements",
        "limitations",
        "note",
        "registered",
        "status",
        "missing",
    }
    for row in payload:
        assert set(row) == expected_fields
        assert isinstance(row["credential_fields"], list)
        assert isinstance(row["requirements"], list)
        assert isinstance(row["limitations"], list)
        assert isinstance(row["missing"], list)


def test_registered_json_matches_available_platforms() -> None:
    payload = providers.capabilities_json()

    assert [row["key"] for row in payload] == [
        key for key, _label in providers.available()
    ]


# ---------------------------------------------------------------------------
# 惰性：列能力不得 import 重型依赖或 vendored 平台
# ---------------------------------------------------------------------------


def test_listing_capabilities_stays_lazy() -> None:
    src = Path(__file__).resolve().parents[2] / "src"
    code = "\n".join(
        [
            "import sys",
            "import novel_downloader.apps.music.providers as p",
            "caps = p.all_capabilities()",
            "assert caps, 'capability matrix is empty'",
            "assert p.capabilities()",
            "assert p.available_labels()",
            "p.capabilities_json(include_unavailable=True)",
            "heavy = {'pywidevine', 'ytmusicapi', 'aigpy', 'mutagen'}",
            "loaded = sorted(heavy & set(sys.modules))",
            "assert not loaded, loaded",
            "sources = [m for m in sys.modules",
            "           if '.providers._vendor.musicdl.modules.sources.' in m]",
            "assert not sources, sources",
            "print('ok')",
        ]
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src) + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(src.parent),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout
