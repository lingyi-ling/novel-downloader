#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.fanqienovel.register
---------------------------------------------------

Self-service device registration for the app reader API.

Two steps, both signed by us:

1. ``POST /service/2/device_register/`` (applog) with an ``X-Gorgon`` query
   signature and an MD5 body stub -> ``device_id`` / ``install_id``.
2. ``POST /reading/crypt/registerkey`` with a self-computed ``X-Argus`` ->
   a server-issued content key, wrapped with the public master key.

The resulting identity is stored next to the site cache so later runs reuse
it.  Nothing here needs the official app.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import os
import random
import struct
import time
import uuid
from typing import Any, Final
from urllib.parse import urlencode

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from novel_downloader.plugins.sites.fanqienovel.appapi import (
    DEVICE_FILENAME,
    FanqieDevice,
    sign_query,
)
from novel_downloader.plugins.sites.fanqienovel.argus import x_argus

logger = logging.getLogger(__name__)

#: AES key wrapping the content key in the registerkey response.
MASTER_KEY: Final = bytes.fromhex("ac25c67ddd8f38c1b37a2348828e222e")

#: ``x-ladon`` seeds captured from the official client.  The value is a
#: signed ``"{ts}-{license}-{aid}"`` envelope; the server accepts any valid
#: one regardless of the timestamp it carries, so a small pool is enough.
LADON_SEEDS: Final = (
    "BQsH+rWlg0BS/umXcHBy+jGh2JwW3x2kHUpHmVNF3cgof3MP",
    "07KhztgRGZydIx5SAXD6UOM1Lq1VmsmA8llirfhLfIN4ixGY",
)

REGISTER_URL: Final = "https://log5-applog.fqnovel.com/service/2/device_register/"
KEY_URL: Final = "https://reading.snssdk.com/reading/crypt/registerkey"
READER_VERSION_NAME: Final = "7.4.1.32"
CLIENT_USER_AGENT: Final = (
    "com.dragon.read/74132 (Linux; U; Android 14; zh_CN; SM-G9750; "
    "Build/coloros__8E20.714880.55611153 release-keys; "
    "Cronet/TTNetVersion:8d40f833 2026-03-03 QuicVersion:21ac1950 2025-11-18)"
)
_REGISTER_USER_AGENT: Final = (
    "com.dragon.read.oversea.gp/68132 (Linux; U; Android 13; zh_CN; "
    "ANY-AN00; Build/V433IR;tt-ok/3.12.13.4-tiktok)"
)

_MODELS: Final = (
    ("Xiaomi", "24031PN0DC"),
    ("HONOR", "ANY-AN00"),
    ("OPPO", "CPH2371"),
    ("vivo", "V2197A"),
)


def _device_profile() -> dict[str, Any]:
    brand, model = random.choice(_MODELS)
    android_id = "".join(random.choices("0123456789abcdef", k=16))
    digest = hashlib.md5(android_id.encode()).hexdigest()
    now_ms = int(time.time() * 1000)
    return {
        "brand": brand,
        "model": model,
        "now_ms": now_ms,
        "install_id": str(random.randint(10**15, 10**16 - 1)),
        "cdid": str(uuid.uuid4()),
        "openudid": digest + hashlib.md5(digest.encode()).hexdigest()[:8],
        "req_id": uuid.uuid4().hex,
        "first_install": now_ms - 86400000,
    }


def _register_body(profile: dict[str, Any]) -> tuple[str, bytes, str]:
    params = urlencode(
        {
            "aid": "1967",
            "version_code": "74132",
            "channel": "43536163a",
            "package": "com.dragon.read",
            "_rticket": str(profile["now_ms"]),
            "use_store_region_cookie": "1",
            "okhttp_version": "4.2.137.76-fanqie",
        }
    )
    header = {
        "display_name": "番茄小说",
        "aid": 1967,
        "channel": "43536163a",
        "package": "com.dragon.read",
        "app_version": READER_VERSION_NAME,
        "version_code": 74132,
        "update_version_code": 74132,
        "manifest_version_code": 74132,
        "app_version_minor": READER_VERSION_NAME,
        "os": "Android",
        "os_version": "13",
        "os_api": 32,
        "device_model": profile["model"],
        "device_brand": profile["brand"],
        "device_manufacturer": profile["brand"],
        "cpu_abi": "arm64-v8a",
        "device_platform": "android",
        "language": "zh",
        "region": "CN",
        "timezone": 8,
        "access": "wifi",
        "resolution": "720*1280",
        "density_dpi": 240,
        "display_density": "hdpi",
        "cdid": profile["cdid"],
        "openudid": profile["openudid"],
        "req_id": profile["req_id"],
        "guest_mode": 0,
        "apk_first_install_time": profile["first_install"],
    }
    payload = {
        "magic_tag": "ss_app_log",
        "header": header,
        "_gen_time": profile["now_ms"],
    }
    body = json.dumps(payload, ensure_ascii=False).encode()
    return params, body, profile["install_id"]


async def _post(session: Any, url: str, headers: dict[str, str], body: bytes) -> str:
    resp = await session.post(url, headers=headers, data=body)
    if resp.status != 200:
        return ""
    return str(resp.text)


async def _register(session: Any) -> tuple[str, str] | None:
    profile = _device_profile()
    params, body, iid = _register_body(profile)
    gorgon, khronos = sign_query(params, stub=hashlib.md5(body).digest()[:4])
    headers = {
        "User-Agent": _REGISTER_USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "Content-Type": "application/json",
        "x-ss-stub": hashlib.md5(body).hexdigest().upper(),
        "x-ss-req-ticket": str(profile["now_ms"]),
        "Cookie": f"store-region=cn-zj; store-region-src=did; install_id={iid}",
        "X-Gorgon": gorgon,
        "X-Khronos": str(khronos),
    }
    text = await _post(session, f"{REGISTER_URL}?{params}", headers, body)
    try:
        payload = json.loads(text)
    except ValueError:
        logger.info("fanqienovel: device registration returned non-JSON")
        return None
    device_id = payload.get("device_id") if isinstance(payload, dict) else None
    if not device_id:
        logger.info("fanqienovel: device registration was refused")
        return None
    return str(device_id), str(payload.get("install_id") or iid)


async def _register_key(
    session: Any, device_id: str, install_id: str, ladon_value: str
) -> tuple[bytes, int] | None:
    timestamp = int(time.time())
    query = (
        f"device_id={device_id}&iid={install_id}&device_type=SM-G9750&aid=1967"
        f"&app_name=novelapp&channel=0&device_platform=android&os_version=0"
        f"&version_code=6.3.9.32"
    )
    iv = os.urandom(16)
    plain = struct.pack("<QQ", int(device_id), 0)
    content = base64.b64encode(
        iv + AES.new(MASTER_KEY, AES.MODE_CBC, iv).encrypt(_pad(plain))
    ).decode()
    body = gzip.compress(
        json.dumps({"content": content, "keyver": 1}).encode(), 9, mtime=0
    )
    stub = hashlib.md5(body).hexdigest().upper()
    seed = ladon_value
    headers = {
        "user-agent": CLIENT_USER_AGENT,
        "accept": "application/json",
        "accept-encoding": "identity",
        "x-ss-req-ticket": str(int(time.time() * 1000)),
        "x-khronos": str(timestamp),
        "x-ss-stub": stub,
        "content-type": "application/json; charset=utf-8",
        "content-encoding": "gzip",
        "x-argus": x_argus(
            query,
            timestamp,
            device_id=device_id,
            version_name=READER_VERSION_NAME,
            stub_hex=stub,
        ),
        "x-ladon": seed,
    }
    text = await _post(session, f"{KEY_URL}?{query}", headers, body)
    if not text:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    wrapped = data.get("key")
    if not isinstance(wrapped, str) or not wrapped:
        return None
    raw = base64.b64decode(wrapped)
    if len(raw) <= 16:
        return None
    key = unpad(AES.new(MASTER_KEY, AES.MODE_CBC, raw[:16]).decrypt(raw[16:]), 16)
    version = data.get("keyver")
    return key, version if isinstance(version, int) else 0


def _pad(data: bytes, block: int = 16) -> bytes:
    n = block - len(data) % block
    return data + bytes([n]) * n


def ladon_candidates() -> list[str]:
    """Seeds to try, the environment override first."""
    override = os.environ.get("NOVELDL_FANQIE_LADON", "").strip()
    return ([override] if override else []) + list(LADON_SEEDS)


async def mint_device(session: Any, cache_dir: Any) -> FanqieDevice | None:
    """Register a device and its content key, then persist the identity."""
    pair = await _register(session)
    if pair is None:
        return None
    issued = None
    for seed in ladon_candidates():
        issued = await _register_key(session, pair[0], pair[1], seed)
        if issued is not None:
            break
    if issued is None:
        logger.info("fanqienovel: content key registration was refused")
        return None
    key, version = issued
    device = FanqieDevice(
        device_id=pair[0],
        install_id=pair[1],
        content_key=key,
        key_version=version,
        source=str(cache_dir / DEVICE_FILENAME),
    )
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / DEVICE_FILENAME).write_text(
            json.dumps(
                {
                    "device_id": device.device_id,
                    "install_id": device.install_id,
                    "content_key": base64.b64encode(device.content_key).decode(),
                    "content_key_version": device.key_version,
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        logger.warning("fanqienovel: could not persist the new device identity")
    logger.info("fanqienovel: registered a new device %s", device.masked)
    return device
