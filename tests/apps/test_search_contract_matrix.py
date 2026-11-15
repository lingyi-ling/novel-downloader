#!/usr/bin/env python3
"""
tests.apps.test_search_contract_matrix
--------------------------------------

Cross-surface contract matrix for search explainability.

The aggregate search attaches three derived, read-only fields to every final
row (``rank_score`` 0..1, ``match_tier`` 0..4 and ``query_intent``
``"title"``/``"author"``) and exposes the list-level intent in the REST and
embedded envelopes.  Four independent front ends consume that payload:

* the CLI's ``--json`` document (one JSON array),
* the REST ``POST /api/v1/search`` envelope,
* the embedded Android engine's ``POST /api/v1/search`` envelope,
* the desktop bridge's ranked rows (which add the ``best`` flag).

Nothing in the repository currently drives *one* fixture through all four and
compares the values field by field; each surface is tested against its own
hand-written expectation.  This module closes that gap.  A single fake
``search`` applies the real
:func:`~novel_downloader.plugins.search._finalize` step to the same rows, so
every surface sees the same ranking, filtering, grouping and derivation, and
only the transport/adaptation layer can introduce a difference.

It also pins the compatibility edges the explainability fields imply:

* a missing field (older cache snapshot / streamed preview) is passed through
  untouched by the data surfaces and normalised to ``None`` by the desktop
  bridge, never invented;
* an invalid field is passed through verbatim by the data surfaces and
  dropped/normalised by the desktop bridge, so the page can never paint a
  badge the ranking did not assign;
* the REST SSE stream never gains ranking telemetry;
* a cache hit re-ranks per request while the cached snapshot keeps no derived
  field.

All tests are offline: the aggregate search is replaced by a local fake.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import threading
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from datetime import date
from typing import Any

import httpx
import pytest
import pytest_asyncio

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.cli.commands.base import EXIT_USAGE
from novel_downloader.apps.cli.commands.search import SearchCmd
from novel_downloader.apps.desktop.webview_app import ApiBridge
from novel_downloader.apps.embedded import server as eserver
from novel_downloader.plugins import registrar
from novel_downloader.plugins.ranking import default_order

#: The key ``_finalize`` adds to every final row.
DERIVED_FIELDS: frozenset[str] = frozenset({"rank_score", "match_tier", "query_intent"})

#: Every content field a raw searcher row carries.
CONTENT_FIELDS: frozenset[str] = frozenset(
    {
        "site",
        "book_id",
        "book_url",
        "cover_url",
        "title",
        "author",
        "latest_chapter",
        "update_date",
        "word_count",
        "priority",
    }
)

#: Keyword / site selection shared by the flat and filtered cases.
KEYWORD = "斗破苍穹"
SITES = ["alpha", "beta", "gamma"]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _row(
    site: str,
    book_id: str,
    title: str,
    author: str,
    *,
    update_date: str = "2020-01-01",
    word_count: str = "30万字",
    priority: int = 1,
    cover_url: str = "",
    latest_chapter: str = "第1章",
) -> dict[str, Any]:
    """Build one raw searcher row (no derived field)."""
    return {
        "site": site,
        "book_id": book_id,
        "book_url": f"https://example.invalid/{site}/{book_id}",
        "cover_url": cover_url,
        "title": title,
        "author": author,
        "latest_chapter": latest_chapter,
        "update_date": update_date,
        "word_count": word_count,
        "priority": priority,
    }


def _main_rows() -> list[dict[str, Any]]:
    """Exact title, prefix match and unrelated title; distinct metadata.

    The update dates are far in the past, so the recency signal is the same
    ``0.5`` bucket on every call: the derived scores are deterministic.
    """
    return [
        _row(
            "alpha",
            "alpha-1",
            "斗破苍穹",
            "天蚕土豆",
            update_date="2020-01-01",
            word_count="300万字",
            priority=1,
            cover_url="https://example.invalid/alpha/1.png",
        ),
        _row(
            "beta",
            "beta-1",
            "斗破苍穹之雷霆震碎",
            "天蚕土豆",
            update_date="2020-06-01",
            word_count="10万字",
            priority=100,
            cover_url="https://example.invalid/beta/1.png",
        ),
        _row(
            "gamma",
            "gamma-1",
            "天蚕土豆",
            "别的作者",
            update_date="",
            word_count="1万字",
            priority=50,
        ),
    ]


def _group_rows() -> list[dict[str, Any]]:
    """Two sites carrying the same book plus a second one, all author hits."""
    return [
        _row(
            "alpha",
            "alpha-1",
            "三体",
            "刘慈欣",
            word_count="300万字",
            priority=1,
            cover_url="https://example.invalid/alpha/1.png",
        ),
        _row(
            "beta",
            "beta-1",
            "三体",
            "刘慈欣",
            word_count="10万字",
            priority=100,
            cover_url="https://example.invalid/beta/1.png",
        ),
        _row(
            "gamma",
            "gamma-1",
            "三体II",
            "刘慈欣",
            word_count="5万字",
            priority=50,
            cover_url="https://example.invalid/gamma/1.png",
        ),
    ]


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep config/cache state outside the real user profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def _cli_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub config/logging/selection so the CLI command never touches a tty."""
    from novel_downloader.apps.cli.commands import search as module

    monkeypatch.setattr(
        module, "load_or_init_config", lambda *_a, **_k: {"general": {}}
    )
    monkeypatch.setattr(module.ui, "setup_logging", lambda **_k: None)
    monkeypatch.setattr(module.ui, "status", lambda _message: nullcontext())
    monkeypatch.setattr(module.prompts, "select_search_result", lambda _r: None)
    # The fixture sites are not registered searchers; this matrix is about the
    # contract, not the registry, so the CLI's typo guard is satisfied here.
    monkeypatch.setattr(module, "_unknown_search_sites", lambda _sites: [])
    monkeypatch.setattr(module, "_available_search_sites", lambda: [])


@pytest_asyncio.fixture
async def rest_client(tmp_path: Any) -> Any:
    application = create_app(token="", output_dir=str(tmp_path / "downloads"))
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client


@pytest.fixture()
def bridge() -> Any:
    api = ApiBridge()
    try:
        yield api
    finally:
        api.shutdown()


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    SearchCmd.register(subparsers)
    return parser


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Run ``SearchCmd.run`` on a worker thread and capture its streams.

    The command calls ``asyncio.run`` itself, which cannot happen inside the
    event loop an async test runs on; a thread gives it a private loop.  Rich
    resolves ``sys.stderr`` dynamically, so redirecting the global streams
    captures both the JSON document and the diagnostics.
    """
    args = _parser().parse_args(["search", *argv])
    out = io.StringIO()
    err = io.StringIO()
    code = 0

    def target() -> None:
        nonlocal code
        try:
            with redirect_stdout(out), redirect_stderr(err):
                args.func(args)
        except SystemExit as exc:
            code = 0 if exc.code is None else int(exc.code)

    thread = threading.Thread(target=target, name="contract-matrix-cli")
    thread.start()
    thread.join(30)
    assert not thread.is_alive(), "the CLI command did not finish"
    return code, out.getvalue(), err.getvalue()


async def _run_cli_async(argv: list[str]) -> tuple[int, str, str]:
    return await asyncio.to_thread(_run_cli, argv)


def _install_ranked(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, Any]],
    calls: list[dict[str, Any]],
) -> None:
    """Serve every surface from one fake that applies the real finalisation."""

    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        from novel_downloader.plugins.search import _finalize

        calls.append({"keyword": keyword, **kwargs})
        return _finalize(
            [dict(row) for row in rows],
            keyword=keyword,
            sort=str(kwargs.get("sort") or "quality"),
            order=kwargs.get("order"),
            limit=kwargs.get("limit"),
            group=bool(kwargs.get("group")),
            min_relevance=None,
            min_words=kwargs.get("min_words"),
            max_words=kwargs.get("max_words"),
            updated_after=kwargs.get("updated_after"),
        )

    monkeypatch.setattr(api_routes, "search", fake_search)
    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)


def _install_passthrough(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, Any]],
    calls: list[dict[str, Any]],
) -> None:
    """Serve rows exactly as given (an older snapshot or a streamed chunk)."""

    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append({"keyword": keyword, **kwargs})
        return [dict(row) for row in rows]

    monkeypatch.setattr(api_routes, "search", fake_search)
    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)


def _embedded_search(body: dict[str, Any]) -> dict[str, Any]:
    """Run the embedded handler and round-trip it like the wire does."""
    payload = eserver._handle_search(body, {}, {})
    return json.loads(json.dumps(payload))


def _effective_call(call: dict[str, Any]) -> dict[str, Any]:
    """The transport-independent search arguments of one forwarded call.

    ``order=None`` and the key's natural direction mean the same thing to the
    ranking layer, and the desktop bridge resolves the direction before
    calling while the other surfaces leave it ``None``; normalising here lets
    the four surfaces be compared on effect instead of spelling.
    """
    sort_key = str(call.get("sort") or "quality")
    order = call.get("order")
    return {
        "keyword": call.get("keyword"),
        "sites": list(call.get("sites") or []),
        "limit": call.get("limit"),
        "per_site_limit": call.get("per_site_limit"),
        "sort": sort_key,
        "order": str(order) if order else default_order(sort_key),
        "group": bool(call.get("group")),
        "min_words": call.get("min_words"),
        "max_words": call.get("max_words"),
        "updated_after": call.get("updated_after"),
    }


def _assert_same_calls(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Every surface forwarded the same effective search arguments."""
    assert len(calls) >= 2
    first = _effective_call(calls[0])
    for call in calls[1:]:
        assert _effective_call(call) == first
    return first


def _without_best(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in row.items() if key != "best"} for row in rows]


def _parse_sse(raw: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    for block in raw.split("\n\n"):
        lines = [line for line in block.split("\n") if line]
        if not any(line.startswith("event: ") for line in lines):
            continue
        name = next(
            line[len("event: ") :] for line in lines if line.startswith("event: ")
        )
        data = next(
            line[len("data: ") :] for line in lines if line.startswith("data: ")
        )
        events.append((name, json.loads(data)))
    return events


def _assert_ranked_row(row: dict[str, Any]) -> None:
    score = row["rank_score"]
    assert isinstance(score, float) and not isinstance(score, bool)
    assert 0.0 <= score <= 1.0
    tier = row["match_tier"]
    assert isinstance(tier, int) and not isinstance(tier, bool)
    assert 0 <= tier <= 4
    assert row["query_intent"] in ("title", "author", None)


# ---------------------------------------------------------------------------
# flat / grouped parity: CLI JSON, REST, embedded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flat_quality_rows_and_intent_match_across_surfaces(
    rest_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    _install_ranked(monkeypatch, _main_rows(), calls)
    body: dict[str, Any] = {
        "keyword": KEYWORD,
        "sites": SITES,
        "per_site_limit": 5,
        "sort": "quality",
    }

    response = await rest_client.post("/api/v1/search", json=body)
    assert response.status_code == 200
    rest_payload = response.json()
    embedded_payload = _embedded_search(body)
    code, out, _err = await _run_cli_async(
        [
            "--json",
            "--site",
            "alpha",
            "--site",
            "beta",
            "--site",
            "gamma",
            "--site-limit",
            "5",
            KEYWORD,
        ]
    )
    assert code == 0
    cli_rows = json.loads(out)

    # One envelope, one JSON array, identical rows field for field.
    assert embedded_payload == rest_payload
    assert cli_rows == rest_payload["results"]
    assert rest_payload["total"] == len(cli_rows) == 3
    assert set(rest_payload) == {"total", "results", "query_intent"}
    for row in cli_rows:
        assert set(row) == CONTENT_FIELDS | DERIVED_FIELDS
        _assert_ranked_row(row)

    # The intent is derived from the same rows on every surface...
    assert rest_payload["query_intent"] == "title"
    assert {row["query_intent"] for row in cli_rows} == {"title"}
    # ...and the tier ordering (exact, prefix, unrelated) is identical.
    assert [row["match_tier"] for row in cli_rows] == [4, 3, 0]
    assert [row["book_id"] for row in cli_rows] == ["alpha-1", "beta-1", "gamma-1"]

    effective = _assert_same_calls(calls)
    assert effective["sites"] == SITES
    assert effective["sort"] == "quality"
    assert effective["order"] == "desc"
    assert effective["group"] is False
    assert effective["per_site_limit"] == 5
    assert effective["limit"] is None
    # The forwarding *spelling* differs by surface and is pinned here: the
    # data transports omit an inactive filter while the CLI spells it None.
    # Both mean "no filter" to ``search``, hence the effective comparison.
    assert "min_words" not in calls[0]
    assert "min_words" not in calls[1]
    assert calls[2]["min_words"] is None


@pytest.mark.asyncio
async def test_grouped_quality_rows_and_sources_match_across_surfaces(
    rest_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    _install_ranked(monkeypatch, _group_rows(), calls)
    body: dict[str, Any] = {
        "keyword": "刘慈欣",
        "sites": SITES,
        "per_site_limit": 5,
        "sort": "quality",
        "group": True,
    }

    response = await rest_client.post("/api/v1/search", json=body)
    assert response.status_code == 200
    rest_payload = response.json()
    embedded_payload = _embedded_search(body)
    code, out, _err = await _run_cli_async(
        [
            "--json",
            "--group",
            "--site",
            "alpha",
            "--site",
            "beta",
            "--site",
            "gamma",
            "--site-limit",
            "5",
            "刘慈欣",
        ]
    )
    assert code == 0
    cli_rows = json.loads(out)

    assert embedded_payload == rest_payload
    assert cli_rows == rest_payload["results"]
    # Two books: the cross-site duplicate merges, the second stays separate.
    assert [row["book_id"] for row in cli_rows] == ["alpha-1", "gamma-1"]
    assert cli_rows[0]["sources"] == ["alpha", "beta"]
    assert rest_payload["query_intent"] == "author"
    for row in cli_rows:
        assert set(row) == CONTENT_FIELDS | DERIVED_FIELDS | {"sources"}
        _assert_ranked_row(row)

    effective = _assert_same_calls(calls)
    assert effective["sites"] == SITES
    assert effective["group"] is True
    assert effective["sort"] == "quality"


# ---------------------------------------------------------------------------
# sort keys and the quality-only intent verdict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sort_key", "direction"),
    [
        ("priority", "asc"),
        ("site", "desc"),
        ("title", "asc"),
        ("author", "desc"),
        ("words", "asc"),
        ("update", "desc"),
    ],
)
@pytest.mark.asyncio
async def test_non_quality_sorts_match_and_report_no_intent(
    rest_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    sort_key: str,
    direction: str,
) -> None:
    """An explicit sort is forwarded identically and yields a null verdict."""
    calls: list[dict[str, Any]] = []
    _install_ranked(monkeypatch, _main_rows(), calls)
    body: dict[str, Any] = {
        "keyword": KEYWORD,
        "sites": SITES,
        "per_site_limit": 5,
        "sort": sort_key,
        "order": direction,
    }

    response = await rest_client.post("/api/v1/search", json=body)
    assert response.status_code == 200
    rest_payload = response.json()
    embedded_payload = _embedded_search(body)
    code, out, _err = await _run_cli_async(
        [
            "--json",
            "--site",
            "alpha",
            "--site",
            "beta",
            "--site",
            "gamma",
            "--site-limit",
            "5",
            "--sort",
            sort_key,
            "--order",
            direction,
            KEYWORD,
        ]
    )
    assert code == 0
    cli_rows = json.loads(out)

    assert embedded_payload == rest_payload
    assert cli_rows == rest_payload["results"]
    assert rest_payload["query_intent"] is None
    assert all(row["query_intent"] is None for row in cli_rows)

    effective = _assert_same_calls(calls)
    assert effective["sort"] == sort_key
    assert effective["order"] == direction


# ---------------------------------------------------------------------------
# local filters
# ---------------------------------------------------------------------------

#: ``(filters, expected book ids after ranking)``.  The third row has an
#: unparseable word count and update date, so it is dropped whenever the
#: corresponding filter is active -- the documented compatibility rule.
_FILTER_CASES: list[tuple[dict[str, Any], list[str]]] = [
    ({}, ["alpha-1", "beta-1", "gamma-1"]),
    (
        {"min_words": 50000},
        ["alpha-1", "beta-1"],
    ),
    (
        {"max_words": 50000},
        ["gamma-1"],
    ),
    (
        {"updated_after": "2020-03-01"},
        ["beta-1"],
    ),
    (
        {"min_words": 0, "max_words": 1_000_000_000},
        ["alpha-1", "beta-1", "gamma-1"],
    ),
]


def _filter_cli_args(filters: dict[str, Any]) -> list[str]:
    argv = ["--json", "--site", "alpha", "--site", "beta", "--site", "gamma"]
    argv += ["--site-limit", "5"]
    if "min_words" in filters:
        argv += ["--min-words", str(filters["min_words"])]
    if "max_words" in filters:
        argv += ["--max-words", str(filters["max_words"])]
    if "updated_after" in filters:
        argv += ["--updated-after", str(filters["updated_after"])]
    return [*argv, KEYWORD]


@pytest.mark.parametrize(("filters", "expected_ids"), _FILTER_CASES)
@pytest.mark.asyncio
async def test_filtered_rows_match_across_all_four_surfaces(
    rest_client: httpx.AsyncClient,
    bridge: ApiBridge,
    monkeypatch: pytest.MonkeyPatch,
    filters: dict[str, Any],
    expected_ids: list[str],
) -> None:
    calls: list[dict[str, Any]] = []
    _install_ranked(monkeypatch, _main_rows(), calls)
    body: dict[str, Any] = {
        "keyword": KEYWORD,
        "sites": SITES,
        "per_site_limit": 5,
        "sort": "quality",
        **filters,
    }

    response = await rest_client.post("/api/v1/search", json=body)
    assert response.status_code == 200
    rest_payload = response.json()
    embedded_payload = _embedded_search(body)
    code, out, _err = await _run_cli_async(_filter_cli_args(filters))
    assert code == 0
    cli_rows = json.loads(out)
    desktop = bridge.search(
        KEYWORD,
        ",".join(SITES),
        False,
        5,
        str(filters["min_words"]) if "min_words" in filters else None,
        str(filters["max_words"]) if "max_words" in filters else None,
        filters.get("updated_after"),
    )

    assert embedded_payload == rest_payload
    assert cli_rows == rest_payload["results"]
    assert _without_best(desktop["results"]) == rest_payload["results"]
    assert rest_payload["total"] == len(expected_ids)
    assert [row["book_id"] for row in cli_rows] == expected_ids

    effective = _assert_same_calls(calls)
    for field in ("min_words", "max_words", "updated_after"):
        expected = filters.get(field)
        if field == "updated_after" and expected is not None:
            expected = date.fromisoformat(str(expected))
        assert effective[field] == expected, field
        if field in filters:
            # An active filter reaches every surface's call by name.
            assert all(field in call for call in calls), field


@pytest.mark.parametrize(
    ("field", "value", "flag"),
    [
        ("min_words", -1, "--min-words"),
        ("max_words", 1_000_000_001, "--max-words"),
        ("updated_after", "2024/01/01", "--updated-after"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_filters_are_rejected_by_every_surface(
    rest_client: httpx.AsyncClient,
    bridge: ApiBridge,
    field: str,
    value: Any,
    flag: str,
) -> None:
    """One bad filter value never reaches the search layer anywhere."""
    body: dict[str, Any] = {"keyword": KEYWORD, "sites": ["alpha"], field: value}
    response = await rest_client.post("/api/v1/search", json=body)
    assert response.status_code == 422

    with pytest.raises(eserver._ApiError) as embedded_error:
        eserver._handle_search(body, {}, {})
    assert embedded_error.value.status == 422

    code, out, err = await _run_cli_async(
        ["--json", "--site", "alpha", flag, str(value), KEYWORD]
    )
    assert code == EXIT_USAGE
    assert out == ""
    assert flag in err

    desktop = bridge.search(KEYWORD, "alpha", **{field: value})
    assert desktop["results"] == []
    assert desktop["ok"] is False
    assert desktop["error_code"] == "invalid_search_filter"
    assert desktop["error_field"] == field


# ---------------------------------------------------------------------------
# desktop bridge: ranked rows aligned with the REST payload
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sort_key", "expected_intent"),
    [
        ("quality", "title"),
        ("priority", None),
        ("words", None),
        ("update", None),
    ],
)
@pytest.mark.asyncio
async def test_desktop_ranked_rows_match_rest_field_for_field(
    rest_client: httpx.AsyncClient,
    bridge: ApiBridge,
    monkeypatch: pytest.MonkeyPatch,
    sort_key: str,
    expected_intent: str | None,
) -> None:
    calls: list[dict[str, Any]] = []
    _install_ranked(monkeypatch, _main_rows(), calls)
    body: dict[str, Any] = {
        "keyword": KEYWORD,
        "sites": SITES,
        "per_site_limit": 5,
        "sort": sort_key,
    }

    response = await rest_client.post("/api/v1/search", json=body)
    assert response.status_code == 200
    rest_payload = response.json()
    desktop = bridge.search(KEYWORD, ",".join(SITES), False, 5, sort=sort_key)

    assert _without_best(desktop["results"]) == rest_payload["results"]
    assert [row["best"] for row in desktop["results"]] == [True, False, False]
    assert rest_payload["query_intent"] == expected_intent
    assert {row["query_intent"] for row in desktop["results"]} == {expected_intent}

    effective = _assert_same_calls(calls)
    assert effective["sort"] == sort_key
    assert effective["group"] is False


@pytest.mark.asyncio
async def test_desktop_grouped_rows_match_rest_including_sources(
    rest_client: httpx.AsyncClient,
    bridge: ApiBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    _install_ranked(monkeypatch, _group_rows(), calls)
    body: dict[str, Any] = {
        "keyword": "刘慈欣",
        "sites": SITES,
        "per_site_limit": 5,
        "sort": "quality",
        "group": True,
    }

    response = await rest_client.post("/api/v1/search", json=body)
    assert response.status_code == 200
    rest_payload = response.json()
    desktop = bridge.search("刘慈欣", ",".join(SITES), True, 5)

    assert _without_best(desktop["results"]) == rest_payload["results"]
    assert desktop["results"][0]["sources"] == ["alpha", "beta"]
    assert [row["best"] for row in desktop["results"]] == [True, False]

    effective = _assert_same_calls(calls)
    assert effective["group"] is True


# ---------------------------------------------------------------------------
# missing / invalid derived fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_telemetry_passes_through_every_surface(
    rest_client: httpx.AsyncClient,
    bridge: ApiBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older snapshot/streamed chunk carries no derived key: none is invented."""
    rows = _main_rows()
    calls: list[dict[str, Any]] = []
    _install_passthrough(monkeypatch, rows, calls)
    body: dict[str, Any] = {
        "keyword": KEYWORD,
        "sites": SITES,
        "per_site_limit": 5,
    }

    response = await rest_client.post("/api/v1/search", json=body)
    assert response.status_code == 200
    rest_payload = response.json()
    embedded_payload = _embedded_search(body)
    code, out, _err = await _run_cli_async(
        [
            "--json",
            "--site",
            "alpha",
            "--site",
            "beta",
            "--site",
            "gamma",
            "--site-limit",
            "5",
            KEYWORD,
        ]
    )
    assert code == 0
    cli_rows = json.loads(out)

    assert rest_payload["results"] == rows
    assert embedded_payload["results"] == rows
    assert cli_rows == rows
    assert rest_payload["query_intent"] is None
    assert embedded_payload["query_intent"] is None
    for surface_rows in (
        rest_payload["results"],
        embedded_payload["results"],
        cli_rows,
    ):
        assert all(DERIVED_FIELDS.isdisjoint(row) for row in surface_rows)

    # The desktop bridge keeps one stable shape for the page: the intent key
    # exists but is null, and the score/tier keys are absent, not fabricated.
    desktop = bridge.search(KEYWORD, ",".join(SITES), False, 5)
    for row in desktop["results"]:
        assert row["query_intent"] is None
        assert "rank_score" not in row
        assert "match_tier" not in row
        assert {
            key: value
            for key, value in row.items()
            if key not in ("best", "query_intent")
        } in rows
    assert [row["best"] for row in desktop["results"]] == [True, False, False]


@pytest.mark.asyncio
async def test_invalid_telemetry_is_passed_through_but_never_painted(
    rest_client: httpx.AsyncClient,
    bridge: ApiBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The data surfaces stay additive; the bridge drops unusable badges."""
    rows = _main_rows()
    rows[0].update({"rank_score": "0.9", "match_tier": True, "query_intent": "book"})
    rows[1].update({"rank_score": 1.5, "match_tier": 9, "query_intent": "author"})
    calls: list[dict[str, Any]] = []
    _install_passthrough(monkeypatch, rows, calls)
    body: dict[str, Any] = {
        "keyword": KEYWORD,
        "sites": SITES,
        "per_site_limit": 5,
    }

    response = await rest_client.post("/api/v1/search", json=body)
    assert response.status_code == 200
    rest_payload = response.json()
    embedded_payload = _embedded_search(body)
    code, out, _err = await _run_cli_async(
        [
            "--json",
            "--site",
            "alpha",
            "--site",
            "beta",
            "--site",
            "gamma",
            "--site-limit",
            "5",
            KEYWORD,
        ]
    )
    assert code == 0
    cli_rows = json.loads(out)

    # Additive data surfaces do not sanitise a caller-supplied row...
    assert rest_payload["results"] == rows
    assert embedded_payload["results"] == rows
    assert cli_rows == rows
    # ...but an unusable/conflicting verdict is reported as null, never guessed.
    assert rest_payload["query_intent"] is None
    assert embedded_payload["query_intent"] is None

    # The desktop bridge drops out-of-range score/tier and normalises intent.
    desktop = bridge.search(KEYWORD, ",".join(SITES), False, 5)
    assert len(desktop["results"]) == len(rows)
    first, second, third = desktop["results"]
    assert "rank_score" not in first and "match_tier" not in first
    assert first["query_intent"] is None
    assert "rank_score" not in second and "match_tier" not in second
    assert second["query_intent"] == "author"
    assert "rank_score" not in third and "match_tier" not in third
    assert third["query_intent"] is None
    assert first["book_id"] == "alpha-1"
    assert second["book_id"] == "beta-1"


# ---------------------------------------------------------------------------
# stream and cache: no derived field may leak into persisted/streamed rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_stream_never_adds_ranking_telemetry(
    rest_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SSE adapter forwards each site chunk verbatim, unranked."""
    rows = _main_rows()

    async def fake_stream(keyword: str, **kwargs: Any) -> Any:
        assert keyword == KEYWORD
        yield [dict(row) for row in rows]

    monkeypatch.setattr(api_routes, "search_stream", fake_stream)
    monkeypatch.setattr(api_routes, "unknown_search_sites", lambda _sites: [])

    response = await rest_client.get(
        "/api/v1/search/stream",
        params={"keyword": KEYWORD, "sites": ",".join(SITES)},
    )
    assert response.status_code == 200
    events = _parse_sse(response.text)

    results_events = [data for name, data in events if name == "results"]
    assert results_events == [rows]
    for row in results_events[0]:
        assert DERIVED_FIELDS.isdisjoint(row)
    assert events[-1] == ("done", {"total": len(rows)})
    assert "rank_score" not in response.text
    assert "query_intent" not in response.text


@pytest.mark.asyncio
async def test_cache_hit_reranks_per_request_and_keeps_the_snapshot_clean(
    rest_client: httpx.AsyncClient,
    bridge: ApiBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache hit is ranked per call; the stored rows never carry telemetry."""
    import novel_downloader.plugins.search as search_module

    snapshot = _main_rows()
    original = [dict(row) for row in snapshot]
    # ``SearchCache.get`` returns a copy of the stored list; hand each call its
    # own copy so the fake has the real cache's isolation semantics.
    monkeypatch.setattr(search_module._CACHE, "get", lambda _key: list(snapshot))
    monkeypatch.setattr(registrar, "get_searcher_classes", lambda *_a, **_k: [])
    body: dict[str, Any] = {
        "keyword": KEYWORD,
        "sites": SITES,
        "per_site_limit": 5,
        "sort": "quality",
    }

    response = await rest_client.post("/api/v1/search", json=body)
    assert response.status_code == 200
    rest_payload = response.json()
    embedded_payload = _embedded_search(body)
    code, out, _err = await _run_cli_async(
        [
            "--json",
            "--site",
            "alpha",
            "--site",
            "beta",
            "--site",
            "gamma",
            "--site-limit",
            "5",
            KEYWORD,
        ]
    )
    assert code == 0
    cli_rows = json.loads(out)
    desktop = bridge.search(KEYWORD, ",".join(SITES), False, 5)

    assert embedded_payload == rest_payload
    assert cli_rows == rest_payload["results"]
    assert _without_best(desktop["results"]) == rest_payload["results"]
    # The default relevance floor drops the unrelated title, identically.
    assert [row["book_id"] for row in cli_rows] == ["alpha-1", "beta-1"]
    assert rest_payload["query_intent"] == "title"
    for row in cli_rows:
        _assert_ranked_row(row)

    # The snapshot is exactly the raw rows: no derived field, no merge key.
    assert snapshot == original
    assert all(DERIVED_FIELDS.isdisjoint(row) for row in snapshot)
    assert all("sources" not in row for row in snapshot)
    # Returned rows are copies; mutating one cannot reach the cache.
    rest_payload["results"][0]["title"] = "tampered"
    assert snapshot[0]["title"] == "斗破苍穹"

    # A second request re-ranks the same snapshot under another sort key.
    response = await rest_client.post(
        "/api/v1/search",
        json={
            "keyword": KEYWORD,
            "sites": SITES,
            "per_site_limit": 5,
            "sort": "words",
            "order": "asc",
        },
    )
    assert response.status_code == 200
    second = response.json()
    assert [row["book_id"] for row in second["results"]] == [
        "gamma-1",
        "beta-1",
        "alpha-1",
    ]
    assert second["query_intent"] is None
    assert all(row["query_intent"] is None for row in second["results"])
    assert snapshot == original
