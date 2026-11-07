#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.fanqienovel.ladon
------------------------------------------------

``X-Ladon`` generator for the 番茄小说 client API.

The value is a 36-byte envelope base64-encoded to 48 characters::

    salt(4 random bytes) || encrypt("{ts}-{license}-{aid}" + PKCS#7)

The block cipher is the Speck-128/256 round function the client uses:
34 rounds of ``y = (ror8(y) + x) ^ k`` / ``x = rol3(x) ^ y`` over 16-byte
blocks, keyed by the **hex string** of ``md5(salt + str(aid))``.

Verified byte-for-byte against requests captured from the official client.
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Final

_M64: Final = 0xFFFFFFFFFFFFFFFF
_ROUNDS: Final = 34

#: Client defaults observed on the wire.
LICENSE_ID: Final = 1611921764
AID: Final = 1967


def _ror(value: int, count: int) -> int:
    count %= 64
    return ((value >> count) | (value << (64 - count))) & _M64


def _rol(value: int, count: int) -> int:
    count %= 64
    return ((value << count) | (value >> (64 - count))) & _M64


def _schedule(key_hex: bytes) -> list[int]:
    """Expand the 32-byte key material into 34 round keys.

    ``k[0] = q0``; the feeder words are ``q1, q2, q3`` and then the previous
    round's cipher word, exactly as the client's loop does it.
    """
    q = [int.from_bytes(key_hex[i * 8 : i * 8 + 8], "little") for i in range(4)]
    k = q[0]
    feeders = [q[1], q[2], q[3]]
    produced: list[int] = []
    keys = [k]
    for i in range(_ROUNDS):
        feeder = feeders[i] if i < len(feeders) else produced[i - len(feeders)]
        word = ((_ror(feeder, 8) + k) & _M64) ^ i
        k = (_rol(k, 3) ^ word) & _M64
        keys.append(k)
        produced.append(word)
    return keys[:_ROUNDS]


def _encrypt_block(block: bytes, keys: list[int]) -> bytes:
    x = int.from_bytes(block[:8], "little")
    y = int.from_bytes(block[8:], "little")
    for i in range(_ROUNDS):
        y = ((_ror(y, 8) + x) & _M64) ^ keys[i]
        x = (_rol(x, 3) ^ y) & _M64
    return x.to_bytes(8, "little") + y.to_bytes(8, "little")


def x_ladon(
    timestamp: int,
    *,
    license_id: int = LICENSE_ID,
    aid: int = AID,
    salt: bytes | None = None,
) -> str:
    """Return a fresh 48-character ``x-ladon`` value for ``timestamp``."""
    nonce = salt if salt is not None else os.urandom(4)
    if len(nonce) != 4:
        raise ValueError("salt must be four bytes")
    key = hashlib.md5(nonce + str(aid).encode()).hexdigest().encode()
    keys = _schedule(key)

    plain = f"{timestamp}-{license_id}-{aid}".encode()
    pad = 16 - len(plain) % 16
    plain += bytes([pad]) * pad
    cipher = b"".join(
        _encrypt_block(plain[i : i + 16], keys) for i in range(0, len(plain), 16)
    )
    return base64.b64encode(nonce + cipher).decode()
