#!/usr/bin/env python3
"""
Embedded-server music handlers.

The provider engine is replaced with a fake: these tests only cover the wire
shape, validation and job lifecycle, never the network.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.embedded import server as eserver


@dataclass
class FakeTrack:
    provider: str = ""
    id: str = ""
    name: str = ""
    artists: str = ""
    album: str = ""
    duration_s: int = 0
    cover_url: str = ""
    track_no: int = 0
    ext: str = ""
    vip: bool = False
    download_url: str = ""
    http_headers: dict[str, str] = field(default_factory=dict)
    path: str = ""
    lyric: str = ""
    sources: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


class FakeProvider:
    def search(self, keyword: str, *, limit: int = 20, offset: int = 0) -> Any:
        return [FakeTrack(id="1", name=keyword, artists="A")], 1


def _download_many(
    provider: Any, tracks: list[Any], options: Any, *, on_result: Any = None
) -> Any:
    for index, track in enumerate(tracks):
        if on_result is not None:
            on_result(
                index,
                track,
                SimpleNamespace(status="ok", reason="", path="/music/a.mp3", bytes_=3),
            )
    return len(tracks), 0, 0, [], []


def _components(tmp_path: Any) -> SimpleNamespace:
    return SimpleNamespace(
        paths=SimpleNamespace(default_download_dir=lambda: tmp_path),
        download_options=lambda out_dir, level: SimpleNamespace(
            out_dir=out_dir, level=level
        ),
        download_many=_download_many,
        download_mixed=_download_many,
        # The real ``export_m3u`` returns the playlist path, not a count.
        # Returning a path here keeps the handler honest: ``int(path)`` used
        # to raise ``ValueError`` (HTTP 500) against the real engine.
        export_m3u=lambda tracks, target: str(target),
        available_labels=lambda: [("netease", "国内 · 网易云音乐")],
        create=lambda key: FakeProvider(),
        platform_note=lambda key: "note",
        track_cls=FakeTrack,
        health_check=lambda key, timeout=5.0: {
            "ok": True,
            "elapsed_ms": 1,
            "error": "",
        },
    )


@pytest.fixture()
def music(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> SimpleNamespace:
    comps = _components(tmp_path)
    monkeypatch.setattr(eserver, "_music_components", lambda: comps)
    return comps


def test_platforms_lists_labels_and_notes(music: SimpleNamespace) -> None:
    payload = eserver._handle_music_platforms({}, {}, {})

    assert payload == {
        "platforms": [{"key": "netease", "label": "国内 · 网易云音乐", "note": "note"}]
    }


def test_search_maps_tracks(music: SimpleNamespace) -> None:
    payload = eserver._handle_music_search(
        {"platform": "netease", "keyword": "晴天"}, {}, {}
    )

    assert payload["total"] == 1
    assert payload["tracks"][0]["name"] == "晴天"
    assert payload["tracks"][0]["provider"] == ""


def test_search_drops_provider_private_data(music: SimpleNamespace) -> None:
    """``extra`` and ``_alternatives`` must not be serialised to the client."""
    from novel_downloader.apps.music.serialize import PUBLIC_TRACK_FIELDS

    original = FakeTrack(
        provider="netease",
        id="1",
        name="晴天",
        artists="A",
        sources=["netease", "qq"],
        extra={"raw": {"cookie": "MUSIC_U=secret"}, "songinfo": object()},
    )
    original.__dict__["_alternatives"] = [FakeTrack(provider="qq", id="9")]

    class StaticProvider:
        def search(self, keyword: str, *, limit: int = 20, offset: int = 0) -> Any:
            return [original], 1

    music.create = lambda key: StaticProvider()

    payload = eserver._handle_music_search(
        {"platform": "netease", "keyword": "晴天"}, {}, {}
    )

    public = payload["tracks"][0]
    assert "extra" not in public
    assert "_alternatives" not in public
    assert tuple(public) == PUBLIC_TRACK_FIELDS
    assert public["id"] == "1"
    assert public["sources"] == ["netease", "qq"]


def test_download_restores_tracks_from_search_id(music: SimpleNamespace) -> None:
    """Echoing a search row plus ``search_id`` hands the engine the original."""
    original = FakeTrack(
        provider="netease",
        id="1",
        name="晴天",
        extra={"raw": {"cookie": "MUSIC_U=secret"}},
    )

    class StaticProvider:
        def search(self, keyword: str, *, limit: int = 20, offset: int = 0) -> Any:
            return [original], 1

    music.create = lambda key: StaticProvider()
    captured: dict[str, Any] = {}

    def fake_download_mixed(
        create_provider: Any,
        tracks: list[Any],
        options: Any,
        *,
        on_result: Any = None,
    ) -> Any:
        captured["tracks"] = tracks
        return 0, 0, 0, [], []

    music.download_mixed = fake_download_mixed

    searched = eserver._handle_music_search(
        {"platform": "netease", "keyword": "晴天"}, {}, {}
    )
    search_id = searched["search_id"]
    assert search_id

    eserver._handle_music_download(
        {
            "platform": "netease",
            "tracks": searched["tracks"],  # exactly what the client echoes back
            "search_id": search_id,
        },
        {},
        {},
    )

    assert captured["tracks"] == [original]
    assert captured["tracks"][0] is original


def test_search_validation_mirrors_the_api(music: SimpleNamespace) -> None:
    with pytest.raises(eserver._ApiError) as missing:
        eserver._handle_music_search({"platform": "netease"}, {}, {})
    assert missing.value.status == 422
    assert missing.value.errors

    with pytest.raises(eserver._ApiError) as unknown:
        eserver._handle_music_search({"platform": "nope", "keyword": "x"}, {}, {})
    assert unknown.value.status == 400


def test_search_limit_is_bounded_like_the_rest_model(music: SimpleNamespace) -> None:
    """``limit`` is ``1..100``: 101 is a 422, not an unbounded provider page."""
    for bad in (0, -1, 101):
        with pytest.raises(eserver._ApiError) as err:
            eserver._handle_music_search(
                {"platform": "netease", "keyword": "x", "limit": bad}, {}, {}
            )
        assert err.value.status == 422, bad
        assert err.value.errors[0]["loc"] == ["body", "limit"]
        if bad > 100:
            assert err.value.errors[0]["type"] == "less_than_equal"

    payload = eserver._handle_music_search(
        {"platform": "netease", "keyword": "x", "limit": 100}, {}, {}
    )
    assert payload["total"] == 1


def test_search_reports_offset_and_paging_support(music: SimpleNamespace) -> None:
    """The response tells a client whether "load more" can work at all."""
    payload = eserver._handle_music_search(
        {"platform": "netease", "keyword": "x", "offset": 30}, {}, {}
    )
    assert payload["offset"] == 30
    # FakeProvider (like a musicdl-backed platform) has no ``supports_offset``.
    assert payload["supports_offset"] is False

    class PagedProvider:
        supports_offset = True

        def search(self, keyword: str, *, limit: int = 20, offset: int = 0) -> Any:
            return [], 0

    music.create = lambda key: PagedProvider()
    payload = eserver._handle_music_search(
        {"platform": "netease", "keyword": "x", "offset": 30}, {}, {}
    )
    assert payload["offset"] == 30
    assert payload["supports_offset"] is True


def test_aggregate_search_caps_the_per_platform_fanout(
    music: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novel_downloader.apps.music.aggregate import AggregateResult

    seen: list[int] = []

    def fake_search_all(*args: Any, **kwargs: Any) -> AggregateResult:
        seen.append(kwargs["per_platform_limit"])
        return AggregateResult(tracks=[], platforms=[], total=0)

    monkeypatch.setattr(
        "novel_downloader.apps.music.aggregate.search_all", fake_search_all
    )

    for limit, expected in ((1, 8), (8, 8), (20, 15), (100, 15)):
        payload = eserver._handle_music_search(
            {"platform": "all", "keyword": "x", "limit": limit}, {}, {}
        )
        assert seen[-1] == expected, (limit, expected, seen)
        assert payload["offset"] == 0
        assert payload["supports_offset"] is False


def test_download_rejects_a_malformed_search_id(music: SimpleNamespace) -> None:
    """Bad shape is a 422 before the cache lookup; blank still means absent."""
    for handler in (eserver._handle_music_download, eserver._handle_music_jobs_post):
        for bad in ("x" * 65, "bad/../id", "has space"):
            with pytest.raises(eserver._ApiError) as err:
                handler(
                    {
                        "platform": "netease",
                        "tracks": [{"id": "1"}],
                        "search_id": bad,
                    },
                    {},
                    {},
                )
            assert err.value.status == 422, bad
            assert err.value.errors[0]["loc"] == ["body", "search_id"]

    # Blank keeps the historical "no id" meaning.
    payload = eserver._handle_music_download(
        {"platform": "netease", "tracks": [{"id": "1"}], "search_id": "   "}, {}, {}
    )
    assert payload["ok"] == 1


def test_job_id_shape_is_validated() -> None:
    for bad in ("z" * 65, "a" * 32 + "\n"):
        with pytest.raises(eserver._ApiError) as err:
            eserver._handle_music_job_status({}, {"job_id": bad}, {})
        assert err.value.status == 422, repr(bad)
        assert err.value.errors[0]["loc"] == ["path", "job_id"]
        assert err.value.errors[0]["type"] == "string_pattern_mismatch"

    # Unknown but well-formed stays the shared 404.
    with pytest.raises(eserver._ApiError) as missing:
        eserver._handle_music_job_status({}, {"job_id": "a" * 32}, {})
    assert missing.value.status == 404


def test_health_reports_per_platform(music: SimpleNamespace) -> None:
    payload = eserver._handle_music_health({}, {}, {})

    assert payload == [
        {"platform": "netease", "ok": True, "elapsed_ms": 1, "error": ""}
    ]


def test_download_returns_counts(music: SimpleNamespace) -> None:
    payload = eserver._handle_music_download(
        {"platform": "netease", "tracks": [{"id": "1", "name": "A"}]}, {}, {}
    )

    assert payload["ok"] == 1
    assert payload["fail"] == 0
    assert payload["results"][0]["status"] == "ok"


@pytest.mark.parametrize(
    "endpoint", ["_handle_music_download", "_handle_music_jobs_post"]
)
def test_download_rejects_out_dir_outside_allowed_roots(
    endpoint: str, music: SimpleNamespace, tmp_path: Any
) -> None:
    """An absolute ``out_dir`` outside the music roots is a 400, not echoed."""

    def must_not_download(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("engine must not run for a rejected out_dir")

    music.download_mixed = must_not_download
    outside = tmp_path.parent / "elsewhere"

    handler = getattr(eserver, endpoint)
    with pytest.raises(eserver._ApiError) as err:
        handler(
            {
                "platform": "netease",
                "tracks": [{"id": "1", "name": "A"}],
                "out_dir": str(outside),
            },
            {},
            {},
        )

    assert err.value.status == 400
    assert "out_dir" in str(err.value.detail)
    assert "elsewhere" not in str(err.value.detail)
    assert str(tmp_path) not in str(err.value.detail)


@pytest.mark.parametrize(
    "endpoint", ["_handle_music_download", "_handle_music_jobs_post"]
)
def test_download_rejects_dotdot_out_dir_escape(
    endpoint: str, music: SimpleNamespace
) -> None:
    handler = getattr(eserver, endpoint)
    with pytest.raises(eserver._ApiError) as err:
        handler(
            {
                "platform": "netease",
                "tracks": [{"id": "1", "name": "A"}],
                "out_dir": "../../escape",
            },
            {},
            {},
        )

    assert err.value.status == 400
    assert "escape" not in str(err.value.detail)


def test_download_accepts_a_subdirectory_inside_the_allowed_root(
    music: SimpleNamespace, tmp_path: Any
) -> None:
    calls: dict[str, Any] = {}

    def fake_download(
        provider: Any, tracks: list[Any], options: Any, *, on_result: Any
    ) -> Any:
        calls["out_dir"] = options.out_dir
        return 0, 0, 0, [], []

    music.download_mixed = fake_download
    album_dir = tmp_path / "albums" / "2026"

    eserver._handle_music_download(
        {
            "platform": "netease",
            "tracks": [{"id": "1", "name": "A"}],
            "out_dir": str(album_dir),
        },
        {},
        {},
    )

    assert calls["out_dir"] == str(album_dir.resolve())


def test_download_defaults_to_the_embedded_music_dir(
    monkeypatch: pytest.MonkeyPatch, music: SimpleNamespace, tmp_path: Any
) -> None:
    default = tmp_path / "music"
    monkeypatch.setattr(eserver, "_music_export_dir", lambda: default)
    calls: dict[str, Any] = {}

    def fake_download(
        provider: Any, tracks: list[Any], options: Any, *, on_result: Any
    ) -> Any:
        calls["out_dir"] = options.out_dir
        return 0, 0, 0, [], []

    music.download_mixed = fake_download

    eserver._handle_music_download(
        {"platform": "netease", "tracks": [{"id": "1", "name": "A"}]}, {}, {}
    )

    assert calls["out_dir"] == str(default.resolve())
    assert default.is_dir()


def test_result_reason_drops_url_query_and_fragment() -> None:
    item = eserver._music_result_item(
        SimpleNamespace(
            status="fail",
            reason="HTTP 403 for https://cdn.example.com/a.mp3?token=SECRET#frag",
            path="",
            bytes_=0,
        )
    )

    assert item["reason"] == "HTTP 403 for https://cdn.example.com/a.mp3"
    assert "SECRET" not in str(item)
    assert "frag" not in str(item)


def test_export_skips_tracks_without_a_path(
    music: SimpleNamespace, tmp_path: Any
) -> None:
    payload = eserver._handle_music_export(
        {
            "platform": "netease",
            "tracks": [
                {"id": "1", "name": "A"},
                {"id": "2", "name": "B", "path": "/m/b.mp3"},
            ],
            "out_path": str(tmp_path / "p.m3u8"),
        },
        {},
        {},
    )

    assert payload == {
        "path": str(tmp_path / "p.m3u8"),
        "tracks_written": 1,
        "skipped": 1,
        "written": 1,
    }


def test_export_dedupes_repeated_file_locations(
    music: SimpleNamespace, tmp_path: Any
) -> None:
    """The same file under two spellings is one entry, counted in ``skipped``."""
    captured: list[Any] = []

    def fake_export(tracks: Any, target: Any) -> str:
        captured.extend(tracks)
        return str(target)

    music.export_m3u = fake_export
    payload = eserver._handle_music_export(
        {
            "platform": "netease",
            "tracks": [
                {"id": "1", "name": "A", "path": "songs/a.mp3"},
                {"id": "2", "name": "A copy", "path": "./songs/a.mp3"},
                {"id": "3", "name": "B", "path": "songs/b.mp3"},
                {"id": "4", "name": "No path"},
            ],
            "out_path": str(tmp_path / "p.m3u8"),
        },
        {},
        {},
    )

    assert len(captured) == 2
    assert [track.id for track in captured] == ["1", "3"]
    assert payload["tracks_written"] == 2
    assert payload["written"] == 2
    assert payload["skipped"] == 2


def test_download_rejects_an_empty_track_list(music: SimpleNamespace) -> None:
    with pytest.raises(eserver._ApiError) as err:
        eserver._handle_music_download({"platform": "netease", "tracks": []}, {}, {})
    assert err.value.status == 422


def test_job_reaches_done_with_results(music: SimpleNamespace) -> None:
    created = eserver._handle_music_jobs_post(
        {"platform": "netease", "tracks": [{"id": "1", "name": "A"}]}, {}, {}
    )
    job_id = created["job_id"]

    deadline = time.time() + 5
    while True:
        job = eserver._handle_music_job_status({}, {"job_id": job_id}, {})
        if job["status"] in ("done", "failed") or time.time() > deadline:
            break
        time.sleep(0.02)

    assert job["status"] == "done"
    assert job["progress"] == {"done": 1, "total": 1}
    assert job["results"][0]["status"] == "ok"
    assert job["error"] is None


def test_unknown_job_is_404() -> None:
    with pytest.raises(eserver._ApiError) as err:
        eserver._handle_music_job_status({}, {"job_id": "missing"}, {})
    assert err.value.status == 404


def test_missing_extras_answer_503(monkeypatch: pytest.MonkeyPatch) -> None:
    def _unavailable() -> Any:
        raise eserver._ApiError(
            503, eserver._MSG_MUSIC_UNAVAILABLE.format(error="ModuleNotFoundError")
        )

    monkeypatch.setattr(eserver, "_music_components", _unavailable)

    with pytest.raises(eserver._ApiError) as err:
        eserver._handle_music_platforms({}, {}, {})
    assert err.value.status == 503


def test_results_carry_a_loopback_download_url(music: SimpleNamespace) -> None:
    """The Kotlin client hands this URL to DownloadManager."""
    item = eserver._music_result_item(
        SimpleNamespace(status="ok", reason="", path="/tmp/music/a.mp3", bytes_=3)
    )

    assert item["download_url"] == "/downloads/music/a.mp3"


def test_results_without_an_audio_extension_have_no_url() -> None:
    item = eserver._music_result_item(
        SimpleNamespace(status="ok", reason="", path="/tmp/music/notes.txt", bytes_=3)
    )

    assert item["download_url"] == ""


def test_music_serve_path_cannot_escape_the_music_folder() -> None:
    """`music/../../secret.mp3` must resolve inside *its own* basename only."""
    from pathlib import Path

    decoded = "music/../../secret.mp3"
    _prefix, _, rest = decoded.partition("/")
    safe = Path(rest).name
    assert safe == "secret.mp3"


# ---------------------------------------------------------------------------
# Export path confinement (embedded)
# ---------------------------------------------------------------------------


def test_export_rejects_a_target_outside_the_music_dir(
    music: SimpleNamespace, tmp_path: Any
) -> None:
    outside = tmp_path.parent / "escape.m3u8"

    with pytest.raises(eserver._ApiError) as err:
        eserver._handle_music_export(
            {
                "platform": "netease",
                "tracks": [{"id": "1", "path": "/m/a.mp3"}],
                "out_path": str(outside),
            },
            {},
            {},
        )

    assert err.value.status == 400
    # The rejected absolute path must not be echoed to the client.
    assert "escape.m3u8" not in err.value.detail
    assert not outside.exists()


def test_export_rejects_dotdot_escape(music: SimpleNamespace, tmp_path: Any) -> None:
    with pytest.raises(eserver._ApiError) as err:
        eserver._handle_music_export(
            {
                "platform": "netease",
                "tracks": [{"id": "1", "path": "/m/a.mp3"}],
                "out_path": "../../escape.m3u8",
            },
            {},
            {},
        )

    assert err.value.status == 400
    assert "escape.m3u8" not in err.value.detail


@pytest.mark.parametrize("name", ["escape.txt", "escape.json"])
def test_export_rejects_non_playlist_suffix(
    name: str, music: SimpleNamespace, tmp_path: Any
) -> None:
    with pytest.raises(eserver._ApiError) as err:
        eserver._handle_music_export(
            {
                "platform": "netease",
                "tracks": [{"id": "1", "path": "/m/a.mp3"}],
                "out_path": str(tmp_path / name),
            },
            {},
            {},
        )

    assert err.value.status == 400
    assert name not in err.value.detail


def test_export_accepts_a_directory_inside_the_music_dir(
    music: SimpleNamespace, tmp_path: Any
) -> None:
    playlists = tmp_path / "playlists"

    payload = eserver._handle_music_export(
        {
            "platform": "netease",
            "tracks": [{"id": "1", "path": "/m/a.mp3"}],
            "out_path": str(playlists),
        },
        {},
        {},
    )

    from pathlib import Path

    written = Path(payload["path"])
    assert written.parent == playlists.resolve()
    assert written.name.startswith("playlist-")
    assert written.suffix == ".m3u8"


# ---------------------------------------------------------------------------
# Job error sanitisation and concurrency cap (embedded)
# ---------------------------------------------------------------------------


def _wait_for_embedded_job(job_id: str, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while True:
        job = eserver._handle_music_job_status({}, {"job_id": job_id}, {})
        if job["status"] in ("done", "failed") or time.time() > deadline:
            return job
        time.sleep(0.01)


def test_job_error_does_not_leak_exception_text(
    music: SimpleNamespace, tmp_path: Any
) -> None:
    secret = "signed https://cdn.example.com/a.mp3?token=SECRET-VALUE"

    def exploding(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(secret)

    music.download_mixed = exploding

    created = eserver._handle_music_jobs_post(
        {"platform": "netease", "tracks": [{"id": "1"}]}, {}, {}
    )
    job = _wait_for_embedded_job(created["job_id"])

    assert job["status"] == "failed"
    assert "RuntimeError" in job["error"]
    assert "SECRET-VALUE" not in job["error"]
    assert "cdn.example.com" not in job["error"]


def test_job_beyond_active_limit_is_refused(
    music: SimpleNamespace,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = threading.Event()

    def blocking(*args: Any, **kwargs: Any) -> Any:
        gate.wait(5.0)
        return 0, 0, 0, [], []

    music.download_mixed = blocking
    monkeypatch.setattr(eserver, "_MAX_ACTIVE_MUSIC_JOBS", 1)

    try:
        created = eserver._handle_music_jobs_post(
            {"platform": "netease", "tracks": [{"id": "1"}]}, {}, {}
        )
        with pytest.raises(eserver._ApiError) as err:
            eserver._handle_music_jobs_post(
                {"platform": "netease", "tracks": [{"id": "2"}]}, {}, {}
            )
        assert err.value.status == 429
        # Same Retry-After as REST: a load signal for the client's backoff.
        assert err.value.headers == {"Retry-After": "5"}
        # The running job's record was not evicted.
        assert eserver._handle_music_job_status({}, {"job_id": created["job_id"]}, {})[
            "status"
        ] in ("queued", "running")
    finally:
        gate.set()
    _wait_for_embedded_job(created["job_id"])


def test_prune_keeps_active_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eserver._MUSIC_JOBS.clear()
    monkeypatch.setattr(eserver, "_MAX_MUSIC_JOBS", 1)
    try:
        with eserver._MUSIC_JOBS_LOCK:
            eserver._MUSIC_JOBS["running"] = {
                "job_id": "running",
                "status": "running",
                "progress": {"done": 0, "total": 1},
                "results": [],
                "error": None,
                "created_at": time.time(),
                "finished_at": None,
            }
            eserver._prune_music_jobs_locked()
            assert "running" in eserver._MUSIC_JOBS
    finally:
        eserver._MUSIC_JOBS.clear()


# ---------------------------------------------------------------------------
# Field-level response contract (mirrors tests/apps/test_api_e2e.py)
#
# The embedded engine hand-mirrors the REST contract. These tests pin the
# exact key set and JSON type of every music response field, and compare the
# two implementations directly where both can be called offline. If a field
# is added/renamed on one side only, these fail before a client does.
# ---------------------------------------------------------------------------


#: Exact key set of a per-track download/job result, on both transports.
_RESULT_KEYS = frozenset({"status", "reason", "path", "download_url", "bytes"})

#: Exact key set of ``POST /api/v1/music/export``, on both transports.
_EXPORT_KEYS = frozenset({"path", "tracks_written", "skipped", "written"})

#: Exact key set of one ``GET /api/v1/music/health`` entry.
_HEALTH_KEYS = frozenset({"platform", "ok", "elapsed_ms", "error"})


def test_result_item_field_set_and_types() -> None:
    item = eserver._music_result_item(
        SimpleNamespace(
            status="fail",
            reason="boom",
            path="/tmp/music/a.mp3",
            bytes_=7,
        )
    )

    assert set(item) == _RESULT_KEYS
    assert isinstance(item["status"], str)
    assert isinstance(item["reason"], str)
    assert isinstance(item["path"], str)
    assert isinstance(item["download_url"], str)
    assert type(item["bytes"]) is int


def test_result_item_shape_matches_the_rest_engine() -> None:
    """Both engines serialise a result with exactly the same keys."""
    from novel_downloader.apps.api import routes as api_routes

    result = SimpleNamespace(status="ok", reason="r", path="/m/a.mp3", bytes_=3)
    embedded = eserver._music_result_item(result)
    rest = api_routes._music_result_item(result)

    assert tuple(embedded) == tuple(rest)
    assert set(embedded) == _RESULT_KEYS


def test_result_reason_is_bounded_and_single_line() -> None:
    """Same ceiling as search/health errors: 200 chars, after query stripping."""
    reason = (
        "provider failed\nat https://cdn.example/a.mp3?token=SECRET#frag " + "x" * 500
    )
    item = eserver._music_result_item(
        SimpleNamespace(status="fail", reason=reason, path="", bytes_=0)
    )

    assert len(item["reason"]) <= 200
    assert "\n" not in item["reason"]
    assert item["reason"].startswith("provider failed at https://cdn.example/a.mp3")
    assert "SECRET" not in item["reason"]
    assert "#frag" not in item["reason"]


def test_export_response_field_set(music: SimpleNamespace, tmp_path: Any) -> None:
    payload = eserver._handle_music_export(
        {
            "platform": "netease",
            "tracks": [{"id": "1", "path": "/m/a.mp3"}],
            "out_path": str(tmp_path / "p.m3u8"),
        },
        {},
        {},
    )

    assert set(payload) == _EXPORT_KEYS
    assert type(payload["path"]) is str
    assert type(payload["tracks_written"]) is int
    assert type(payload["written"]) is int
    assert type(payload["skipped"]) is int
    assert payload["tracks_written"] == payload["written"] == 1
    assert payload["skipped"] == 0


def test_export_rejects_an_empty_track_list(music: SimpleNamespace) -> None:
    """``MusicExportRequest.tracks`` is ``min_length=1`` on both sides."""
    with pytest.raises(eserver._ApiError) as err:
        eserver._handle_music_export({"platform": "netease", "tracks": []}, {}, {})
    assert err.value.status == 422
    assert err.value.errors


def test_export_rejects_too_many_tracks(music: SimpleNamespace) -> None:
    tracks = [{"id": str(index), "path": "/m/a.mp3"} for index in range(201)]
    with pytest.raises(eserver._ApiError) as err:
        eserver._handle_music_export({"platform": "netease", "tracks": tracks}, {}, {})
    assert err.value.status == 422
    assert err.value.errors


@pytest.mark.parametrize(
    "handler,body",
    [
        (
            eserver._handle_music_search,
            {"platform": None, "keyword": "x"},
        ),
        (eserver._handle_music_search, {"platform": "", "keyword": "x"}),
        (eserver._handle_music_search, {"platform": 3, "keyword": "x"}),
        (
            eserver._handle_music_download,
            {"platform": None, "tracks": [{"id": "1"}]},
        ),
        (
            eserver._handle_music_download,
            {"platform": "", "tracks": [{"id": "1"}]},
        ),
        (
            eserver._handle_music_jobs_post,
            {"platform": 3, "tracks": [{"id": "1"}]},
        ),
        (
            eserver._handle_music_export,
            {"platform": None, "tracks": [{"id": "1", "path": "/m/a.mp3"}]},
        ),
    ],
)
def test_platform_validation_matches_the_request_model(
    music: SimpleNamespace, handler: Any, body: dict[str, Any]
) -> None:
    """``platform`` is ``str`` + ``min_length=1`` (explicit null/invalid → 422)."""
    with pytest.raises(eserver._ApiError) as err:
        handler(body, {}, {})
    assert err.value.status == 422
    assert any(error["loc"][:2] == ["body", "platform"] for error in err.value.errors)


def test_platform_value_is_not_stripped(music: SimpleNamespace) -> None:
    """REST does not strip, so ``" netease "`` is unknown on both transports."""
    with pytest.raises(eserver._ApiError) as err:
        eserver._handle_music_search({"platform": " netease ", "keyword": "x"}, {}, {})
    assert err.value.status == 400


def test_health_rejects_invalid_timeout(music: SimpleNamespace) -> None:
    """REST's ``Query(gt=0, le=60, allow_inf_nan=False)`` is a 422."""
    expected = {
        "0": "greater_than",
        "-1": "greater_than",
        "61": "less_than_equal",
        "abc": "float_parsing",
        "nan": "finite_number",
        "inf": "finite_number",
        "-inf": "finite_number",
    }
    for raw, error_type in expected.items():
        with pytest.raises(eserver._ApiError) as err:
            eserver._handle_music_health({}, {}, {"timeout": [raw]})
        assert err.value.status == 422, raw
        assert err.value.errors
        assert err.value.errors[0]["loc"] == ["query", "timeout"]
        assert err.value.errors[0]["type"] == error_type


def test_health_passes_a_valid_timeout_through(
    music: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[float] = []

    def fake_health(key: str, timeout: float = 5.0) -> dict[str, Any]:
        calls.append(timeout)
        return {"ok": True, "elapsed_ms": 1, "error": ""}

    music.health_check = fake_health

    payload = eserver._handle_music_health({}, {}, {"timeout": ["2.5"]})

    assert payload == [
        {"platform": "netease", "ok": True, "elapsed_ms": 1, "error": ""}
    ]
    assert calls == [2.5]


def test_health_response_field_set(music: SimpleNamespace) -> None:
    payload = eserver._handle_music_health({}, {}, {})

    assert isinstance(payload, list) and payload
    for item in payload:
        assert set(item) == _HEALTH_KEYS
        assert isinstance(item["platform"], str)
        assert type(item["ok"]) is bool
        assert type(item["elapsed_ms"]) is int
        assert isinstance(item["error"], str)


def test_search_and_job_responses_have_the_rest_key_sets(
    music: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Single-platform search and a finished job expose the REST field sets."""
    from novel_downloader.apps.music.aggregate import ALL_PLATFORM_KEY
    from novel_downloader.apps.music.serialize import PUBLIC_TRACK_FIELDS

    searched = eserver._handle_music_search(
        {"platform": "netease", "keyword": "晴天"}, {}, {}
    )
    # ``offset``/``supports_offset`` make the paging contract explicit: the
    # FakeProvider has no opt-in attribute, so it reports no paging (same
    # conservative value a musicdl-backed platform reports).
    assert set(searched) == {
        "total",
        "tracks",
        "search_id",
        "platform",
        "offset",
        "supports_offset",
    }
    assert type(searched["total"]) is int
    assert isinstance(searched["search_id"], str) and searched["search_id"]
    assert searched["platform"] == "netease"
    assert type(searched["offset"]) is int and searched["offset"] == 0
    assert searched["supports_offset"] is False
    assert tuple(searched["tracks"][0]) == PUBLIC_TRACK_FIELDS
    assert isinstance(searched["tracks"][0]["sources"], list)

    created = eserver._handle_music_jobs_post(
        {"platform": "netease", "tracks": [{"id": "1"}]}, {}, {}
    )
    assert set(created) == {"job_id"}
    assert isinstance(created["job_id"], str)

    deadline = time.time() + 5
    while True:
        job = eserver._handle_music_job_status({}, {"job_id": created["job_id"]}, {})
        if job["status"] in ("done", "failed") or time.time() > deadline:
            break
        time.sleep(0.01)

    assert set(job) == {
        "job_id",
        "status",
        "progress",
        "results",
        "error",
        "created_at",
        "finished_at",
    }
    assert set(job["progress"]) == {"done", "total"}
    assert all(type(value) is int for value in job["progress"].values())
    assert job["error"] is None or isinstance(job["error"], str)
    assert type(job["created_at"]) is float
    assert job["finished_at"] is None or type(job["finished_at"]) is float
    for item in job["results"]:
        assert set(item) == _RESULT_KEYS
    assert ALL_PLATFORM_KEY == "all"


def test_job_creation_is_202_on_the_http_layer() -> None:
    """``POST /music/jobs`` answers 202 on the embedded server too (not 200)."""
    assert eserver._SUCCESS_STATUS[eserver._handle_music_jobs_post] == 202
    assert eserver._SUCCESS_STATUS.get(eserver._handle_music_search, 200) == 200
