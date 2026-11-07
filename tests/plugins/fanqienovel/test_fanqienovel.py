#!/usr/bin/env python3
import json
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps import browser
from novel_downloader.plugins.sites.fanqienovel.searcher import FanqienovelSearcher
from novel_downloader.schemas import SearchResult

_SEARCH_PAYLOAD = {
    "code": 0,
    "data": {
        "search_book_data_list": [
            {
                "book_id": "7143038691195",
                "book_name": "斗破苍穹",
                "author": "天蚕土豆",
                "thumb_url": "//example.com/cover.jpg",
                "word_count": 5353400,
                "last_chapter_title": "第1623章 大结局",
                "last_chapter_time": 1706000000,
            },
            {
                "book_id": "7143038691196",
                "book_name": "武破苍穹",
                "author": "破苍天",
                "thumb_url": "http://example.com/cover2.jpg",
                "word_count": 540000,
                "last_chapter_title": "第四十五章 神奇小兽",
                "last_chapter_time": "2023-04-20 18:22:08",
            },
        ],
        "search_author_data_list": [],
        "total_count": 2,
    },
}


def test_searcher_parse_html() -> None:
    searcher = FanqienovelSearcher.__new__(FanqienovelSearcher)
    results = searcher._parse_html(json.dumps(_SEARCH_PAYLOAD, ensure_ascii=False))
    assert results == [
        SearchResult(
            site="fanqienovel",
            book_id="7143038691195",
            book_url="https://fanqienovel.com/page/7143038691195",
            cover_url="https://example.com/cover.jpg",
            title="斗破苍穹",
            author="天蚕土豆",
            latest_chapter="第1623章 大结局",
            update_date="2024-01-23",
            word_count="5353400",
            priority=5,
        ),
        SearchResult(
            site="fanqienovel",
            book_id="7143038691196",
            book_url="https://fanqienovel.com/page/7143038691196",
            cover_url="https://example.com/cover2.jpg",
            title="武破苍穹",
            author="破苍天",
            latest_chapter="第四十五章 神奇小兽",
            update_date="2023-04-20 18:22:08",
            word_count="540000",
            priority=6,
        ),
    ]


def test_searcher_parse_html_handles_garbage() -> None:
    searcher = FanqienovelSearcher.__new__(FanqienovelSearcher)
    assert searcher._parse_html("<<<not json>>>") == []
    assert searcher._parse_html("") == []
    assert searcher._parse_html("[]") == []
    assert searcher._parse_html(json.dumps({"data": {}})) == []


def test_searcher_skips_items_without_book_id() -> None:
    searcher = FanqienovelSearcher.__new__(FanqienovelSearcher)
    payload = {"data": {"search_book_data_list": [{"book_name": "无ID"}, "junk"]}}
    assert searcher._parse_html(json.dumps(payload)) == []


def test_searcher_respects_limit() -> None:
    searcher = FanqienovelSearcher.__new__(FanqienovelSearcher)
    items = [{"book_id": str(i), "book_name": f"书{i}"} for i in range(5)]
    payload = json.dumps({"data": {"search_book_data_list": items}})
    assert len(searcher._parse_html(payload, limit=2)) == 2


def test_load_cookies_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FANQIENOVEL_COOKIE", "a=1; b=2")
    assert FanqienovelSearcher._load_cookies() == {"a": "1", "b": "2"}


def test_load_cookies_from_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("FANQIENOVEL_COOKIE", raising=False)
    monkeypatch.setenv("NOVEL_CACHE_DIR", str(tmp_path))
    cookie_dir = tmp_path / "fanqienovel"
    cookie_dir.mkdir()
    (cookie_dir / "aiohttp.cookies").write_text(
        json.dumps([{"name": "sessionid", "value": "abc"}]),
        encoding="utf-8",
    )
    assert FanqienovelSearcher._load_cookies() == {"sessionid": "abc"}


@pytest.mark.asyncio
async def test_fetch_without_cookie_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searcher = FanqienovelSearcher.__new__(FanqienovelSearcher)
    searcher._cookies = {}
    assert await searcher._fetch_html("斗破苍穹") == ""


_BOOK_PAGE = (
    "<html><script>window.__INITIAL_STATE__ = "
    '{"page":{"bookName":"穿越：我成为了萧炎的义兄？",'
    '"authorName":"路过的独眼死神"}};</script></html>'
)


def _make_searcher(**attrs: Any) -> FanqienovelSearcher:
    """Build a searcher without running the network-bound constructor."""
    searcher = FanqienovelSearcher.__new__(FanqienovelSearcher)
    searcher._cookies = {}
    searcher.session = _FakeSession("")  # type: ignore[assignment]
    for name, value in attrs.items():
        setattr(searcher, name, value)
    return searcher


class _FakeResponse:
    charset = "utf-8"

    def __init__(self, text: str) -> None:
        self._text = text

    def raise_for_status(self) -> None:
        return None

    async def read(self) -> bytes:
        return self._text.encode("utf-8")


class _FakeGet:
    def __init__(self, text: str) -> None:
        self._text = text

    async def __aenter__(self) -> _FakeResponse:
        return _FakeResponse(self._text)

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    headers = {"User-Agent": "test-agent"}

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeGet:
        self.calls.append((url, headers or {}))
        return _FakeGet(self._text)


@pytest.mark.asyncio
async def test_search_without_explicit_selection_uses_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searcher = _make_searcher()

    async def _fake_fetch(self: FanqienovelSearcher, _keyword: str) -> str:
        return json.dumps(_SEARCH_PAYLOAD, ensure_ascii=False)

    monkeypatch.setattr(FanqienovelSearcher, "_fetch_html", _fake_fetch)

    def _unexpected(*args: Any, **kwargs: Any) -> str | None:
        raise AssertionError("the browser must not be used for aggregate searches")

    monkeypatch.setattr(browser, "search_via_browser", _unexpected)

    results = await searcher.search("斗破苍穹")
    assert [item["title"] for item in results] == ["斗破苍穹", "武破苍穹"]


@pytest.mark.asyncio
async def test_explicit_search_uses_browser_and_book_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searcher = _make_searcher(explicitly_selected=True)
    calls: list[tuple[Any, ...]] = []

    def _fake_browser_search(site: str, keyword: str, **kwargs: Any) -> str:
        calls.append((site, keyword, kwargs))
        return json.dumps(_SEARCH_PAYLOAD, ensure_ascii=False)

    async def _fake_book_page(
        self: FanqienovelSearcher, book_id: str, headers: dict[str, str]
    ) -> dict[str, Any]:
        return {"bookName": f"真实书名-{book_id}", "authorName": "真实作者"}

    monkeypatch.setattr(browser, "search_via_browser", _fake_browser_search)
    monkeypatch.setattr(FanqienovelSearcher, "_fetch_book_page", _fake_book_page)

    results = await searcher.search("斗破苍穹")

    assert calls[0][0] == "fanqienovel"
    assert calls[0][1] == "斗破苍穹"
    assert [item["title"] for item in results] == [
        "真实书名-7143038691195",
        "真实书名-7143038691196",
    ]
    assert results[0]["author"] == "真实作者"


@pytest.mark.asyncio
async def test_explicit_search_falls_back_to_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searcher = _make_searcher(explicitly_selected=True)

    monkeypatch.setattr(browser, "search_via_browser", lambda *a, **k: None)

    async def _fake_fetch(self: FanqienovelSearcher, _keyword: str) -> str:
        return json.dumps(_SEARCH_PAYLOAD, ensure_ascii=False)

    monkeypatch.setattr(FanqienovelSearcher, "_fetch_html", _fake_fetch)

    results = await searcher.search("斗破苍穹")
    assert len(results) == 2


@pytest.mark.asyncio
async def test_fill_book_metadata_sends_cookies_and_fixes_titles() -> None:
    searcher = _make_searcher(_cookies={"sessionid": "abc"})
    session = _FakeSession(_BOOK_PAGE)
    searcher.session = session  # type: ignore[assignment]

    results = searcher._parse_html(json.dumps(_SEARCH_PAYLOAD, ensure_ascii=False))
    await searcher._fill_book_metadata(results)

    assert all(item["title"] == "穿越：我成为了萧炎的义兄？" for item in results)
    assert all(item["author"] == "路过的独眼死神" for item in results)
    assert session.calls[0][0] == "https://fanqienovel.com/page/7143038691195"
    assert session.calls[0][1]["Cookie"] == "sessionid=abc"


@pytest.mark.asyncio
async def test_fetch_book_page_tolerates_broken_html() -> None:
    searcher = _make_searcher()
    searcher.session = _FakeSession("<html>no state here</html>")  # type: ignore[assignment]
    assert await searcher._fetch_book_page("123", {}) == {}


@pytest.fixture(autouse=True)
def _no_app_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep these tests off the network by default."""
    monkeypatch.setattr(FanqienovelSearcher, "_app_device", lambda self: None)

    async def _no_mint(self: Any) -> None:
        return None

    monkeypatch.setattr(FanqienovelSearcher, "_ensure_device", _no_mint)


_APP_PAYLOAD = {
    "code": 0,
    "data": {
        "has_more": False,
        "offset": 0,
        "ret_data": [
            {
                "book_id": "7551348540727905342",
                "title": "开局剑落南海",
                "author": "索尔配音",
                "thumb_url": "https://p6-tt.bytecdn.cn/novel-pic/abc",
            }
        ],
    },
}


@pytest.mark.asyncio
async def test_explicit_search_uses_the_app_api_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an identity available, search needs neither login nor a browser."""
    searcher = _make_searcher(explicitly_selected=True)
    fake_device = type("D", (), {"device_id": "123", "install_id": "456"})()
    monkeypatch.setattr(FanqienovelSearcher, "_app_device", lambda self: fake_device)

    async def _device(self: Any) -> Any:
        return fake_device

    monkeypatch.setattr(FanqienovelSearcher, "_ensure_device", _device)
    calls: list[str] = []

    async def _fake_json(
        self: FanqienovelSearcher, url: str, headers: dict[str, str]
    ) -> Any:
        calls.append(url)
        assert headers["x-argus"].startswith("8o")
        assert headers["x-ladon"]
        return _APP_PAYLOAD

    def _unexpected(*args: Any, **kwargs: Any) -> str | None:
        raise AssertionError("the browser must not be used when the app API answers")

    monkeypatch.setattr(FanqienovelSearcher, "_get_json", _fake_json)
    monkeypatch.setattr(browser, "search_via_browser", _unexpected)

    results = await searcher.search("剑来")

    assert calls and "novel.snssdk.com" in calls[0]
    assert [r["title"] for r in results] == ["开局剑落南海"]
    assert results[0]["book_id"] == "7551348540727905342"
    assert results[0]["cover_url"] == "https://p6-tt.bytecdn.cn/novel-pic/abc"


_RANK_STATE = {
    "rank": {
        "serverRendered": True,
        "book_list": [
            {
                "bookId": "7231424472606051384",
                "bookName": "惹\u3000枝",
                "author": "空留",
                "thumbUri": "//p3.example.com/cover.jpg",
                "wordNumber": 1163967,
                "lastChapterTitle": "第550章 终（完结）",
            },
            {"bookId": "", "bookName": "missing id"},
        ],
    }
}


@pytest.mark.asyncio
async def test_rank_list_maps_the_server_rendered_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searcher = _make_searcher()
    page = (
        "<html><script>window.__INITIAL_STATE__ = "
        + json.dumps(_RANK_STATE, ensure_ascii=False)
        + ";</script></html>"
    )
    searcher.session = _FakeSession(page)  # type: ignore[assignment]

    async def _no_repair(
        self: FanqienovelSearcher, results: list[SearchResult]
    ) -> None:
        for item in results:
            item["title"] = "真实书名"

    monkeypatch.setattr(FanqienovelSearcher, "_fill_book_metadata", _no_repair)

    results = await searcher.rank(5)

    assert len(results) == 1
    assert results[0]["book_id"] == "7231424472606051384"
    assert results[0]["title"] == "真实书名"
    assert results[0]["author"] == "空留"
    assert results[0]["cover_url"] == "https://p3.example.com/cover.jpg"
    assert results[0]["word_count"] == "1163967"
    assert results[0]["latest_chapter"] == "第550章 终（完结）"


@pytest.mark.asyncio
async def test_rank_list_returns_empty_on_a_broken_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searcher = _make_searcher()
    searcher.session = _FakeSession("<html>no state</html>")  # type: ignore[assignment]

    assert await searcher.rank(5) == []


_CATEGORY_STATE = {
    "rank": {
        "rankCategoryTypeList": {
            "1": [
                {"id": 1141, "name": "西方奇幻"},
                {"id": 1140, "name": "东方仙侠"},
            ],
            "2": [{"id": 8, "name": "现代言情"}],
            "broken": "not-a-list",
        }
    }
}

_LIBRARY_PAYLOAD = {
    "data": {
        "book_list": [
            {
                "book_id": "7617740782291651646",
                "book_name": "骇夜：我在恐怖游戏里暴富",
                "author": "阿一不是阿二",
                "thumb_url": "http://p3.example.com/cover.jpg",
                "last_chapter_time": 1794002246,
            },
            {"book_id": "", "book_name": "missing id"},
        ]
    }
}


class _ParamGet:
    def __init__(self, text: str, params: dict[str, str]) -> None:
        self._text = text
        self.params = params

    async def __aenter__(self) -> _FakeResponse:
        return _FakeResponse(self._text)

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _ParamSession:
    """Session double that records the query parameters of each call."""

    headers = {"User-Agent": "test-agent"}

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> _ParamGet:
        self.calls.append((url, params or {}))
        return _ParamGet(self._text, params or {})


@pytest.mark.asyncio
async def test_categories_parses_the_rank_page() -> None:
    searcher = _make_searcher()
    page = (
        "<html><script>window.__INITIAL_STATE__ = "
        + json.dumps(_CATEGORY_STATE, ensure_ascii=False)
        + ";</script></html>"
    )
    searcher.session = _ParamSession(page)  # type: ignore[assignment]

    groups = await searcher.categories()

    assert groups == {
        "1": [
            {"id": "1141", "name": "西方奇幻"},
            {"id": "1140", "name": "东方仙侠"},
        ],
        "2": [{"id": "8", "name": "现代言情"}],
    }


@pytest.mark.asyncio
async def test_categories_returns_empty_on_a_broken_page() -> None:
    searcher = _make_searcher()
    searcher.session = _ParamSession("<html>no state</html>")  # type: ignore[assignment]

    assert await searcher.categories() == {}


@pytest.mark.asyncio
async def test_category_books_sends_the_required_library_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searcher = _make_searcher()
    session = _ParamSession(json.dumps(_LIBRARY_PAYLOAD, ensure_ascii=False))
    searcher.session = session  # type: ignore[assignment]

    async def _no_repair(
        self: FanqienovelSearcher, results: list[SearchResult]
    ) -> None:
        return None

    monkeypatch.setattr(FanqienovelSearcher, "_fill_book_metadata", _no_repair)

    results = await searcher.category_books("1141", gender="1", limit=20)

    assert len(results) == 1
    item = results[0]
    assert item["book_id"] == "7617740782291651646"
    assert item["title"] == "骇夜：我在恐怖游戏里暴富"
    assert item["author"] == "阿一不是阿二"
    assert item["cover_url"] == "https://p3.example.com/cover.jpg"
    assert item["update_date"] != ""

    url, params = session.calls[0]
    assert url == FanqienovelSearcher.LIBRARY_URL
    assert params["category_id"] == "1141"
    assert params["gender"] == "1"
    assert params["word_count"] == "0"
    assert params["sort"] == "0"
    assert params["offset"] == "0"
    assert params["limit"] == "20"


@pytest.mark.asyncio
async def test_category_books_tolerates_garbage() -> None:
    searcher = _make_searcher()
    searcher.session = _ParamSession("<html>not json</html>")  # type: ignore[assignment]

    assert await searcher.category_books("1") == []
