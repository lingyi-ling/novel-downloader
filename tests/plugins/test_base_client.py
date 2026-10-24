#!/usr/bin/env python3
"""Offline robustness tests for the base client and download pipeline.

The registry is monkeypatched with in-memory fetcher/parser doubles, so no
network or site plugin is involved.  The persistence assertions focus on what
is (and is not) left on disk after failures.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.infra.persistence.chapter_storage import ChapterStorage
from novel_downloader.plugins.common.client import CommonClient
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import (
    BookConfig,
    BookInfoDict,
    ChapterDict,
    ClientConfig,
    FetcherConfig,
    ParserConfig,
)

SITE = "demo"


class FakeFetcher:
    def __init__(self, config: Any = None) -> None:
        self.config = config
        self.is_logged_in = False
        self.init_calls = 0
        self.close_calls = 0
        self.save_state_calls = 0
        self.load_state_calls = 0
        self.login_calls: list[dict[str, Any]] = []
        self.login_result = True
        self.load_result = True
        self.fail_init = False
        self.fail_close = False
        self.fail_save_state = False
        self.book_info_calls = 0
        self.chapter_calls = 0
        self.book_info_pages = ["<html>info</html>"]
        self.chapter_pages = ["<html>chapter</html>"]
        self.book_info_errors: list[BaseException] = []
        self.chapter_errors: list[BaseException] = []
        self.media_calls: list[list[Any]] = []
        self.image_calls: list[str] = []
        self.images_calls: list[list[str]] = []

    async def init(self) -> None:
        self.init_calls += 1
        if self.fail_init:
            raise RuntimeError("fetcher init failed")

    async def close(self) -> None:
        self.close_calls += 1
        if self.fail_close:
            raise RuntimeError("fetcher close failed")

    async def login(
        self,
        username: str = "",
        password: str = "",
        cookies: dict[str, str] | None = None,
        attempt: int = 1,
        **kwargs: Any,
    ) -> bool:
        self.login_calls.append({"cookies": cookies, **kwargs})
        return self.login_result

    async def load_state(self) -> bool:
        self.load_state_calls += 1
        return self.load_result

    async def save_state(self) -> bool:
        self.save_state_calls += 1
        if self.fail_save_state:
            raise RuntimeError("cannot save state")
        return True

    @property
    def login_fields(self) -> list[Any]:
        return []

    async def fetch_book_info(self, book_id: str, **kwargs: Any) -> list[str]:
        self.book_info_calls += 1
        if self.book_info_errors:
            raise self.book_info_errors.pop(0)
        return list(self.book_info_pages)

    async def fetch_chapter_content(
        self, book_id: str, chapter_id: str, **kwargs: Any
    ) -> list[str]:
        self.chapter_calls += 1
        if self.chapter_errors:
            raise self.chapter_errors.pop(0)
        return list(self.chapter_pages)

    async def fetch_data(self, url: str, **kwargs: Any) -> bytes | None:
        return b"data"

    async def fetch_image(
        self,
        url: str,
        img_dir: Path,
        *,
        name: str | None = None,
        on_exist: str = "skip",
    ) -> Path | None:
        self.image_calls.append(url)
        return None

    async def fetch_images(
        self,
        img_dir: Path,
        urls: list[str],
        *,
        on_exist: str = "skip",
        concurrent: int = 5,
    ) -> None:
        self.images_calls.append(list(urls))

    async def fetch_fonts(
        self,
        font_dir: Path,
        urls: list[str],
        *,
        on_exist: str = "skip",
        concurrent: int = 5,
    ) -> None:
        return None

    async def fetch_media(
        self,
        media_dir: Path,
        resources: list[Any],
        *,
        on_exist: str = "skip",
        concurrent: int = 5,
    ) -> None:
        self.media_calls.append(list(resources))


class FakeParser:
    def __init__(self) -> None:
        self.book_info_calls = 0
        self.chapter_calls = 0
        self.book_info_result: BookInfoDict | None = _book_info()
        self.chapter_result: ChapterDict | None = _chapter()
        self.book_info_error: BaseException | None = None
        self.chapter_error: BaseException | None = None

    def parse_book_info(
        self, raw_pages: list[str], **kwargs: Any
    ) -> BookInfoDict | None:
        self.book_info_calls += 1
        if self.book_info_error:
            raise self.book_info_error
        return copy.deepcopy(self.book_info_result)

    def parse_chapter_content(
        self, raw_pages: list[str], chapter_id: str, **kwargs: Any
    ) -> ChapterDict | None:
        self.chapter_calls += 1
        if self.chapter_error:
            raise self.chapter_error
        return copy.deepcopy(self.chapter_result)


def _book_info(chapters: list[dict[str, Any]] | None = None) -> BookInfoDict:
    return {
        "book_name": "Book",
        "author": "Author",
        "cover_url": "",
        "update_time": "",
        "serial_status": "",
        "summary": "",
        "tags": [],
        "volumes": [
            {
                "volume_name": "V",
                "chapters": chapters
                if chapters is not None
                else [{"title": "C1", "url": "u1", "chapterId": "c1"}],
            }
        ],
        "extra": {},
    }


def _chapter(chapter_id: str = "c1") -> ChapterDict:
    return {"id": chapter_id, "title": "title", "content": "body", "extra": {}}


@pytest.fixture()
def harness(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    fetchers: list[FakeFetcher] = []
    parser = FakeParser()

    def _get_fetcher(site: str, cfg: FetcherConfig) -> FakeFetcher:
        fetcher = FakeFetcher(cfg)
        fetchers.append(fetcher)
        return fetcher

    def _get_parser(site: str, cfg: ParserConfig) -> FakeParser:
        return parser

    monkeypatch.setattr(registrar, "get_fetcher", _get_fetcher)
    monkeypatch.setattr(registrar, "get_parser", _get_parser)
    return SimpleNamespace(fetchers=fetchers, parser=parser)


def _client(tmp_path: Path, **overrides: Any) -> CommonClient:
    kwargs: dict[str, Any] = {
        "raw_data_dir": str(tmp_path / "raw"),
        "cache_dir": str(tmp_path / "cache"),
        "output_dir": str(tmp_path / "out"),
        "request_interval": 0.0,
        "backoff_factor": 0.0,
        "retry_times": 0,
    }
    kwargs.update(overrides)
    return CommonClient(SITE, ClientConfig(**kwargs))


def _book_dir(tmp_path: Path, book_id: str = "b1") -> Path:
    return tmp_path / "raw" / SITE / book_id


async def _initialised(
    tmp_path: Path, harness: SimpleNamespace, **overrides: Any
) -> tuple[CommonClient, FakeFetcher, FakeParser]:
    client = _client(tmp_path, **overrides)
    await client.init(FetcherConfig(), ParserConfig())
    return client, harness.fetchers[-1], harness.parser


# --------------------------------------------------------------------------- #
# client lifecycle
# --------------------------------------------------------------------------- #
def test_properties_raise_before_init(tmp_path: Path) -> None:
    client = _client(tmp_path)

    with pytest.raises(RuntimeError, match="Fetcher is not initialized"):
        _ = client.fetcher
    with pytest.raises(RuntimeError, match="Parser is not initialized"):
        _ = client.parser


@pytest.mark.asyncio
async def test_init_builds_and_initialises_both_components(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher, parser = await _initialised(tmp_path, harness)

    assert client.fetcher is fetcher
    assert client.parser is parser
    assert fetcher.init_calls == 1


@pytest.mark.asyncio
async def test_reinit_closes_the_previous_fetcher(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client = _client(tmp_path)
    await client.init(FetcherConfig(), ParserConfig())
    first = harness.fetchers[0]

    await client.init(FetcherConfig(), ParserConfig())

    assert first.close_calls == 1
    assert client.fetcher is harness.fetchers[1]
    assert harness.fetchers[1].init_calls == 1


@pytest.mark.asyncio
async def test_client_can_be_reinitialised_after_close(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, first, _ = await _initialised(tmp_path, harness)
    await client.close()

    await client.init(FetcherConfig(), ParserConfig())

    assert first.close_calls == 1
    assert client.fetcher is harness.fetchers[1]
    assert harness.fetchers[1].init_calls == 1


@pytest.mark.asyncio
async def test_concurrent_close_calls_only_close_once(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher, _ = await _initialised(tmp_path, harness)

    await asyncio.gather(client.close(), client.close())

    assert fetcher.close_calls == 1


@pytest.mark.asyncio
async def test_close_saves_state_when_logged_in(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher, _ = await _initialised(tmp_path, harness)
    fetcher.is_logged_in = True

    await client.close()

    assert fetcher.save_state_calls == 1
    assert fetcher.close_calls == 1
    assert client._fetcher is None
    assert client._parser is None


@pytest.mark.asyncio
async def test_close_still_closes_session_when_saving_state_fails(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher, _ = await _initialised(tmp_path, harness)
    fetcher.is_logged_in = True
    fetcher.fail_save_state = True

    with pytest.raises(RuntimeError, match="cannot save state"):
        await client.close()

    assert fetcher.close_calls == 1
    # The client is detached even though close raised.
    assert client._fetcher is None
    assert client._parser is None


@pytest.mark.asyncio
async def test_close_is_idempotent(tmp_path: Path, harness: SimpleNamespace) -> None:
    client, fetcher, _ = await _initialised(tmp_path, harness)

    await client.close()
    await client.close()

    assert fetcher.close_calls == 1  # second call is a no-op on the client


@pytest.mark.asyncio
async def test_init_releases_fetcher_when_parser_lookup_fails(
    tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        registrar,
        "get_parser",
        lambda site, cfg: (_ for _ in ()).throw(ValueError("no parser")),
    )
    client = _client(tmp_path)

    with pytest.raises(ValueError, match="no parser"):
        await client.init(FetcherConfig(), ParserConfig())

    assert harness.fetchers[0].close_calls == 1
    assert client._fetcher is None
    assert client._parser is None


@pytest.mark.asyncio
async def test_init_releases_fetcher_when_init_fails(
    tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _get_fetcher(site: str, cfg: FetcherConfig) -> FakeFetcher:
        fetcher = FakeFetcher(cfg)
        fetcher.fail_init = True
        harness.fetchers.append(fetcher)
        return fetcher

    monkeypatch.setattr(registrar, "get_fetcher", _get_fetcher)
    client = _client(tmp_path)

    with pytest.raises(RuntimeError, match="fetcher init failed"):
        await client.init(FetcherConfig(), ParserConfig())

    assert harness.fetchers[0].close_calls == 1
    assert client._fetcher is None
    assert client._parser is None


@pytest.mark.asyncio
async def test_async_context_manager_initialises_and_closes(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client = _client(tmp_path)

    async with client:
        fetcher = harness.fetchers[0]
        assert client.fetcher is fetcher

    assert fetcher.close_calls == 1
    assert client._fetcher is None


# --------------------------------------------------------------------------- #
# metadata persistence
# --------------------------------------------------------------------------- #
def test_book_info_roundtrip(tmp_path: Path) -> None:
    client = _client(tmp_path)
    info = _book_info()

    client._save_book_info("b1", info)
    assert client._load_book_info("b1") == info

    client._save_book_info("b1", info, stage="cleaner")
    assert (_book_dir(tmp_path) / "book_info.cleaner.json").is_file()
    assert client._load_book_info("b1", stage="cleaner") == info


def test_load_book_info_missing_raises_file_not_found(tmp_path: Path) -> None:
    client = _client(tmp_path)

    with pytest.raises(FileNotFoundError, match="Missing metadata file"):
        client._load_book_info("unknown")


def test_load_book_info_corrupt_json_raises_value_error(tmp_path: Path) -> None:
    client = _client(tmp_path)
    book_dir = _book_dir(tmp_path)
    book_dir.mkdir(parents=True)
    (book_dir / "book_info.raw.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match="Corrupt JSON"):
        client._load_book_info("b1")


def test_load_book_info_non_object_raises_value_error(tmp_path: Path) -> None:
    client = _client(tmp_path)
    book_dir = _book_dir(tmp_path)
    book_dir.mkdir(parents=True)
    (book_dir / "book_info.raw.json").write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="expected an object"):
        client._load_book_info("b1")


def test_load_book_info_invalid_utf8_raises_value_error(tmp_path: Path) -> None:
    client = _client(tmp_path)
    book_dir = _book_dir(tmp_path)
    book_dir.mkdir(parents=True)
    (book_dir / "book_info.raw.json").write_bytes(b"\xff\xfe\x00")

    with pytest.raises(ValueError, match="Cannot decode"):
        client._load_book_info("b1")


def test_save_book_info_failure_creates_no_directory(tmp_path: Path) -> None:
    client = _client(tmp_path)
    info = _book_info()
    info["extra"] = {"bad": {1, 2}}  # type: ignore[dict-item]  # not JSON serialisable

    with pytest.raises(TypeError):
        client._save_book_info("b1", info)

    # Serialisation happens before the directory is touched.
    assert not _book_dir(tmp_path).exists()


def test_pipeline_meta_roundtrip_and_defaults(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client._load_pipeline_meta("b1") == {"pipeline": [], "executed": {}}

    meta = {"pipeline": ["cleaner"], "executed": {"cleaner": {"x": 1}}}
    client._save_pipeline_meta("b1", meta)  # type: ignore[arg-type]
    assert client._load_pipeline_meta("b1") == meta


def test_pipeline_meta_corrupt_or_wrong_shape_falls_back(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    book_dir = _book_dir(tmp_path)
    book_dir.mkdir(parents=True)
    meta_path = book_dir / "pipeline.json"
    meta_path.write_text("{bad", encoding="utf-8")
    assert client._load_pipeline_meta("b1") == {"pipeline": [], "executed": {}}

    meta_path.write_text(
        json.dumps({"pipeline": "cleaner", "executed": ["x"]}), encoding="utf-8"
    )
    assert client._load_pipeline_meta("b1") == {"pipeline": [], "executed": {}}


# --------------------------------------------------------------------------- #
# helper selection logic
# --------------------------------------------------------------------------- #
def test_extract_chapter_ids_honours_range_and_ignore(tmp_path: Path) -> None:
    client = _client(tmp_path)
    vols = _book_info(
        [
            {"chapterId": "1", "title": "a"},
            {"chapterId": "2", "title": "b"},
            {"chapterId": "3", "title": "c"},
            {"title": "missing id"},
        ]
    )["volumes"]

    assert client._extract_chapter_ids(vols, None, None, frozenset()) == [
        "1",
        "2",
        "3",
    ]
    assert client._extract_chapter_ids(vols, "2", None, frozenset()) == ["2", "3"]
    assert client._extract_chapter_ids(vols, None, "2", frozenset()) == ["1", "2"]
    assert client._extract_chapter_ids(vols, None, None, frozenset({"2"})) == [
        "1",
        "3",
    ]
    assert client._extract_chapter_ids(vols, "9", None, frozenset()) == []


def test_extract_chapter_ids_skips_inaccessible_unless_configured(
    tmp_path: Path,
) -> None:
    vols = _book_info(
        [
            {"chapterId": "1", "accessible": False},
            {"chapterId": "2", "accessible": True},
        ]
    )["volumes"]

    normal = _client(tmp_path)
    permissive = _client(tmp_path, fetch_inaccessible=True)

    assert normal._extract_chapter_ids(vols, None, None, frozenset()) == ["2"]
    assert permissive._extract_chapter_ids(vols, None, None, frozenset()) == [
        "1",
        "2",
    ]


def test_filter_volumes_keeps_only_the_selected_range(tmp_path: Path) -> None:
    client = _client(tmp_path)
    vols = _book_info(
        [
            {"chapterId": "1", "title": "a"},
            {"chapterId": "2", "title": "b"},
            {"chapterId": "3", "title": "c"},
        ]
    )["volumes"]

    filtered = client._filter_volumes(vols, "2", "3", frozenset())

    assert [c["chapterId"] for c in filtered[0]["chapters"]] == ["2", "3"]
    assert filtered[0]["volume_name"] == "V"


def test_detect_latest_stage_uses_the_pipeline_list(tmp_path: Path) -> None:
    client = _client(tmp_path)
    book_dir = _book_dir(tmp_path)
    book_dir.mkdir(parents=True)
    assert client._detect_latest_stage("b1") == "raw"

    (book_dir / "book_info.cleaner.json").write_text("{}", encoding="utf-8")
    (book_dir / "chapter.cleaner.sqlite").write_bytes(b"")
    # ``executed`` alone is not considered; only ``pipeline`` is walked.
    client._save_pipeline_meta(
        "b1",
        {"pipeline": [], "executed": {"cleaner": {"x": 1}}},  # type: ignore[arg-type]
    )
    assert client._detect_latest_stage("b1") == "raw"

    client._save_pipeline_meta(
        "b1",
        {"pipeline": ["cleaner"], "executed": {}},  # type: ignore[arg-type]
    )
    assert client._detect_latest_stage("b1") == "cleaner"


# --------------------------------------------------------------------------- #
# get_book_info(): caching and failure behaviour
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_book_info_saves_and_caches(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher, parser = await _initialised(tmp_path, harness)

    info = await client.get_book_info("b1")

    assert info["book_name"] == "Book"
    assert "last_checked" in info
    assert (_book_dir(tmp_path) / "book_info.raw.json").is_file()
    assert fetcher.book_info_calls == 1

    again = await client.get_book_info("b1")

    assert again["book_name"] == "Book"
    assert fetcher.book_info_calls == 1  # served from the on-disk cache
    assert parser.book_info_calls == 1


@pytest.mark.asyncio
async def test_get_book_info_ignores_cache_when_disabled(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher, _ = await _initialised(tmp_path, harness, cache_book_info=False)

    await client.get_book_info("b1")
    await client.get_book_info("b1")

    assert fetcher.book_info_calls == 2


@pytest.mark.asyncio
async def test_get_book_info_recovers_from_a_corrupt_cache(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher, _ = await _initialised(tmp_path, harness)
    book_dir = _book_dir(tmp_path)
    book_dir.mkdir(parents=True)
    (book_dir / "book_info.raw.json").write_text("{bad", encoding="utf-8")

    info = await client.get_book_info("b1")

    assert info["book_name"] == "Book"
    assert fetcher.book_info_calls == 1
    # The corrupt file is replaced by a valid one.
    assert json.loads((book_dir / "book_info.raw.json").read_text("utf-8"))


@pytest.mark.asyncio
async def test_get_book_info_fetch_failure_raises_lookup_error(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher, _ = await _initialised(tmp_path, harness)
    fetcher.book_info_errors.append(RuntimeError("network down"))

    with pytest.raises(LookupError, match="Unable to load book_info"):
        await client.get_book_info("b1")

    assert not (_book_dir(tmp_path) / "book_info.raw.json").exists()


@pytest.mark.asyncio
async def test_get_book_info_parse_failure_raises_lookup_error(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, _, parser = await _initialised(tmp_path, harness)
    parser.book_info_result = None

    with pytest.raises(LookupError, match="Unable to load book_info"):
        await client.get_book_info("b1")

    assert not (_book_dir(tmp_path) / "book_info.raw.json").exists()


# --------------------------------------------------------------------------- #
# get_chapter(): retry boundary
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_chapter_success_first_attempt(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher, _ = await _initialised(tmp_path, harness)

    chap = await client.get_chapter("b1", "c1")

    assert chap is not None and chap["id"] == "c1"
    assert fetcher.chapter_calls == 1


@pytest.mark.asyncio
async def test_get_chapter_retries_then_succeeds(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher, _ = await _initialised(tmp_path, harness, retry_times=2)
    fetcher.chapter_errors.append(RuntimeError("transient"))

    chap = await client.get_chapter("b1", "c1")

    assert chap is not None
    assert fetcher.chapter_calls == 2


@pytest.mark.asyncio
async def test_get_chapter_exhausts_the_configured_retries(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher, _ = await _initialised(tmp_path, harness, retry_times=2)
    fetcher.chapter_errors.extend(RuntimeError("down") for _ in range(10))

    assert await client.get_chapter("b1", "c1") is None
    assert fetcher.chapter_calls == 3  # retry_times + 1


@pytest.mark.asyncio
async def test_get_chapter_zero_retries_makes_one_attempt(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher, _ = await _initialised(tmp_path, harness, retry_times=0)
    fetcher.chapter_errors.extend(RuntimeError("down") for _ in range(3))

    assert await client.get_chapter("b1", "c1") is None
    assert fetcher.chapter_calls == 1


@pytest.mark.asyncio
async def test_get_chapter_retries_empty_parse_results(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher, parser = await _initialised(tmp_path, harness, retry_times=2)
    parser.chapter_result = None

    assert await client.get_chapter("b1", "c1") is None
    assert fetcher.chapter_calls == 3


@pytest.mark.asyncio
async def test_get_chapter_restricted_page_stops_without_retry(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    class _RestrictedClient(CommonClient):
        def _dl_check_restricted(self, raw_pages: list[str]) -> bool:
            return True

    client = _RestrictedClient(
        SITE,
        ClientConfig(
            raw_data_dir=str(tmp_path / "raw"),
            retry_times=3,
            request_interval=0.0,
            backoff_factor=0.0,
        ),
    )
    await client.init(FetcherConfig(), ParserConfig())
    fetcher = harness.fetchers[-1]

    assert await client.get_chapter("b1", "c1") is None
    assert fetcher.chapter_calls == 1


@pytest.mark.asyncio
async def test_get_chapter_empty_check_stops_without_retry(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    class _EmptyClient(CommonClient):
        def _dl_check_empty(self, raw_pages: list[str]) -> bool:
            return True

    client = _EmptyClient(
        SITE,
        ClientConfig(
            raw_data_dir=str(tmp_path / "raw"),
            retry_times=3,
            request_interval=0.0,
            backoff_factor=0.0,
        ),
    )
    await client.init(FetcherConfig(), ParserConfig())
    parser = harness.parser
    parser.chapter_result = None
    fetcher = harness.fetchers[-1]

    assert await client.get_chapter("b1", "c1") is None
    assert fetcher.chapter_calls == 1


@pytest.mark.asyncio
async def test_get_chapter_caches_referenced_media(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher, parser = await _initialised(tmp_path, harness)
    parser.chapter_result = _chapter()
    parser.chapter_result["extra"] = {
        "resources": [{"url": "https://img/a.png", "type": "image"}]
    }

    await client.get_chapter("b1", "c1")

    assert fetcher.media_calls == [[{"url": "https://img/a.png", "type": "image"}]]


# --------------------------------------------------------------------------- #
# download entry points: failures do not leave databases behind
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_download_book_metadata_failure_leaves_no_database(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher, _ = await _initialised(tmp_path, harness)
    fetcher.book_info_errors.append(RuntimeError("network down"))

    with pytest.raises(LookupError):
        await client.download_book(BookConfig(book_id="b1"))

    book_dir = _book_dir(tmp_path)
    # Note: DownloadMixin creates the book directory before fetching metadata,
    # so an empty directory may remain.  No metadata, DB or media is written.
    assert not (book_dir / "chapter.raw.sqlite").exists()
    assert not (book_dir / "book_info.raw.json").exists()


@pytest.mark.asyncio
async def test_download_book_persists_metadata_and_chapters(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, _, _ = await _initialised(tmp_path, harness)

    await client.download_book(BookConfig(book_id="b1"))

    book_dir = _book_dir(tmp_path)
    assert (book_dir / "book_info.raw.json").is_file()
    with ChapterStorage(book_dir, "chapter.raw.sqlite") as storage:
        assert storage.exists("c1")


@pytest.mark.asyncio
async def test_download_chapter_failure_leaves_no_database(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher, _ = await _initialised(tmp_path, harness)
    fetcher.chapter_errors.extend(RuntimeError("down") for _ in range(3))

    await client.download_chapter("b1", "c1")

    # Same as above: an empty book directory may remain, but no DB is created.
    assert not (_book_dir(tmp_path) / "chapter.raw.sqlite").exists()


@pytest.mark.asyncio
async def test_download_chapter_success_writes_database(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, _, _ = await _initialised(tmp_path, harness)

    await client.download_chapter("b1", "c1")

    with ChapterStorage(_book_dir(tmp_path), "chapter.raw.sqlite") as storage:
        assert storage.exists("c1")


@pytest.mark.asyncio
async def test_save_html_failure_does_not_affect_raw_data(
    tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _ = await _initialised(tmp_path, harness, save_html=True)
    # Raw page dumps go to ./debug/<site>, not to raw_data; keep the test
    # self-contained by redirecting the debug directory.
    debug_dir = tmp_path / "debug" / SITE
    client._debug_dir = debug_dir

    client._save_raw_pages("b1", "c1", ["<html>"])

    assert (debug_dir / "html" / "b1_c1_0.html").read_text(encoding="utf-8") == (
        "<html>"
    )
