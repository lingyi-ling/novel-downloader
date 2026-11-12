#!/usr/bin/env python3
"""
Hardening tests for the in-process search-result cache and track serialisation.

The cache must never hand one search's tracks to another ``search_id``, and
must stay correct when many request threads put/get at the same time.
Serialisation must tolerate provider objects whose fields raise or carry
absurd values: one broken track must not fail the whole response.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.music import serialize, trackcache
from novel_downloader.apps.music.providers.base import Track
from novel_downloader.apps.music.serialize import PUBLIC_TRACK_FIELDS, track_dict
from novel_downloader.apps.music.trackcache import TrackCache


def _track(name: str, provider: str = "netease") -> Track:
    return Track(provider=provider, id=f"{provider}-{name}", name=name, artists="A")


# ---------------------------------------------------------------------------
# TrackCache
# ---------------------------------------------------------------------------


def test_put_never_overwrites_a_live_entry_on_id_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A colliding id must not swap one search's tracks into another's slot."""
    monkeypatch.setattr(trackcache.secrets, "token_hex", lambda size: "a" * 24)
    cache = TrackCache()
    first_tracks = [_track("first")]
    second_tracks = [_track("second")]

    first_id = cache.put(first_tracks)
    second_id = cache.put(second_tracks)

    assert first_id != second_id
    assert cache.get(first_id) == first_tracks
    assert cache.get(second_id) == second_tracks


def test_get_returns_a_copy_of_the_track_list() -> None:
    cache = TrackCache()
    tracks = [_track("A")]
    search_id = cache.put(tracks)

    fetched = cache.get(search_id)
    assert fetched is not None
    fetched.append(_track("B"))

    assert cache.get(search_id) == tracks


def test_expired_entry_is_gone_and_does_not_grow_the_cache() -> None:
    clock = {"now": 100.0}
    cache = TrackCache(ttl=10.0, clock=lambda: clock["now"])
    search_id = cache.put([_track("A")])

    clock["now"] += 11
    assert cache.get(search_id) is None
    assert len(cache) == 0
    # A stale id resolves to the submitted copy, matching the documented API.
    submitted = [Track(provider="netease", id="x", name="A")]
    assert cache.resolve(search_id, submitted) == submitted


def test_concurrent_put_get_never_mixes_searches() -> None:
    """Hammer the cache from many threads; each id must map to its own tracks."""
    cache = TrackCache(max_entries=16)
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            for round_no in range(40):
                name = f"t{index}-{round_no}"
                tracks = [_track(name)]
                search_id = cache.put(tracks)
                fetched = cache.get(search_id)
                # Another thread may evict our entry between put and get, but a
                # hit must always be *our* list, never someone else's.
                if fetched is not None and fetched != tracks:
                    raise AssertionError(f"{search_id} returned {fetched!r}")
            with cache._lock:  # noqa: SLF001 - invariant, not API behaviour
                if len(cache._entries) > cache._max_entries:  # noqa: SLF001
                    raise AssertionError("cache exceeded max_entries")
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert errors == []
    assert len(cache) <= 16


def test_lru_eviction_keeps_the_most_recently_used_entry() -> None:
    cache = TrackCache(max_entries=2)
    first = cache.put([_track("A")])
    second = cache.put([_track("B")])
    assert cache.get(first) is not None
    third = cache.put([_track("C")])

    assert cache.get(first) is not None
    assert cache.get(second) is None
    assert cache.get(third) is not None


def test_resolve_after_expiry_keeps_the_submitted_objects() -> None:
    clock = {"now": 0.0}
    cache = TrackCache(ttl=5.0, clock=lambda: clock["now"])
    original = [_track("A")]
    search_id = cache.put(original)

    clock["now"] = 6.0
    submitted = [Track(provider="netease", id=original[0].id, name="A", artists="A")]

    resolved = cache.resolve(search_id, submitted)

    assert resolved == submitted
    assert resolved[0] is submitted[0]


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def test_track_dict_survives_attributes_that_raise() -> None:
    class Broken:
        @property
        def name(self) -> str:
            raise RuntimeError("provider object is half-initialised")

        @property
        def duration_s(self) -> int:
            raise RuntimeError("no duration")

        @property
        def http_headers(self) -> Any:
            raise RuntimeError("no headers")

    payload = track_dict(Broken())

    assert tuple(payload) == PUBLIC_TRACK_FIELDS
    assert payload["name"] == ""
    assert payload["duration_s"] == 0
    assert payload["http_headers"] == {}
    assert payload["sources"] == []


def test_track_dict_bounds_hostile_integers() -> None:
    big = "9" * 40
    payload = track_dict(
        SimpleNamespace(duration_s=float("inf"), track_no=float("nan"))
    )
    assert payload["duration_s"] == 0
    assert payload["track_no"] == 0

    payload = track_dict(SimpleNamespace(duration_s=big, track_no=-7))
    assert payload["duration_s"] == serialize._INT_MAX
    assert payload["track_no"] == 0


def test_track_dict_accepts_generic_mappings_for_headers() -> None:
    class Headers(dict):
        pass

    track = _track("A")
    track.http_headers = Headers({"Referer": "https://example.com"})

    payload = track_dict(track)

    assert payload["http_headers"] == {"Referer": "https://example.com"}


def test_track_dict_handles_a_duck_typed_track() -> None:
    payload = track_dict(
        SimpleNamespace(
            provider="qq",
            id="1",
            name="A",
            artists="X",
            sources=("qq", "netease"),
            vip="YES",
        )
    )

    assert payload["provider"] == "qq"
    assert payload["sources"] == ["qq", "netease"]
    assert payload["vip"] is True


def test_track_dict_never_emits_private_fields() -> None:
    track = _track("A")
    track.extra = {"secret": object()}
    track.__dict__["_alternatives"] = [_track("B", "qq")]

    payload = track_dict(track)

    assert "extra" not in payload
    assert "_alternatives" not in payload


def test_sanitize_reason_still_bounds_and_redacts() -> None:
    reason = serialize.sanitize_reason(
        "boom https://cdn/x?token=SECRET\n\nmore   text" * 30, limit=50
    )

    assert len(reason) <= 50
    assert "SECRET" not in reason
    assert "\n" not in reason
