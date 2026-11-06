#!/usr/bin/env python3
"""Protocol tests for :mod:`...fanqienovel.appapi`.

The signature routine is pinned against a vector produced by the reference
implementation, so a refactor cannot silently change the bytes the server
receives.  Everything else here is offline.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
from pathlib import Path

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from novel_downloader.plugins.sites.fanqienovel import appapi
from novel_downloader.plugins.sites.fanqienovel.appapi import (
    FanqieDevice,
    build_reader_query,
    decrypt_content,
    discover_device,
    load_device,
    reader_url,
    sign_query,
    to_reader_page,
)

_QUERY = (
    "aid=1967&app_name=novelapp&channel=0&device_platform=android"
    "&device_id=1026320456490435&device_type=Honor10"
    "&item_id=7704667598306165273&os_version=0&version_code=66.9"
)
_GORGON = "8404112200004b1be6e16ebf6fbcf9c246e3b3033ee17baf68d9"


def _device() -> FanqieDevice:
    return FanqieDevice(
        device_id="1026320456490435",
        install_id="1026320456494531",
        content_key=b"\x01" * 16,
    )


def _encrypt(plain: bytes, key: bytes, iv: bytes = b"\x00" * 16) -> str:
    body = pad(plain, 16)
    return base64.b64encode(iv + AES.new(key, AES.MODE_CBC, iv).encrypt(body)).decode()


# --------------------------------------------------------------------------- #
# Request signing
# --------------------------------------------------------------------------- #
def test_sign_query_matches_pinned_vector() -> None:
    gorgon, khronos = sign_query(_QUERY, khronos=1700000000, key_bytes=b"\x11\x22")

    assert gorgon == _GORGON
    assert khronos == 1700000000


def test_sign_query_prefix_carries_the_key_seed() -> None:
    gorgon, _ = sign_query(_QUERY, khronos=1, key_bytes=b"\xab\xcd")

    assert gorgon.startswith("8404abcd")
    assert len(gorgon) == 52


def test_sign_query_rejects_short_seed() -> None:
    with pytest.raises(ValueError):
        sign_query(_QUERY, khronos=1, key_bytes=b"\x00")


def test_reader_url_uses_the_low_version_code() -> None:
    url = reader_url(_device(), "7704667598306165273")

    assert url == f"https://reading.snssdk.com/reading/reader/full/v/?{_QUERY}"
    assert build_reader_query(_device(), "7704667598306165273") == _QUERY


# --------------------------------------------------------------------------- #
# Content decryption
# --------------------------------------------------------------------------- #
def test_decrypt_content_roundtrip() -> None:
    key = b"k" * 16
    blob = _encrypt("<p>正文</p>".encode(), key)

    assert decrypt_content(blob, key) == "<p>正文</p>"


def test_decrypt_content_handles_gzip_body() -> None:
    key = b"k" * 16
    blob = _encrypt(gzip.compress("<p>压缩</p>".encode(), mtime=0), key)

    assert decrypt_content(blob, key) == "<p>压缩</p>"


def test_decrypt_content_rejects_wrong_key() -> None:
    blob = _encrypt("<p>正文</p>".encode(), b"k" * 16)

    with pytest.raises(ValueError):
        decrypt_content(blob, b"z" * 16)


# --------------------------------------------------------------------------- #
# Device identity loading
# --------------------------------------------------------------------------- #
def test_load_device_accepts_base64_key(tmp_path: Path) -> None:
    path = tmp_path / "device.json"
    path.write_text(
        json.dumps(
            {
                "device_id": "1234567890123456",
                "install_id": "1234567890123457",
                "content_key": base64.b64encode(b"\x02" * 16).decode(),
                "content_key_version": 42,
            }
        ),
        encoding="utf-8",
    )

    device = load_device(path)

    assert device.content_key == b"\x02" * 16
    assert device.key_version == 42


def test_load_device_accepts_hex_key(tmp_path: Path) -> None:
    path = tmp_path / "device.json"
    path.write_text(
        json.dumps(
            {
                "device_id": "1",
                "install_id": "2",
                "content_key": "ab" * 16,
            }
        ),
        encoding="utf-8",
    )

    assert load_device(path).content_key == bytes.fromhex("ab" * 16)


def test_load_device_rejects_short_key(tmp_path: Path) -> None:
    path = tmp_path / "device.json"
    path.write_text(
        json.dumps(
            {
                "device_id": "1",
                "install_id": "2",
                "content_key": base64.b64encode(b"short").decode(),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(appapi.DeviceIdentityError):
        load_device(path)


def test_load_device_rejects_missing_field(tmp_path: Path) -> None:
    path = tmp_path / "device.json"
    path.write_text(json.dumps({"device_id": "1"}), encoding="utf-8")

    with pytest.raises(appapi.DeviceIdentityError):
        load_device(path)


def test_device_masks_identifier() -> None:
    assert _device().masked == "…0435"


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def _write_device(path: Path, key: bytes = b"\x03" * 16) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "device_id": "555",
                "install_id": "666",
                "content_key": base64.b64encode(key).decode(),
            }
        ),
        encoding="utf-8",
    )


def test_discover_device_prefers_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "explicit.json"
    _write_device(override)
    monkeypatch.setenv(appapi.DEVICE_ENV_VAR, str(override))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))

    found = discover_device(tmp_path / "cache")

    assert found is not None
    assert found.source == str(override)


def test_discover_device_falls_back_to_site_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(appapi.DEVICE_ENV_VAR, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    cache = tmp_path / "cache"
    _write_device(cache / appapi.DEVICE_FILENAME)

    assert discover_device(cache) is not None


def test_discover_device_skips_unusable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(appapi.DEVICE_ENV_VAR, raising=False)
    cache = tmp_path / "cache"
    broken = cache / appapi.DEVICE_FILENAME
    broken.parent.mkdir(parents=True)
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))

    assert discover_device(cache) is None


def test_discover_device_returns_none_without_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(appapi.DEVICE_ENV_VAR, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))

    assert discover_device(tmp_path / "cache") is None


def test_device_candidates_are_absolute(tmp_path: Path) -> None:
    for path in appapi.device_candidates(tmp_path):
        assert path.name.endswith(".json")
        assert os.sep in str(path)


# --------------------------------------------------------------------------- #
# Parser compatibility
# --------------------------------------------------------------------------- #
def test_reader_page_is_understood_by_the_parser() -> None:
    from novel_downloader.plugins.sites.fanqienovel.parser import FanqienovelParser
    from novel_downloader.schemas import ParserConfig

    page = to_reader_page(
        "第280章 练题", '<article><p idx="0">第一段</p><p idx="1">第二段</p></article>'
    )

    parsed = FanqienovelParser(ParserConfig()).parse_chapter_content(
        [page], "7704667598306165273"
    )

    assert parsed is not None
    assert parsed["title"] == "第280章 练题"
    assert parsed["content"] == "第一段\n第二段"
