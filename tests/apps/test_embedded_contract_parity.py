#!/usr/bin/env python3
"""
tests.apps.test_embedded_contract_parity
----------------------------------------

Contract tests that drive the embedded engine's validation against the REST
request models.

For every malformed payload the table below asserts that
``apps.api.schemas`` rejects it *and* the corresponding embedded handler
answers the same ``422`` with the same field location.  This is the guard that
keeps the two transports from drifting when a schema gains a bound or a
normalisation rule (the embedded module is stdlib-only, so it re-implements
the checks rather than importing Pydantic).

The remaining tests pin behaviour that a model alone cannot express: music job
timeouts/partial results, sanitized engine and platform failures, and the
path-parameter validation of ``DELETE /watchlist/{site}/{book_id}``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from novel_downloader.apps.api import schemas as rest
from novel_downloader.apps.embedded import server as eserver

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


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
    supports_offset = False

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
        export_m3u=lambda tracks, target: str(target),
        available_labels=lambda: [("netease", "网易云音乐")],
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


# ---------------------------------------------------------------------------
# model contract: the same payload must fail on both transports
# ---------------------------------------------------------------------------

#: ``(model, embedded handler, payload)`` pairs.  Each payload must fail model
#: validation for exactly one reason, so the first error's location can be
#: compared with the embedded engine's.
_PARITY_CASES: list[tuple[type[BaseModel], eserver.Handler, dict[str, Any]]] = [
    # POST /search
    (rest.SearchRequest, eserver._handle_search, {"keyword": ""}),
    (rest.SearchRequest, eserver._handle_search, {"keyword": "   "}),
    (rest.SearchRequest, eserver._handle_search, {"keyword": "x" * 201}),
    (rest.SearchRequest, eserver._handle_search, {"keyword": 5}),
    (
        rest.SearchRequest,
        eserver._handle_search,
        {"keyword": "x", "sites": ["bad site"]},
    ),
    (
        rest.SearchRequest,
        eserver._handle_search,
        {"keyword": "x", "sites": ["x"] * 101},
    ),
    (
        rest.SearchRequest,
        eserver._handle_search,
        {"keyword": "x", "sites": ["ok", 5]},
    ),
    (
        rest.SearchRequest,
        eserver._handle_search,
        {"keyword": "x", "per_site_limit": 101},
    ),
    (rest.SearchRequest, eserver._handle_search, {"keyword": "x", "limit": 501}),
    (rest.SearchRequest, eserver._handle_search, {"keyword": "x", "timeout": 121}),
    (
        rest.SearchRequest,
        eserver._handle_search,
        {"keyword": "x", "timeout": float("nan")},
    ),
    (
        rest.SearchRequest,
        eserver._handle_search,
        {"keyword": "x", "timeout": float("inf")},
    ),
    (rest.SearchRequest, eserver._handle_search, {"keyword": "x", "sort": "bogus"}),
    # local search filters: same bounds/format as ``SearchRequest``
    (rest.SearchRequest, eserver._handle_search, {"keyword": "x", "min_words": -1}),
    (
        rest.SearchRequest,
        eserver._handle_search,
        {"keyword": "x", "min_words": 1_000_000_001},
    ),
    (rest.SearchRequest, eserver._handle_search, {"keyword": "x", "max_words": -1}),
    (
        rest.SearchRequest,
        eserver._handle_search,
        {"keyword": "x", "max_words": 1_000_000_001},
    ),
    (
        rest.SearchRequest,
        eserver._handle_search,
        {"keyword": "x", "updated_after": "2024/01/01"},
    ),
    (
        rest.SearchRequest,
        eserver._handle_search,
        {"keyword": "x", "updated_after": "20240101"},
    ),
    (
        rest.SearchRequest,
        eserver._handle_search,
        {"keyword": "x", "updated_after": "2024-13-01"},
    ),
    (
        rest.SearchRequest,
        eserver._handle_search,
        {"keyword": "x", "updated_after": "2024-02-30"},
    ),
    (
        rest.SearchRequest,
        eserver._handle_search,
        {"keyword": "x", "updated_after": ""},
    ),
    (
        rest.SearchRequest,
        eserver._handle_search,
        {"keyword": "x", "updated_after": "soon"},
    ),
    # POST /browse
    (rest.BrowseRequest, eserver._handle_browse, {"site": ""}),
    (rest.BrowseRequest, eserver._handle_browse, {"site": "bad site"}),
    (rest.BrowseRequest, eserver._handle_browse, {"site": 5}),
    (
        rest.BrowseRequest,
        eserver._handle_browse,
        {"site": "qidian", "category_id": "x" * 129},
    ),
    (rest.BrowseRequest, eserver._handle_browse, {"site": "qidian", "limit": 101}),
    (rest.BrowseRequest, eserver._handle_browse, {"site": "qidian", "gender": "x"}),
    # POST /tasks
    (rest.TaskCreateRequest, eserver._handle_tasks_post, {}),
    (rest.TaskCreateRequest, eserver._handle_tasks_post, {"site": "", "book_id": "1"}),
    (
        rest.TaskCreateRequest,
        eserver._handle_tasks_post,
        {"site": "bad site", "book_id": "1"},
    ),
    (
        rest.TaskCreateRequest,
        eserver._handle_tasks_post,
        {"site": "qidian", "book_id": "a/b"},
    ),
    (
        rest.TaskCreateRequest,
        eserver._handle_tasks_post,
        {"site": "qidian", "book_id": "1", "title": "x" * 513},
    ),
    (
        rest.TaskCreateRequest,
        eserver._handle_tasks_post,
        {"site": "qidian", "book_id": "1", "title": "bad\x01"},
    ),
    (
        rest.TaskCreateRequest,
        eserver._handle_tasks_post,
        {"site": "qidian", "book_id": "1", "title": 5},
    ),
    # POST /watchlist
    (
        rest.WatchlistAddRequest,
        eserver._handle_watchlist_post,
        {"site": "bad site", "book_id": "1"},
    ),
    (
        rest.WatchlistAddRequest,
        eserver._handle_watchlist_post,
        {"site": "s", "book_id": "."},
    ),
    # POST /login/requests/{req_id}
    (rest.LoginSubmitRequest, eserver._handle_login_submit, {"values": None}),
    (
        rest.LoginSubmitRequest,
        eserver._handle_login_submit,
        {"values": {"a": 1}},
    ),
    (
        rest.LoginSubmitRequest,
        eserver._handle_login_submit,
        {"values": {"a": "x\r\nInjected: 1"}},
    ),
    (
        rest.LoginSubmitRequest,
        eserver._handle_login_submit,
        {"values": {"": "x"}},
    ),
    # POST /music/search
    (
        rest.MusicSearchRequest,
        eserver._handle_music_search,
        {"platform": "netease", "keyword": ""},
    ),
    (
        rest.MusicSearchRequest,
        eserver._handle_music_search,
        {"platform": "netease", "keyword": "x" * 201},
    ),
    (
        rest.MusicSearchRequest,
        eserver._handle_music_search,
        {"platform": "", "keyword": "x"},
    ),
    (
        rest.MusicSearchRequest,
        eserver._handle_music_search,
        {"platform": "a" * 65, "keyword": "x"},
    ),
    (
        rest.MusicSearchRequest,
        eserver._handle_music_search,
        {"platform": 5, "keyword": "x"},
    ),
    (
        rest.MusicSearchRequest,
        eserver._handle_music_search,
        {"platform": "netease", "keyword": "x", "limit": 101},
    ),
    (
        rest.MusicSearchRequest,
        eserver._handle_music_search,
        {"platform": "netease", "keyword": "x", "offset": 10_001},
    ),
    # POST /music/download
    (
        rest.MusicDownloadRequest,
        eserver._handle_music_download,
        {"tracks": []},
    ),
    (
        rest.MusicDownloadRequest,
        eserver._handle_music_download,
        {"tracks": [{"id": 5}]},
    ),
    (
        rest.MusicDownloadRequest,
        eserver._handle_music_download,
        {"tracks": [{"id": "1"}], "level": 5},
    ),
    (
        rest.MusicDownloadRequest,
        eserver._handle_music_download,
        {"tracks": [{"id": "1"}], "out_dir": 5},
    ),
    (
        rest.MusicDownloadRequest,
        eserver._handle_music_download,
        {"tracks": [{"id": "1"}], "search_id": "bad/id"},
    ),
    (
        rest.MusicDownloadRequest,
        eserver._handle_music_download,
        {"tracks": [{"id": "1", "download_url": "file:///etc/passwd"}]},
    ),
    (
        rest.MusicDownloadRequest,
        eserver._handle_music_download,
        {"tracks": [{"id": "1", "sources": "netease"}]},
    ),
    (
        rest.MusicDownloadRequest,
        eserver._handle_music_download,
        {"tracks": [{"id": "1", "http_headers": {"Bad Header": "x"}}]},
    ),
    # POST /music/export
    (
        rest.MusicExportRequest,
        eserver._handle_music_export,
        {"tracks": []},
    ),
    (
        rest.MusicExportRequest,
        eserver._handle_music_export,
        {"tracks": [{"id": "1"}], "out_path": 5},
    ),
    (
        rest.MusicExportRequest,
        eserver._handle_music_export,
        {"tracks": [{"id": "1", "path": "x" * 1025}]},
    ),
]


def _strip_source(loc: Any) -> list[Any]:
    return [part for part in loc if part not in ("body", "query", "path", "header")]


@pytest.mark.parametrize(
    "model,handler,payload",
    _PARITY_CASES,
    ids=[f"{case[0].__name__}-{index}" for index, case in enumerate(_PARITY_CASES)],
)
def test_validation_parity_with_rest_models(
    music: SimpleNamespace,
    model: type[BaseModel],
    handler: eserver.Handler,
    payload: dict[str, Any],
) -> None:
    """Both transports reject the payload with the same field location."""
    with pytest.raises(ValidationError) as rest_error:
        model.model_validate(payload)

    with pytest.raises(eserver._ApiError) as embedded_error:
        handler(payload, {}, {})

    assert embedded_error.value.status == 422, payload
    assert embedded_error.value.errors, payload
    rest_loc = _strip_source(rest_error.value.errors()[0]["loc"])
    embedded_loc = _strip_source(embedded_error.value.errors[0]["loc"])
    assert embedded_loc == rest_loc, (payload, rest_loc, embedded_loc)


def test_error_type_matches_the_rest_model(
    music: SimpleNamespace,
) -> None:
    """The machine-readable ``type`` is the same for representative cases."""
    cases = [
        (
            rest.SearchRequest,
            eserver._handle_search,
            {"keyword": "x" * 201},
            "value_error",
        ),
        (
            rest.MusicSearchRequest,
            eserver._handle_music_search,
            {"platform": "netease", "keyword": "x", "offset": 10_001},
            "less_than_equal",
        ),
        (
            rest.SearchRequest,
            eserver._handle_search,
            {"keyword": "x", "timeout": float("inf")},
            "finite_number",
        ),
        (
            rest.MusicDownloadRequest,
            eserver._handle_music_download,
            {"tracks": [{"id": 5}]},
            "string_type",
        ),
        (
            rest.MusicDownloadRequest,
            eserver._handle_music_download,
            {"tracks": [{"id": "1", "sources": "netease"}]},
            "list_type",
        ),
    ]
    for model, handler, payload, expected in cases:
        with pytest.raises(ValidationError) as rest_error:
            model.model_validate(payload)
        with pytest.raises(eserver._ApiError) as embedded_error:
            handler(payload, {}, {})
        assert rest_error.value.errors()[0]["type"] == expected, payload
        assert embedded_error.value.errors[0]["type"] == expected, payload


# ---------------------------------------------------------------------------
# normalisation rules that a status-only check would miss
# ---------------------------------------------------------------------------


def test_search_sites_are_stripped_and_deduplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``[" Qidian ", "qidian"]`` reaches the engine as ``["Qidian"]``."""
    seen: dict[str, Any] = {}

    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        seen.update(kwargs)
        return []

    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)
    payload = eserver._handle_search(
        {"keyword": "x", "sites": [" Qidian ", "qidian"]}, {}, {}
    )
    assert payload == {"total": 0, "results": []}
    assert seen["sites"] == ["Qidian"]


def test_task_fields_are_stripped_like_the_rest_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_add(*, title: str, site: str, book_id: str) -> SimpleNamespace:
        captured.update({"title": title, "site": site, "book_id": book_id})
        return SimpleNamespace(
            task_id="t1",
            title=title,
            site=site,
            book_id=book_id,
            status=SimpleNamespace(value="queued"),
            chapters_done=0,
            chapters_total=0,
            error=None,
            exported_paths={},
            created_at=0.0,
            finished_at=None,
            export_stage=None,
            progress=lambda: 0.0,
        )

    monkeypatch.setattr(
        "novel_downloader.apps.core.task_manager.manager.add_task", fake_add
    )
    eserver._handle_tasks_post(
        {"site": " qidian ", "book_id": " 42 ", "title": "  Title  "}, {}, {}
    )
    assert captured == {"title": "Title", "site": "qidian", "book_id": "42"}


def test_watchlist_delete_validates_path_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed path id is a 422, not a misleading 404."""
    with pytest.raises(eserver._ApiError) as error:
        eserver._handle_watchlist_delete({}, {"site": "bad site", "book_id": "1"}, {})
    assert error.value.status == 422
    assert error.value.errors[0]["loc"] == ["path", "site"]

    with pytest.raises(eserver._ApiError) as error:
        eserver._handle_watchlist_delete({}, {"site": "qidian", "book_id": "a/b"}, {})
    assert error.value.status == 422
    assert error.value.errors[0]["loc"] == ["path", "book_id"]


# ---------------------------------------------------------------------------
# music download failures and job lifecycle
# ---------------------------------------------------------------------------


def test_music_download_engine_failure_is_502_without_leaking(
    music: SimpleNamespace,
) -> None:
    secret = "signed https://cdn.example/a.mp3?token=SECRET"

    def exploding(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(secret)

    music.download_mixed = exploding

    with pytest.raises(eserver._ApiError) as error:
        eserver._handle_music_download(
            {"platform": "netease", "tracks": [{"id": "1"}]}, {}, {}
        )

    # REST maps engine failures to 502 with only the exception class.
    assert error.value.status == 502
    assert error.value.detail == "Music download failed: RuntimeError"
    assert "SECRET" not in error.value.detail


def test_music_platform_init_failure_is_sanitized_500(
    music: SimpleNamespace,
) -> None:
    def broken_create(key: str) -> Any:
        raise RuntimeError("secret /data/user/0 path")

    music.create = broken_create

    with pytest.raises(eserver._ApiError) as error:
        eserver._handle_music_search({"platform": "netease", "keyword": "x"}, {}, {})

    assert error.value.status == 500
    assert error.value.detail == (
        "Could not initialise music platform 'netease': RuntimeError"
    )
    assert "secret" not in error.value.detail


def test_job_timeout_is_enforced_without_callbacks(
    music: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung engine must not leave a job ``running`` forever."""
    gate = threading.Event()

    def hung(*args: Any, **kwargs: Any) -> Any:
        gate.wait(5.0)
        return 0, 0, 0, [], []

    music.download_mixed = hung
    monkeypatch.setattr(eserver, "_MUSIC_JOB_TIMEOUT", 0.05)
    created = eserver._handle_music_jobs_post(
        {"platform": "netease", "tracks": [{"id": "1"}]}, {}, {}
    )
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            job = eserver._handle_music_job_status(
                {}, {"job_id": created["job_id"]}, {}
            )
            if job["status"] in ("done", "failed"):
                break
            time.sleep(0.01)
        assert job["status"] == "failed"
        assert job["error"] == "Music download timed out after 0.05s"
    finally:
        gate.set()


def test_job_failure_keeps_partial_results(
    music: SimpleNamespace,
) -> None:
    """REST records the tracks finished before an engine failure."""

    def half_then_fail(
        provider: Any, tracks: list[Any], options: Any, *, on_result: Any = None
    ) -> Any:
        on_result(
            0,
            tracks[0],
            SimpleNamespace(status="ok", reason="", path="/music/a.mp3", bytes_=3),
        )
        raise RuntimeError("provider exploded")

    music.download_mixed = half_then_fail
    created = eserver._handle_music_jobs_post(
        {"platform": "netease", "tracks": [{"id": "1"}, {"id": "2"}]}, {}, {}
    )

    deadline = time.time() + 5
    while time.time() < deadline:
        job = eserver._handle_music_job_status({}, {"job_id": created["job_id"]}, {})
        if job["status"] in ("done", "failed"):
            break
        time.sleep(0.01)

    assert job["status"] == "failed"
    assert job["error"] == "Music download failed: RuntimeError"
    assert len(job["results"]) == 1
    assert job["progress"]["done"] == 1
