#!/usr/bin/env python3
"""
Offline tests for the translator performance layer:
:mod:`...translator.cache` plus the cache/dedupe integration in the Google,
Edge and Youdao processors.

Every HTTP call is replaced by an in-memory stub and ``time.sleep`` is a no-op,
so the suite stays deterministic and offline. The tests pin the behaviour the
optimisation promises:

* a cache hit performs no request, failures are never cached, TTL/LRU work;
* repeated paragraphs are translated once per batch;
* cache access is thread safe and concurrent identical calls are coalesced;
* outputs are unchanged compared with the uncached processors.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any

import pytest
import requests

from novel_downloader.plugins.processors.translator import youdao as youdao_module
from novel_downloader.plugins.processors.translator.cache import TranslationCache
from novel_downloader.plugins.processors.translator.edge import (
    EdgeTranslaterProcessor,
)
from novel_downloader.plugins.processors.translator.google import (
    GoogleTranslaterProcessor,
)
from novel_downloader.plugins.processors.translator.youdao import (
    YoudaoTranslaterProcessor,
)
from tests.plugins._processor_test_utils import make_chapter

FAR_FUTURE = datetime(2100, 1, 1)


# --------------------------------------------------------------------------- #
# offline HTTP / sleep stubs
# --------------------------------------------------------------------------- #
class FakeResponse:
    """Minimal ``requests.Response`` look-alike."""

    def __init__(
        self,
        status_code: int = 200,
        payload: Any = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class HttpStub:
    """Records requests and serves queued / fixed responses."""

    def __init__(self) -> None:
        self.get_calls: list[tuple[str, dict[str, Any]]] = []
        self.post_calls: list[tuple[str, dict[str, Any]]] = []
        self.get_response: Any = FakeResponse(200, text="")
        self.post_response: Any = FakeResponse(200, {})
        self.get_raises: Exception | None = None
        self.post_raises: Exception | None = None
        self.post_queue: list[Any] = []

    def get(self, url: str, **kwargs: Any) -> Any:
        self.get_calls.append((url, kwargs))
        if self.get_raises is not None:
            raise self.get_raises
        return self.get_response

    def post(self, url: str, **kwargs: Any) -> Any:
        self.post_calls.append((url, kwargs))
        if self.post_raises is not None:
            raise self.post_raises
        if self.post_queue:
            return self.post_queue.pop(0)
        return self.post_response

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(requests, "get", self.get)
        monkeypatch.setattr(requests, "post", self.post)


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> HttpStub:
    stub = HttpStub()
    stub.install(monkeypatch)
    return stub


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never block on the processors' politeness delay."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


def sentences_response(*parts: str) -> FakeResponse:
    return FakeResponse(200, {"sentences": [{"trans": part} for part in parts]})


def edge_response(text: str) -> FakeResponse:
    return FakeResponse(200, [{"translations": [{"text": text}]}])


def prime_edge_token(proc: EdgeTranslaterProcessor) -> None:
    proc._token_cache = {"token": "cached-token", "expire": FAR_FUTURE}


class CountingClient:
    """Stand-in for ``_YoudaoWebFanyi`` that records translated strings."""

    def __init__(self, result: str = "T", error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []

    def translate(self, text: str, src: str = "auto", tgt: str = "zh-CHS") -> str:
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        return self.result


def cache_key(cache: TranslationCache, text: str) -> str:
    key = cache.make_key(text)
    assert key is not None
    return key


# --------------------------------------------------------------------------- #
# chunking boundaries
# --------------------------------------------------------------------------- #
class TestSplitTextBoundaries:
    def test_split_never_cuts_a_paragraph_or_sentence(self) -> None:
        # Each paragraph is longer than the chunk limit: it must stay intact.
        first = "第一段。" * 80
        second = "第二段！" * 80
        third = "short paragraph"
        text = f"{first}\n\n  {second}  \r\n{third}"
        lines = [line.strip() for line in text.splitlines() if line.strip()]

        chunks = EdgeTranslaterProcessor._split_text(text, max_length=100)

        rebuilt = [line for chunk in chunks for line in chunk.split("\n")]
        assert rebuilt == lines
        assert chunks[0] == first
        assert chunks[1] == second
        assert chunks[2] == third

    def test_exact_fill_boundary_keeps_lines_together(self) -> None:
        # "aaaa\nbbbb" is exactly 9 characters.
        text = "aaaa\nbbbb\ncccc"

        assert EdgeTranslaterProcessor._split_text(text, max_length=9) == [
            "aaaa\nbbbb",
            "cccc",
        ]
        assert EdgeTranslaterProcessor._split_text(text, max_length=8) == [
            "aaaa",
            "bbbb",
            "cccc",
        ]

    def test_blank_and_whitespace_only_lines_are_dropped(self) -> None:
        text = "\n \n\t\nfirst\n\n   \nsecond\n\n"

        assert EdgeTranslaterProcessor._split_text(text, max_length=100) == [
            "first\nsecond"
        ]

    def test_crlf_input_is_normalized_not_split_mid_line(self) -> None:
        assert EdgeTranslaterProcessor._split_text("a\r\nb\r\nc", max_length=100) == [
            "a\nb\nc"
        ]

    def test_edge_and_youdao_share_the_same_chunking(self) -> None:
        text = "\n".join(["x" * 700, "y" * 700, "z" * 700, "w" * 10])

        assert EdgeTranslaterProcessor._split_text(text, max_length=1500) == (
            YoudaoTranslaterProcessor._split_text(text, max_length=1500)
        )


# --------------------------------------------------------------------------- #
# cache unit tests
# --------------------------------------------------------------------------- #
class TestTranslationCache:
    def test_key_is_scoped_by_engine_source_and_target(self) -> None:
        google = TranslationCache(engine="google", source="auto", target="zh-CN")
        edge = TranslationCache(engine="edge", source="auto", target="zh-CN")
        other_target = TranslationCache(engine="google", source="auto", target="zh-TW")
        other_source = TranslationCache(engine="google", source="en", target="zh-CN")

        key = cache_key(google, "hello")

        assert "hello" not in key  # only the digest is stored in the key
        assert len(key.rsplit("|", 1)[1]) == 64
        assert key != cache_key(edge, "hello")
        assert key != cache_key(other_target, "hello")
        assert key != cache_key(other_source, "hello")

    def test_lru_evicts_the_least_recently_used_entry(self) -> None:
        cache = TranslationCache(
            engine="google", source="auto", target="zh-CN", maxsize=2
        )
        key_a = cache_key(cache, "a")
        key_b = cache_key(cache, "b")
        cache.put(key_a, "A")
        cache.put(key_b, "B")

        assert cache.get(key_a) == "A"  # ``a`` is now the most recent

        key_c = cache_key(cache, "c")
        cache.put(key_c, "C")

        assert cache.get(key_b) is None  # evicted
        assert cache.get(key_a) == "A"
        assert cache.stats["evictions"] == 1

    def test_ttl_zero_expires_immediately(self) -> None:
        cache = TranslationCache(engine="google", source="auto", target="zh-CN", ttl=0)
        key = cache_key(cache, "hello")
        cache.put(key, "你好")

        assert cache.get(key) is None
        stats = cache.stats
        assert stats["expired"] == 1
        assert stats["misses"] == 1
        assert stats["size"] == 0

    def test_clear_resets_entries_and_counters(self) -> None:
        cache = TranslationCache(engine="google", source="auto", target="zh-CN")
        key = cache_key(cache, "hello")
        cache.put(key, "你好")
        assert cache.get(key) == "你好"

        cache.clear()

        stats = cache.stats
        assert stats["size"] == 0
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert cache.get(key) is None
        assert cache.stats["misses"] == 1

    def test_max_text_length_is_checked_per_paragraph(self) -> None:
        cache = TranslationCache(
            engine="google",
            source="auto",
            target="zh-CN",
            max_text_length=10,
        )

        assert cache.make_key("x" * 10) is not None
        assert cache.make_key("x" * 11) is None
        assert cache.make_key("short\n" + "x" * 11) is None
        # Several short paragraphs add up to a long unit but are still cached.
        multi_paragraph = "\n".join(["y" * 10] * 5)
        assert cache.make_key(multi_paragraph) is not None

    def test_disabled_cache_never_stores_anything(self) -> None:
        cache = TranslationCache(
            engine="google", source="auto", target="zh-CN", maxsize=0
        )
        assert cache.make_key("hello") is None
        cache.put("ignored", "value")
        assert cache.stats["size"] == 0

    def test_concurrent_access_is_thread_safe(self) -> None:
        cache = TranslationCache(
            engine="google", source="auto", target="zh-CN", maxsize=8
        )
        errors: list[BaseException] = []

        def worker(worker_id: int) -> None:
            try:
                for step in range(80):
                    key = cache_key(cache, f"key-{(worker_id + step) % 40}")
                    cache.get(key)
                    if (worker_id + step) % 3 == 0:
                        cache.put(key, f"value-{worker_id}-{step}")
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        assert cache.stats["size"] <= 8

    def test_malformed_sizes_fall_back_to_defaults(
        self, http: HttpStub, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(
            "WARNING", logger="novel_downloader.plugins.processors.translator.cache"
        ):
            proc = GoogleTranslaterProcessor(
                {"cache_size": "many", "cache_max_text_length": "far"}
            )

        stats = proc.cache_stats
        assert stats["maxsize"] == 4096
        assert "Invalid cache_size" in caplog.text
        assert "Invalid cache_max_text_length" in caplog.text


# --------------------------------------------------------------------------- #
# Google
# --------------------------------------------------------------------------- #
class TestGoogleCache:
    def test_hit_skips_http_and_updates_counters(self, http: HttpStub) -> None:
        http.post_response = sentences_response("你好")
        proc = GoogleTranslaterProcessor({})
        chapter = make_chapter(title="hello", content="")

        first = proc.process_chapter(chapter)
        second = proc.process_chapter(chapter)

        assert first == second
        assert len(http.post_calls) == 1
        stats = proc.cache_stats
        assert stats == {
            "engine": "google",
            "source": "auto",
            "target": "zh-CN",
            "hits": 1,
            "misses": 1,
            "size": 1,
            "maxsize": 4096,
            "evictions": 0,
            "expired": 0,
            "ttl": None,
        }

    def test_cache_size_zero_disables_caching(self, http: HttpStub) -> None:
        http.post_response = sentences_response("你好")
        proc = GoogleTranslaterProcessor({"cache_size": 0})
        chapter = make_chapter(title="hello", content="")

        proc.process_chapter(chapter)
        proc.process_chapter(chapter)

        assert len(http.post_calls) == 2
        stats = proc.cache_stats
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["size"] == 0

    def test_http_failure_is_not_cached(self, http: HttpStub) -> None:
        http.post_response = FakeResponse(429, text="rate limited")
        proc = GoogleTranslaterProcessor({})
        chapter = make_chapter(title="hello", content="")

        proc.process_chapter(chapter)
        proc.process_chapter(chapter)

        assert len(http.post_calls) == 2
        stats = proc.cache_stats
        assert stats["hits"] == 0
        assert stats["misses"] == 2
        assert stats["size"] == 0

    def test_network_failure_is_not_cached(self, http: HttpStub) -> None:
        http.post_raises = requests.ConnectionError("boom")
        proc = GoogleTranslaterProcessor({})
        chapter = make_chapter(title="hello", content="")

        proc.process_chapter(chapter)
        proc.process_chapter(chapter)

        assert len(http.post_calls) == 2
        assert proc.cache_stats["size"] == 0

    def test_malformed_payload_is_not_cached(self, http: HttpStub) -> None:
        http.post_response = FakeResponse(200, {"unexpected": True})
        proc = GoogleTranslaterProcessor({})

        proc.process_chapter(make_chapter(title="hello", content=""))
        proc.process_chapter(make_chapter(title="hello", content=""))

        assert len(http.post_calls) == 2
        assert proc.cache_stats["size"] == 0

    def test_ttl_expiry_triggers_a_new_request(self, http: HttpStub) -> None:
        http.post_response = sentences_response("你好")
        proc = GoogleTranslaterProcessor({"cache_ttl": 0})
        chapter = make_chapter(title="hello", content="")

        proc.process_chapter(chapter)
        proc.process_chapter(chapter)

        assert len(http.post_calls) == 2
        stats = proc.cache_stats
        assert stats["expired"] == 1
        assert stats["misses"] == 2

    def test_long_text_bypasses_the_cache(self, http: HttpStub) -> None:
        http.post_response = sentences_response("T")
        proc = GoogleTranslaterProcessor({"cache_max_text_length": 1000})
        long_title = "x" * 1500

        proc.process_chapter(make_chapter(title=long_title, content=""))
        proc.process_chapter(make_chapter(title=long_title, content=""))

        assert len(http.post_calls) == 2
        stats = proc.cache_stats
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["size"] == 0

    def test_concurrent_identical_calls_issue_one_request(self, http: HttpStub) -> None:
        http.post_response = sentences_response("T")
        proc = GoogleTranslaterProcessor({})
        results: list[str] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                results.append(proc._translate("same text"))
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not errors
        assert results == ["T"] * 16
        assert len(http.post_calls) == 1
        stats = proc.cache_stats
        assert stats["misses"] == 1
        assert stats["hits"] == 15
        assert stats["hits"] + stats["misses"] == 16

    def test_output_matches_the_uncached_processor(self, http: HttpStub) -> None:
        http.post_response = sentences_response("译")
        chapter = make_chapter(title="hello", content="world")

        cached = GoogleTranslaterProcessor({}).process_chapter(chapter)
        uncached = GoogleTranslaterProcessor({"cache_size": 0}).process_chapter(chapter)

        assert cached == uncached


# --------------------------------------------------------------------------- #
# Edge dedupe / cache
# --------------------------------------------------------------------------- #
class TestEdgeDedupe:
    @staticmethod
    def _repeated_blocks(line: str, count: int) -> str:
        return "\n".join([line] * count)

    def test_duplicate_chunks_are_translated_once(self, http: HttpStub) -> None:
        http.post_response = edge_response("T")
        # Disable the LRU cache so the effect measured here is dedupe alone.
        proc = EdgeTranslaterProcessor({"cache_size": 0})
        prime_edge_token(proc)
        content = self._repeated_blocks("a" * 900, 10)

        chapter = proc.process_chapter(make_chapter(title="章", content=content))

        # 10 x 900 chars -> chunks of 3/3/3/1; the first three are identical.
        assert chapter["content"] == "T\nT\nT\nT"
        assert len(http.post_calls) == 3  # title + distinct chunk + tail chunk
        assert proc.cache_stats["size"] == 0

    def test_multi_paragraph_chunk_is_cached(self, http: HttpStub) -> None:
        http.post_response = edge_response("T")
        proc = EdgeTranslaterProcessor({})
        prime_edge_token(proc)
        chapter = make_chapter(
            title="hello", content=self._repeated_blocks("a" * 900, 3)
        )

        first = proc.process_chapter(chapter)
        second = proc.process_chapter(chapter)

        assert first["content"] == second["content"] == "T"
        # title + the (multi-paragraph, 2702-char) chunk, both cached afterwards
        assert len(http.post_calls) == 2
        stats = proc.cache_stats
        assert stats["hits"] == 2
        assert stats["misses"] == 2
        assert stats["size"] == 2

    def test_dedupe_can_be_disabled(self, http: HttpStub) -> None:
        http.post_response = edge_response("T")
        proc = EdgeTranslaterProcessor({"dedupe": False, "cache_size": 0})
        prime_edge_token(proc)
        content = self._repeated_blocks("a" * 900, 10)

        chapter = proc.process_chapter(make_chapter(title="章", content=content))

        assert chapter["content"] == "T\nT\nT\nT"
        assert len(http.post_calls) == 5  # title + four chunks

    def test_failed_translations_are_not_deduplicated(self, http: HttpStub) -> None:
        http.post_response = FakeResponse(500, text="down")
        proc = EdgeTranslaterProcessor({})
        prime_edge_token(proc)
        content = self._repeated_blocks("a" * 900, 10)

        chapter = proc.process_chapter(make_chapter(title="章", content=content))

        assert chapter["content"] == content
        assert len(http.post_calls) == 5
        assert proc.cache_stats["size"] == 0

    def test_duplicate_blocks_keep_their_positions(self, http: HttpStub) -> None:
        proc = EdgeTranslaterProcessor({"cache_size": 0})
        prime_edge_token(proc)
        a_block = self._repeated_blocks("a" * 900, 3)
        b_block = self._repeated_blocks("b" * 800, 3)
        content = "\n".join([a_block, b_block, a_block])
        http.post_queue = [
            edge_response("TITLE"),
            edge_response("AAA"),
            edge_response("BBB"),
        ]

        chapter = proc.process_chapter(make_chapter(title="t", content=content))

        assert chapter["title"] == "TITLE"
        assert chapter["content"] == "AAA\nBBB\nAAA"
        assert len(http.post_calls) == 3  # A translated once, then B

    def test_cache_hit_across_chapter_calls(self, http: HttpStub) -> None:
        http.post_response = edge_response("T")
        proc = EdgeTranslaterProcessor({})
        prime_edge_token(proc)
        chapter = make_chapter(title="hello", content="")

        proc.process_chapter(chapter)
        second = proc.process_chapter(chapter)

        assert second["title"] == "T"
        assert len(http.post_calls) == 1
        stats = proc.cache_stats
        assert stats["hits"] == 1
        assert stats["misses"] == 1

    def test_clear_cache_forces_new_requests(self, http: HttpStub) -> None:
        http.post_response = edge_response("T")
        proc = EdgeTranslaterProcessor({})
        prime_edge_token(proc)
        chapter = make_chapter(title="hello", content="")

        proc.process_chapter(chapter)
        proc.clear_cache()
        proc.process_chapter(chapter)

        assert len(http.post_calls) == 2
        stats = proc.cache_stats
        assert stats["hits"] == 0
        assert stats["misses"] == 1


# --------------------------------------------------------------------------- #
# Youdao
# --------------------------------------------------------------------------- #
class TestYoudaoCache:
    def test_repeated_text_hits_the_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = CountingClient(result="你好")
        monkeypatch.setattr(youdao_module, "_YoudaoWebFanyi", lambda: client)
        proc = YoudaoTranslaterProcessor({})
        chapter = make_chapter(title="hello", content="")

        proc.process_chapter(chapter)
        proc.process_chapter(chapter)

        assert client.calls == ["hello"]
        stats = proc.cache_stats
        assert stats["hits"] == 1
        assert stats["misses"] == 1

    def test_failure_is_not_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = CountingClient(error=RuntimeError("down"))
        monkeypatch.setattr(youdao_module, "_YoudaoWebFanyi", lambda: client)
        proc = YoudaoTranslaterProcessor({})
        chapter = make_chapter(title="hello", content="")

        proc.process_chapter(chapter)
        proc.process_chapter(chapter)

        assert client.calls == ["hello", "hello"]
        assert proc.cache_stats["size"] == 0

    def test_duplicate_chunks_are_translated_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = CountingClient(result="T")
        monkeypatch.setattr(youdao_module, "_YoudaoWebFanyi", lambda: client)
        # Disable the LRU cache so this test isolates within-batch dedupe.
        proc = YoudaoTranslaterProcessor({"cache_size": 0})
        content = "\n".join(["a" * 900] * 10)

        chapter = proc.process_chapter(make_chapter(title="章", content=content))

        assert chapter["content"] == "T\nT\nT\nT"
        assert len(client.calls) == 3  # title + one duplicated chunk + tail chunk

    def test_cache_is_keyed_by_target_language(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = CountingClient(result="T")
        monkeypatch.setattr(youdao_module, "_YoudaoWebFanyi", lambda: client)
        zh = YoudaoTranslaterProcessor({"target": "zh-CHS"})
        en = YoudaoTranslaterProcessor({"target": "en"})

        zh.process_chapter(make_chapter(title="hello", content=""))
        en.process_chapter(make_chapter(title="hello", content=""))

        # Separate processor instances have separate caches, but even inside
        # one instance the target language is part of the key.
        assert client.calls == ["hello", "hello"]
        assert zh.cache_stats["size"] == 1
        assert en.cache_stats["size"] == 1


# --------------------------------------------------------------------------- #
# concurrent auth / key fetch races
# --------------------------------------------------------------------------- #
class SlowCountingSession:
    """``requests.Session``-like stub whose GET blocks to widen races.

    The block uses ``Event.wait`` rather than ``time.sleep`` so the suite's
    autouse sleep-stubbing fixture cannot deflate the race window.
    """

    def __init__(self, delay: float = 0.3) -> None:
        self.gets = 0
        self._gate = threading.Event()
        self._delay = delay

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.gets += 1
        self._gate.wait(self._delay)
        return FakeResponse(
            200,
            {"code": 0, "data": {"secretKey": "S", "aesKey": "K", "aesIv": "I"}},
        )


def make_fake_jwt(exp: datetime) -> str:
    import base64 as b64
    import json as json_mod

    payload = json_mod.dumps({"exp": int(exp.timestamp())}).encode()
    return "h." + b64.urlsafe_b64encode(payload).decode().rstrip("=") + ".s"


class TestConcurrentAuth:
    def test_concurrent_youdao_key_fetches_are_coalesced(self) -> None:
        client = youdao_module._YoudaoWebFanyi()
        slow = SlowCountingSession()
        client.sess = slow  # type: ignore[assignment]

        threads = [threading.Thread(target=client._ensure_keys) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert all(not thread.is_alive() for thread in threads)
        assert slow.gets == 1

    def test_concurrent_edge_token_fetches_are_coalesced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gets = {"n": 0}

        gate = threading.Event()

        def slow_get(url: str, **kwargs: Any) -> FakeResponse:
            gets["n"] += 1
            gate.wait(0.3)
            return FakeResponse(200, text=make_fake_jwt(FAR_FUTURE))

        monkeypatch.setattr(requests, "get", slow_get)
        proc = EdgeTranslaterProcessor({})

        threads = [threading.Thread(target=proc._get_token) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert all(not thread.is_alive() for thread in threads)
        assert gets["n"] == 1

    def test_youdao_blank_text_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A whitespace-only title must not be sent to the service; Google and
        # Edge already short-circuit it.
        client = CountingClient(result="T")
        monkeypatch.setattr(youdao_module, "_YoudaoWebFanyi", lambda: client)
        proc = YoudaoTranslaterProcessor({})

        proc.process_chapter(make_chapter(title="   ", content=""))

        assert client.calls == []
