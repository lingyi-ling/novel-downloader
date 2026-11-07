#!/usr/bin/env python3
"""X-Ladon tests, pinned to a request captured from the client."""

from __future__ import annotations

import base64

from novel_downloader.plugins.sites.fanqienovel.ladon import x_ladon

#: (timestamp, salt) and the exact header value the client sent.
_TS = 1794002246
_SALT = bytes.fromhex("5f2777a7")
_GOLDEN = "Xyd3px3KFrhufCQTVpt0/VfrZGrsO3uYDq2U26T23Sv4S34R"


def test_x_ladon_reproduces_the_captured_header() -> None:
    assert x_ladon(_TS, salt=_SALT) == _GOLDEN


def test_x_ladon_is_deterministic_for_a_fixed_salt() -> None:
    assert x_ladon(_TS, salt=_SALT) == x_ladon(_TS, salt=_SALT)


def test_x_ladon_changes_with_the_salt() -> None:
    assert x_ladon(_TS, salt=b"\x00\x00\x00\x00") != x_ladon(_TS, salt=_SALT)


def test_x_ladon_changes_with_the_timestamp() -> None:
    assert x_ladon(_TS, salt=_SALT) != x_ladon(_TS + 1, salt=_SALT)


def test_x_ladon_frame_is_salt_plus_two_blocks() -> None:
    raw = base64.b64decode(x_ladon(_TS))

    assert len(raw) == 36
    assert raw[:4] != b"\x00" * 4  # random salt
