#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.fanqienovel.argus
------------------------------------------------

Long-form ``X-Argus`` generator for the 番茄小说 client API.

Pipeline (variant used by the 7.x client, ``aid=1967``)::

    protobuf bean -> PKCS#7 -> Simon-128/256 (72 rounds)
                  -> XOR/reverse wrap -> "ao" -> AES-128-CBC -> base64

The nine-byte head and the ``f2 81`` output prefix are fixed; everything
else is derived from the public ByteDance root key.  The encoding here is
byte-for-byte identical to what the official client sends (verified against
captured requests).
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct
from typing import Final

from Crypto.Cipher import AES

#: Public ByteDance root key shared by TikTok / Douyin / fqnovel.
ROOT_KEY: Final = bytes.fromhex(
    "ac1adaae95a7af94a5114ab3b3a97dd80050aa0a39314c40528caec95256c28c"
)
#: Simon-128/256 key: ``sm3(ROOT_KEY + b"\xf2\x81ao" + ROOT_KEY)``.
SIGN_KEY: Final = bytes.fromhex(
    "fc78e0a9657a0c748ce51559903ccf03510e51d3cff232d71343e88a321c5304"
)
AES_KEY: Final = hashlib.md5(ROOT_KEY[:16]).digest()
AES_IV: Final = hashlib.md5(ROOT_KEY[16:]).digest()

MAGIC8: Final = bytes([0xF2, 0xF7, 0xFC, 0xFF] * 2)
TAIL9: Final = bytes([0xEA, 0xFB, 0x2C, 0xFE, 0x85, 0x68, 0x51, 0x91, 0x54])
OUT_PREFIX: Final = bytes([0xF2, 0x81])

_M32: Final = 0xFFFFFFFF
_M64: Final = 0xFFFFFFFFFFFFFFFF
_F1_MAGIC: Final = 1077940818  # 0x20200929 << 1
_LICENSE_ID: Final = 1611921764
_SDK_STRING: Final = "v04.04.05-ov-android"
_SDK_INT: Final = 134744640


# --------------------------------------------------------------------------- #
# SM3
# --------------------------------------------------------------------------- #
def _rotl32(value: int, count: int) -> int:
    count %= 32
    return ((value << count) | (value >> (32 - count))) & _M32


def sm3(data: bytes) -> bytes:
    """Return the SM3 digest of ``data``."""
    state = [
        0x7380166F,
        0x4914B2B9,
        0x172442D7,
        0xDA8A0600,
        0xA96F30BC,
        0x163138AA,
        0xE38DEE4D,
        0xB0FB0E4E,
    ]
    message = bytearray(data) + b"\x80"
    while len(message) % 64 != 56:
        message.append(0)
    message += struct.pack(">Q", len(data) * 8)

    for offset in range(0, len(message), 64):
        chunk = list(struct.unpack(">16I", bytes(message[offset : offset + 64])))
        for j in range(16, 68):
            x = chunk[j - 16] ^ chunk[j - 9] ^ _rotl32(chunk[j - 3], 15)
            x ^= _rotl32(x, 15) ^ _rotl32(x, 23)
            chunk.append(x ^ _rotl32(chunk[j - 13], 7) ^ chunk[j - 6])
        derived = [chunk[j] ^ chunk[j + 4] for j in range(64)]
        a, b, c, d, e, f, g, h = state
        for j in range(64):
            const = 0x79CC4519 if j < 16 else 0x7A879D8A
            ss1 = _rotl32((_rotl32(a, 12) + e + _rotl32(const, j)) & _M32, 7)
            ss2 = ss1 ^ _rotl32(a, 12)
            if j < 16:
                ff, gg = a ^ b ^ c, e ^ f ^ g
            else:
                ff = (a & b) | (a & c) | (b & c)
                gg = (e & f) | (~e & g)
            tt1 = (ff + d + ss2 + derived[j]) & _M32
            tt2 = (gg + h + ss1 + chunk[j]) & _M32
            d, c, b, a = c, _rotl32(b, 9), a, tt1
            h, g, f, e = (
                g,
                _rotl32(f, 19),
                e,
                tt2 ^ _rotl32(tt2, 9) ^ _rotl32(tt2, 17),
            )
        state = [
            old ^ new for old, new in zip(state, (a, b, c, d, e, f, g, h), strict=True)
        ]
    return struct.pack(">8I", *state)


# --------------------------------------------------------------------------- #
# Simon-128/256 (72 rounds, the variant this client uses)
# --------------------------------------------------------------------------- #
def _rotr64(value: int, count: int) -> int:
    count %= 64
    return ((value >> count) | (value << (64 - count))) & _M64


def _rotl64(value: int, count: int) -> int:
    count %= 64
    return ((value << count) | (value >> (64 - count))) & _M64


def _simon_schedule(key: bytes) -> list[int]:
    words = list(struct.unpack("<4Q", key)) + [0] * 68
    for i in range(4, 72):
        tmp = _rotr64(words[i - 1], 3) ^ words[i - 3]
        tmp ^= _rotr64(tmp, 1)
        words[i] = (
            (~words[i - 4] & _M64)
            ^ tmp
            ^ ((0x3DC94C3A046D678B >> ((i - 4) % 62)) & 1)
            ^ 3
        )
    return words


def _simon_encrypt_block(block: bytes, schedule: list[int]) -> bytes:
    x, y = struct.unpack("<2Q", block)
    for i in range(72):
        x, y = (
            y,
            (x ^ (_rotl64(y, 1) & _rotl64(y, 8)) ^ _rotl64(y, 2) ^ schedule[i]) & _M64,
        )
    return struct.pack("<2Q", x, y)


def _pkcs7(data: bytes, block: int = 16) -> bytes:
    pad = block - len(data) % block
    return data + bytes([pad]) * pad


# --------------------------------------------------------------------------- #
# Protobuf helpers (varints are masked to 32 bits, like the reference)
# --------------------------------------------------------------------------- #
def _varint(value: int) -> bytes:
    value &= _M32
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _pb_int(field: int, value: int) -> bytes:
    return _tag(field, 0) + _varint(value)


def _pb_bytes(field: int, data: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(data)) + data


def _pb_str(field: int, text: str) -> bytes:
    return _pb_bytes(field, text.encode())


def _query_param(query: str, name: str) -> str:
    for part in query.split("&"):
        key, _, value = part.partition("=")
        if key == name:
            return value
    return ""


def _hash6(data: bytes) -> bytes:
    return sm3(data)[:6]


# --------------------------------------------------------------------------- #
# Bean / framing
# --------------------------------------------------------------------------- #
def build_bean(
    query: str,
    *,
    timestamp: int,
    device_id: str,
    version_name: str,
    stub_hex: str = "",
    field3: int | None = None,
) -> bytes:
    """Serialize the argus bean exactly as the client does."""
    nonce = (
        int.from_bytes(os.urandom(4), "little") & 0x7FFFFFFF
        if field3 is None
        else field3
    )
    version = _query_param(query, "version_name") or version_name
    stub_hash = _hash6(bytes.fromhex(stub_hex)) if stub_hex else _hash6(bytes(16))
    bean = bytearray()
    bean += _pb_int(1, _F1_MAGIC)
    bean += _pb_int(2, 2)
    bean += _pb_int(3, nonce)
    bean += _pb_str(4, "1967")
    bean += _pb_str(5, device_id)
    bean += _pb_str(6, str(_LICENSE_ID))
    bean += _pb_str(7, version)
    bean += _pb_str(8, _SDK_STRING)
    bean += _pb_int(9, _SDK_INT)
    bean += _pb_bytes(10, bytes(8))
    bean += _pb_int(11, 0)
    bean += _pb_int(12, timestamp << 1)
    bean += _pb_bytes(13, stub_hash)
    bean += _pb_bytes(14, _hash6(query.encode()))
    bean += _pb_bytes(
        15, _pb_int(1, 1) + _pb_int(2, 1) + _pb_int(3, 1) + _pb_int(7, 0xC792ECCC)
    )
    bean += _pb_str(16, "")
    bean += _pb_str(20, "none")
    bean += _pb_int(21, 738)
    bean += _pb_bytes(
        23, _pb_str(1, "NX551J") + _pb_int(2, 8196) + _pb_int(4, 0x80E0D800)
    )
    bean += _pb_int(25, 2)
    return bytes(bean)


def x_argus(
    query: str,
    timestamp: int,
    *,
    device_id: str,
    version_name: str = "7.4.1.32",
    stub_hex: str = "",
    field3: int | None = None,
) -> str:
    """Return a fresh ``X-Argus`` header value for ``query``."""
    bean = build_bean(
        query,
        timestamp=timestamp,
        device_id=device_id,
        version_name=version_name,
        stub_hex=stub_hex,
        field3=field3,
    )
    schedule = _simon_schedule(SIGN_KEY)
    padded = _pkcs7(bean)
    encrypted = b"".join(
        _simon_encrypt_block(padded[i : i + 16], schedule)
        for i in range(0, len(padded), 16)
    )

    wrapped = bytearray(len(MAGIC8) + len(encrypted) + len(TAIL9))
    wrapped[: len(MAGIC8)] = MAGIC8
    wrapped[len(MAGIC8) : len(MAGIC8) + len(encrypted)] = encrypted
    wrapped[len(MAGIC8) + len(encrypted) :] = TAIL9
    for i in range(len(MAGIC8), len(wrapped)):
        wrapped[i] ^= MAGIC8[i % 8]

    plain = bytes(reversed(wrapped)) + b"ao"
    plain = _pkcs7(plain)
    cipher = AES.new(AES_KEY, AES.MODE_CBC, AES_IV).encrypt(plain)
    return base64.b64encode(OUT_PREFIX + cipher).decode()
