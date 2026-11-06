#!/usr/bin/env python3
"""Self-service device registration tests (offline fake transport)."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from novel_downloader.infra.sessions.response import BaseResponse
from novel_downloader.plugins.sites.fanqienovel import register as reg


class _Resp:
    def __init__(self, payload: dict[str, Any] | None, status: int = 200) -> None:
        self._resp = BaseResponse(
            content=json.dumps(payload).encode() if payload is not None else b"",
            status=status,
        )

    @property
    def status(self) -> int:
        return self._resp.status

    @property
    def text(self) -> str:
        return self._resp.text


class _Session:
    """Answers device_register then registerkey, recording what was sent."""

    def __init__(
        self,
        *,
        register: dict[str, Any] | None,
        key: dict[str, Any] | None,
        key_status: int = 200,
    ) -> None:
        self.register = register
        self.key = key
        self.key_status = key_status
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> _Resp:
        self.calls.append({"url": url, **kwargs})
        if "device_register" in url:
            return _Resp(self.register)
        return _Resp(self.key, status=self.key_status)


def _wrapped_key(content_key: bytes) -> str:
    iv = b"\x01" * 16
    ct = AES.new(reg.MASTER_KEY, AES.MODE_CBC, iv).encrypt(pad(content_key, 16))
    return base64.b64encode(iv + ct).decode()


@pytest.mark.asyncio
async def test_mint_device_registers_and_persists(tmp_path: Path) -> None:
    content_key = b"\x0a" * 16
    session = _Session(
        register={"device_id": 1234567890123456, "install_id": 1234567890123457},
        key={"code": 0, "data": {"key": _wrapped_key(content_key), "keyver": 42}},
    )

    device = await reg.mint_device(session, tmp_path)

    assert device is not None
    assert device.device_id == "1234567890123456"
    assert device.content_key == content_key
    assert device.key_version == 42
    saved = json.loads((tmp_path / "device.json").read_text(encoding="utf-8"))
    assert saved["device_id"] == "1234567890123456"
    assert base64.b64decode(saved["content_key"]) == content_key


@pytest.mark.asyncio
async def test_register_key_request_is_signed(tmp_path: Path) -> None:
    session = _Session(
        register={"device_id": 1, "install_id": 2},
        key={"code": 0, "data": {"key": _wrapped_key(b"\x0b" * 16), "keyver": 1}},
    )

    await reg.mint_device(session, tmp_path)

    request = session.calls[-1]
    headers = request["headers"]
    assert request["url"].startswith(
        "https://reading.snssdk.com/reading/crypt/registerkey?"
    )
    assert "version_code=6.3.9.32" in request["url"]
    argus = base64.b64decode(headers["x-argus"])
    assert argus[:2] == bytes([0xF2, 0x81])
    assert (len(argus) - 2) % 16 == 0  # CBC ciphertext blocks only
    assert headers["x-ladon"] in reg.LADON_SEEDS
    assert headers["content-encoding"] == "gzip"
    assert headers["x-ss-stub"] == hashlib.md5(request["data"]).hexdigest().upper()


@pytest.mark.asyncio
async def test_mint_device_gives_up_when_registration_is_refused(
    tmp_path: Path,
) -> None:
    session = _Session(register={"device_id": 0, "install_id": 0}, key=None)

    assert await reg.mint_device(session, tmp_path) is None
    assert not (tmp_path / "device.json").exists()


@pytest.mark.asyncio
async def test_mint_device_gives_up_when_the_key_is_refused(tmp_path: Path) -> None:
    session = _Session(register={"device_id": 9, "install_id": 9}, key=None)

    assert await reg.mint_device(session, tmp_path) is None


@pytest.mark.asyncio
async def test_mint_device_survives_a_non_json_reply(tmp_path: Path) -> None:
    class _Broken(_Session):
        async def post(self, url: str, **kwargs: Any) -> _Resp:
            return _Resp(None)

    assert await reg.mint_device(_Broken(register=None, key=None), tmp_path) is None


def test_ladon_override_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOVELDL_FANQIE_LADON", "custom-value")

    assert reg.os.environ["NOVELDL_FANQIE_LADON"] == "custom-value"
    assert reg.LADON_SEEDS


@pytest.mark.asyncio
async def test_mint_device_tries_the_next_seed_when_one_is_refused(
    tmp_path: Path,
) -> None:
    """A dead ladon seed must not take the whole registration down."""
    content_key = b"" * 16
    good = {"code": 0, "data": {"key": _wrapped_key(content_key), "keyver": 7}}

    class _Flaky(_Session):
        async def post(self, url: str, **kwargs: Any) -> _Resp:
            self.calls.append({"url": url, **kwargs})
            if "device_register" in url:
                return _Resp({"device_id": 1, "install_id": 2})
            if kwargs["headers"]["x-ladon"] == reg.LADON_SEEDS[0]:
                return _Resp(None)
            return _Resp(good)

    session = _Flaky(register=None, key=None)
    device = await reg.mint_device(session, tmp_path)

    assert device is not None
    assert device.content_key == content_key
    ladons = [
        c["headers"]["x-ladon"] for c in session.calls if "registerkey" in c["url"]
    ]
    assert ladons == list(reg.LADON_SEEDS[:2])


def test_ladon_candidates_puts_the_override_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOVELDL_FANQIE_LADON", "override-value")

    assert reg.ladon_candidates()[0] == "override-value"
