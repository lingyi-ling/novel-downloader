#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.fanqienovel.appapi
-------------------------------------------------

Client for the official Fanqie app reader API.

The web front end (``fanqienovel.com``) only serves a teaser for chapters the
platform wants to paywall, while the app API hands out the full text to any
device that has registered a content key.  This module implements the parts
needed to use that API from this project:

* ``x-gorgon`` request signing (algorithm id ``8404``),
* loading a device identity (``device_id`` / ``install_id`` / ``content_key``),
* AES-128-CBC decryption of the returned chapter body.

Everything here is derived from public protocol descriptions; see
``docs/notes/fanqie-client-protocol.md`` for the observed request shape and
for the parts that are still unknown (notably the content-key registration
endpoint).
"""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

logger = logging.getLogger(__name__)

#: Environment variable holding a path to a device identity JSON file.
DEVICE_ENV_VAR: Final = "NOVELDL_FANQIE_DEVICE"

#: File name looked up inside the site cache directory.
DEVICE_FILENAME: Final = "device.json"

#: Host serving the reader API.  ``reading.snssdk.com`` is the one the web
#: reader used before it was locked down, and the one that answers today.
READER_HOST: Final = "https://reading.snssdk.com"
READER_PATH: Final = "/reading/reader/full/v/"

#: User agent of the official client.  Anything else is rejected outright.
READER_USER_AGENT: Final = "com.dragon.read"

#: ``version_code`` deliberately stays low.  Newer codes turn on the
#: ``x-medusa`` / ``x-helios`` signature gate, which needs a native library we
#: cannot reproduce; the old code path only checks ``x-gorgon``.
READER_VERSION_CODE: Final = "66.9"

_CONTENT_KEY_LENGTH: Final = 16
_GORGON_HEX_DIGITS: Final = (30, 64, 224, 217, 147, 69, 0, 180)
_GORGON_LENGTH: Final = 20
_M32: Final = 0xFFFFFFFF
_M8: Final = 0xFF


class DeviceIdentityError(ValueError):
    """Raised when a device identity file cannot be used."""


@dataclass(frozen=True)
class FanqieDevice:
    """A registered device plus the AES key its chapter bodies are wrapped in."""

    device_id: str
    install_id: str
    content_key: bytes
    key_version: int | None = None
    source: str = ""

    def __post_init__(self) -> None:
        if not self.device_id.isdigit() or not self.install_id.isdigit():
            raise DeviceIdentityError("device_id/install_id must be numeric")
        if len(self.content_key) != _CONTENT_KEY_LENGTH:
            raise DeviceIdentityError("content_key must decode to 16 bytes")

    @property
    def masked(self) -> str:
        """Identifier safe to log: last four digits only."""
        return f"…{self.device_id[-4:]}"


def sign_query(
    query: str,
    *,
    khronos: int | None = None,
    key_bytes: bytes | None = None,
    stub: bytes | None = None,
) -> tuple[str, int]:
    """Return ``(x-gorgon, x-khronos)`` for a raw query string.

    The timestamp is part of the signature, so both values must be sent
    together; ``khronos`` exists only so tests can pin the clock.
    """
    timestamp = int(time.time()) if khronos is None else khronos
    if key_bytes is None:
        key_bytes = os.urandom(2)
    if len(key_bytes) != 2:
        raise ValueError("key_bytes must be two bytes")

    digest = hashlib.md5(query.encode()).digest()[:4]
    data = list(
        digest
        + b"\0" * 4
        + bytes((0, 0, 0, 0, 0, 1, 7, 4))
        + timestamp.to_bytes(4, "big")
    )
    # Key schedule.  This is *not* RC4's swap: only the entry at ``i`` is
    # rewritten, which is what the reference implementation does.
    table = list(range(256))
    warm = 0
    key = [0x4A, 0x00, 0x16, key_bytes[1], 0x47, 0x6C, 0x00, key_bytes[0]]
    for i in range(256):
        warm = (warm + table[i] + key[i & 7]) & _M32
        slot = warm & _M8
        table[i] = table[slot]
        warm = slot

    length = len(data)
    state = 0
    for i in range(1, length + 1):
        index = i - 1
        shifted = ((index & 1) << 1) & 2
        shifted = (shifted + (index ^ 1)) & _M8
        first = table[shifted]
        block = state ^ first
        state = ((((state | first) << 1) & _M32) - block) & _M32
        second = state & _M8
        value = table[second]
        table[shifted] = value
        table[second] = value
        data[index] ^= table[(value + value) & _M8]

    scratch = ~length & _M32
    for i in range(1, length + 1):
        index = i - 1
        value = data[index]
        swapped = ((value >> 4) & 0x0F) | ((value & 0x0F) << 4)
        data[index] = swapped & _M8
        if i == length:
            data[index] = data[0] ^ swapped
        else:
            nxt = data[index + 1]
            data[index] = ((swapped | nxt) - (swapped & nxt)) & _M8

        rolling = data[index]
        rolling = (0xFFFFFFAA & (rolling << 1)) | (0x55 & (rolling >> 1))
        mixed = ((rolling << 2) & 0xFFFFFFCF) | (0x33 & (rolling >> 2))
        mixed = ((mixed & 0x0FFFFFFF) << 4) | ((mixed >> 4) & 0x0F)
        data[index] = (mixed ^ scratch) & _M8

    if length != _GORGON_LENGTH:  # pragma: no cover - structural guard
        raise ValueError(f"unexpected gorgon length {length}")
    prefix = bytes((0x84, 0x04, key_bytes[0], key_bytes[1], 0x00, 0x00))
    return (prefix + bytes(data)).hex(), timestamp


def build_reader_query(device: FanqieDevice, item_id: str) -> str:
    """Query string for one chapter, in the exact order the signature covers."""
    return (
        f"aid=1967&app_name=novelapp&channel=0&device_platform=android"
        f"&device_id={device.device_id}&device_type=Honor10"
        f"&item_id={item_id}&os_version=0&version_code={READER_VERSION_CODE}"
    )


def reader_url(device: FanqieDevice, item_id: str) -> str:
    """Full reader URL for one chapter."""
    return f"{READER_HOST}{READER_PATH}?{build_reader_query(device, item_id)}"


def build_batch_query(device: FanqieDevice, item_ids: list[str]) -> str:
    """Query string for a batch of chapters (same host, up to ~20)."""
    joined = ",".join(item_ids)
    return (
        f"aid=1967&app_name=novelapp&channel=0&device_platform=android"
        f"&device_id={device.device_id}&device_type=Honor10&book_id=0"
        f"&item_ids={joined}&novel_text_type=1&os_version=0"
        f"&req_type=1&version_code={READER_VERSION_CODE}"
    )


def batch_url(device: FanqieDevice, item_ids: list[str]) -> str:
    """Full batch reader URL."""
    return (
        "https://api5-normal-sinfonlineb.fqnovel.com/reading/reader/batch_full/v"
        f"?{build_batch_query(device, item_ids)}"
    )


def decrypt_content(cipher_text: str, key: bytes) -> str:
    """Decrypt a ``data.content`` body and undo optional gzip compression."""
    raw = base64.b64decode(cipher_text)
    if len(raw) <= 16:
        raise DeviceIdentityError("cipher text is too short")
    plain = unpad(AES.new(key, AES.MODE_CBC, raw[:16]).decrypt(raw[16:]), 16)
    if plain[:2] == b"\x1f\x8b":
        plain = gzip.decompress(plain)
    return plain.decode("utf-8", "replace")


def to_reader_page(title: str, content_html: str) -> str:
    """Wrap app-API output in the page shape the site parser already handles.

    ``source`` tells the parser this text never went through the web font
    obfuscation, so it must not warn about the missing webfont.
    """
    state = {
        "reader": {
            "source": "app-api",
            "chapterData": {
                "title": title,
                "content": content_html,
                "isChapterLock": False,
                "needPay": 0,
            },
        }
    }
    payload = json.dumps(state, ensure_ascii=False)
    return (
        "<html><head><title>"
        + title.replace("<", "&lt;")
        + "</title></head><body><script>window.__INITIAL_STATE__ = "
        + payload
        + ";</script></body></html>"
    )


def _decode_key(value: str) -> bytes:
    """Accept base64 (with or without padding) and 32-char hex."""
    text = value.strip()
    if len(text) == 32:
        try:
            return bytes.fromhex(text)
        except ValueError:
            pass
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DeviceIdentityError("content_key is neither base64 nor hex") from exc


def load_device(path: Path) -> FanqieDevice:
    """Read a device identity JSON file."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DeviceIdentityError(f"cannot read {path}: {exc}") from exc
    except ValueError as exc:
        raise DeviceIdentityError(f"{path} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise DeviceIdentityError(f"{path} does not contain an object")

    try:
        device_id = str(payload["device_id"])
        install_id = str(payload["install_id"])
        key = _decode_key(str(payload["content_key"]))
    except KeyError as exc:
        raise DeviceIdentityError(f"{path} is missing {exc.args[0]!r}") from exc

    version = payload.get("content_key_version", payload.get("key_version"))
    key_version: int | None = None
    if isinstance(version, int):
        key_version = version
    elif isinstance(version, str) and version.isdigit():
        key_version = int(version)

    return FanqieDevice(
        device_id=device_id,
        install_id=install_id,
        content_key=key,
        key_version=key_version,
        source=str(path),
    )


def device_candidates(cache_dir: Path) -> list[Path]:
    """Identity files to try, in priority order."""
    candidates: list[Path] = []
    override = os.environ.get(DEVICE_ENV_VAR, "").strip()
    if override:
        candidates.append(Path(override))
    candidates.append(cache_dir / DEVICE_FILENAME)

    profile = os.environ.get("APPDATA")
    names = ("fanqie_primary_device.json", "device.json")
    roots = [Path(profile)] if profile else [Path.home() / ".config"]
    for root in roots:
        for name in names:
            candidates.append(root / "com.pofl.fanqienoveldownloader" / name)
    return candidates


def discover_device(cache_dir: Path) -> FanqieDevice | None:
    """Return the first usable device identity, or ``None``."""
    for path in device_candidates(cache_dir):
        if not path.is_file():
            continue
        try:
            device = load_device(path)
        except DeviceIdentityError as exc:
            logger.info("fanqienovel: ignoring device file %s (%s)", path, exc)
            continue
        logger.info("fanqienovel: using device %s from %s", device.masked, path)
        return device
    return None
