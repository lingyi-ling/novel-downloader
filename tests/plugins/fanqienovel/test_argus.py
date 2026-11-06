#!/usr/bin/env python3
"""X-Argus / SM3 tests, pinned to a request captured from the client."""

from __future__ import annotations

from novel_downloader.plugins.sites.fanqienovel.argus import sm3, x_argus

_QUERY = (
    "device_id=3067014224025180&iid=3067014224029276&device_type=SM-G9750"
    "&aid=1967&app_name=novelapp&channel=0&device_platform=android"
    "&os_version=0&version_code=6.3.9.32"
)
_STUB = "C169B3C490E1A8BC9306CA66704D2F79"
_GOLDEN = (
    "8oHAH26Sbp9WBxIQWA8MN9eox+aFcBalk7RNICCaGHil3RQ08294iTPr033RHVuDHBU92xgR1"
    "+ROzJN0wKigGzBCqeaqoK5fxuv5F6PPOrpNW1p7UjNC28AXMAdOJobi+f97AF0tk85p570fWf9"
    "6ccwVDGACPxBaf+rf1yxnD4DCHGMHXqLgHoTJ0C+fl1eWulPztsuWKyUZ7rSkeOEIa08BV5xZ"
    "aNv7ndZ+TaRCAkV33+1h8te9oVCz4cTjj8nvXJEOJnF8uBbOaYkxTNasf5mZ"
)


def test_sm3_matches_the_standard_vector() -> None:
    assert sm3(b"abc").hex() == (
        "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0"
    )


def test_x_argus_reproduces_the_captured_signature() -> None:
    """Byte-exact agreement with a request the official client sent."""
    value = x_argus(
        _QUERY,
        1794004376,
        device_id="3067014224025180",
        version_name="7.4.1.32",
        stub_hex=_STUB,
        field3=766596299,
    )

    assert value == _GOLDEN
    assert len(value) == 280


def test_x_argus_is_stable_for_a_fixed_nonce() -> None:
    a = x_argus(_QUERY, 1794004376, device_id="1", stub_hex=_STUB, field3=7)
    b = x_argus(_QUERY, 1794004376, device_id="1", stub_hex=_STUB, field3=7)

    assert a == b


def test_x_argus_changes_with_the_nonce() -> None:
    a = x_argus(_QUERY, 1794004376, device_id="1", stub_hex=_STUB, field3=7)
    b = x_argus(_QUERY, 1794004376, device_id="1", stub_hex=_STUB, field3=8)

    assert a != b
