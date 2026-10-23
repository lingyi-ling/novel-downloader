#!/usr/bin/env python3
"""
Offline tests for the translation processors
(:mod:`...translator.google`, ``...translator.edge``, ``...translator.youdao``).

All three talk to the public internet, so every test replaces the HTTP layer
(``requests.get``/``requests.post`` or an injected session/client) and the
``time.sleep`` delay. This pins the contract operators care about:

* the documented request shape and paragraph handling;
* failure modes degrade to the *original* text (the pipeline must never lose a
  chapter because a translation endpoint is down);
* missing auth/token, malformed payloads and non-string metadata never raise.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import datetime
from typing import Any

import pytest
import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from novel_downloader.plugins.processors.translator.edge import (
    EdgeTranslaterProcessor,
)
from novel_downloader.plugins.processors.translator.google import (
    GoogleTranslaterProcessor,
)
from novel_downloader.plugins.processors.translator.youdao import (
    YoudaoTranslaterProcessor,
    _YoudaoWebFanyi,
)
from tests.plugins._processor_test_utils import make_book_info, make_chapter

GOOGLE_LOGGER = "novel_downloader.plugins.processors.translator.google"
EDGE_LOGGER = "novel_downloader.plugins.processors.translator.edge"
YOUDAO_LOGGER = "novel_downloader.plugins.processors.translator.youdao"

FAR_FUTURE = datetime(2100, 1, 1)


# --------------------------------------------------------------------------- #
# shared HTTP / sleep stubs
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
    """Records calls and serves queued responses for get/post."""

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

    @property
    def posted_json(self) -> list[Any]:
        return [
            json.loads(kwargs.get("data") or "null") for _, kwargs in self.post_calls
        ]

    @property
    def posted_form(self) -> list[dict[str, Any]]:
        return [kwargs.get("data") or {} for _, kwargs in self.post_calls]


class SleepRecorder:
    """Records ``time.sleep`` calls instead of blocking the suite."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[float] = []
        monkeypatch.setattr("time.sleep", self.calls.append)


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> HttpStub:
    stub = HttpStub()
    stub.install(monkeypatch)
    return stub


@pytest.fixture(autouse=True)
def sleep(monkeypatch: pytest.MonkeyPatch) -> SleepRecorder:
    """
    Never block on the processors' politeness delay.

    Autouse so that *every* test is fast and offline; tests that assert on the
    delay simply request this fixture by name.
    """
    return SleepRecorder(monkeypatch)


def sentences_response(*parts: str) -> FakeResponse:
    return FakeResponse(200, {"sentences": [{"trans": p} for p in parts]})


def jwt_token(exp: int = 4_102_444_800) -> str:
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    )
    return f"header.{payload}.signature"


def prime_edge_token(proc: EdgeTranslaterProcessor) -> None:
    proc._token_cache = {"token": "cached-token", "expire": FAR_FUTURE}


# --------------------------------------------------------------------------- #
# google
# --------------------------------------------------------------------------- #
class TestGoogle:
    def test_defaults_match_the_documented_values(self) -> None:
        proc = GoogleTranslaterProcessor({})
        assert proc._source == "auto"
        assert proc._target == "zh-CN"
        assert proc._sleep == 2.0

    def test_chapter_title_and_content_are_translated(self, http: HttpStub) -> None:
        http.post_response = sentences_response("你好，", "世界")
        proc = GoogleTranslaterProcessor({})

        chapter = proc.process_chapter(
            make_chapter(title="第一章", content="第一行。\n第二行。")
        )

        assert chapter["title"] == "你好，世界"
        # Google receives the whole content in one request (no splitting).
        assert chapter["content"] == "你好，世界"
        assert len(http.post_calls) == 2
        assert http.posted_form[0]["q"] == "第一章"
        assert http.posted_form[1]["q"] == "第一行。\n第二行。"

    def test_request_uses_documented_form_fields(self, http: HttpStub) -> None:
        http.post_response = sentences_response("ok")
        proc = GoogleTranslaterProcessor({"source": "en", "target": "fr"})

        proc.process_chapter(make_chapter(title="Hello", content=""))

        url, kwargs = http.post_calls[0]
        assert url == "https://translate.googleapis.com/translate_a/single"
        form = kwargs["data"]
        assert form["client"] == "gtx"
        assert form["sl"] == "en"
        assert form["tl"] == "fr"
        assert form["dj"] == "1"
        assert kwargs["timeout"] == 20

    def test_book_info_fields_are_translated(self, http: HttpStub) -> None:
        http.post_response = sentences_response("T")
        proc = GoogleTranslaterProcessor({})

        book_info = proc.process_book_info(
            make_book_info(
                summary_brief="短简介",
                volumes=[
                    {
                        "volume_name": "第一卷",
                        "volume_intro": "卷简介",
                        "chapters": [
                            {
                                "title": "章标题",
                                "url": "https://example.com/1",
                                "chapterId": "1",
                            }
                        ],
                    }
                ],
            )
        )

        assert book_info["book_name"] == "T"
        assert book_info["summary"] == "T"
        assert book_info["summary_brief"] == "T"
        volume = book_info["volumes"][0]
        assert volume["volume_name"] == "T"
        assert volume["volume_intro"] == "T"
        assert volume["chapters"][0]["title"] == "T"

    def test_optional_fields_are_left_absent_when_not_present(
        self, http: HttpStub
    ) -> None:
        http.post_response = sentences_response("T")
        proc = GoogleTranslaterProcessor({})

        book_info = proc.process_book_info(
            {
                "book_name": "书",
                "author": "作者",
                "cover_url": "",
                "update_time": "",
                "summary": "简介",
                "extra": {},
                "volumes": [
                    {"volume_name": "卷", "chapters": [{"title": "章"}]},
                ],
            }
        )

        assert "summary_brief" not in book_info
        assert "volume_intro" not in book_info["volumes"][0]

    def test_blank_text_short_circuits(
        self, http: HttpStub, sleep: SleepRecorder
    ) -> None:
        proc = GoogleTranslaterProcessor({})

        chapter = proc.process_chapter(make_chapter(title="   ", content="\n\t"))

        assert chapter["title"] == "   "
        assert chapter["content"] == "\n\t"
        assert http.post_calls == []
        assert sleep.calls == []

    def test_missing_fields_become_empty_strings(self, http: HttpStub) -> None:
        proc = GoogleTranslaterProcessor({})

        chapter = proc.process_chapter({"id": "1", "extra": {}})

        assert chapter["title"] == ""
        assert chapter["content"] == ""
        assert http.post_calls == []

    def test_http_error_returns_original_text(
        self, http: HttpStub, caplog: pytest.LogCaptureFixture
    ) -> None:
        http.post_response = FakeResponse(429, text="rate limited")
        proc = GoogleTranslaterProcessor({})

        with caplog.at_level(logging.WARNING, logger=GOOGLE_LOGGER):
            chapter = proc.process_chapter(make_chapter(title="题", content="文"))

        assert chapter["title"] == "题"
        assert chapter["content"] == "文"
        assert "HTTP 429" in caplog.text

    def test_network_error_returns_original_text(
        self, http: HttpStub, sleep: SleepRecorder
    ) -> None:
        http.post_raises = requests.ConnectionError("boom")
        proc = GoogleTranslaterProcessor({})

        chapter = proc.process_chapter(make_chapter(title="题", content="文"))

        assert chapter["title"] == "题"
        assert chapter["content"] == "文"
        assert sleep.calls == [2.0, 2.0]

    def test_malformed_payload_returns_original_text(self, http: HttpStub) -> None:
        http.post_response = FakeResponse(200, {"sentences": [{"no_trans": "x"}]})
        proc = GoogleTranslaterProcessor({})

        assert (
            proc.process_chapter(make_chapter(title="题", content=""))["title"] == "题"
        )

    def test_non_json_payload_returns_original_text(self, http: HttpStub) -> None:
        http.post_response = FakeResponse(200, ValueError("not json"))
        proc = GoogleTranslaterProcessor({})

        assert (
            proc.process_chapter(make_chapter(title="题", content=""))["title"] == "题"
        )

    def test_sleep_runs_after_every_request(
        self, http: HttpStub, sleep: SleepRecorder
    ) -> None:
        http.post_response = sentences_response("T")
        proc = GoogleTranslaterProcessor({"sleep": 0.5})

        proc.process_chapter(make_chapter(title="题", content="文"))

        assert sleep.calls == [0.5, 0.5]

    def test_negative_sleep_is_clamped(
        self, http: HttpStub, sleep: SleepRecorder
    ) -> None:
        http.post_response = sentences_response("T")
        proc = GoogleTranslaterProcessor({"sleep": -3})

        proc.process_chapter(make_chapter(title="题", content=""))

        assert sleep.calls == [0.0]

    def test_non_numeric_sleep_fails_fast(self) -> None:
        with pytest.raises(ValueError, match="could not convert string to float"):
            GoogleTranslaterProcessor({"sleep": "soon"})

    def test_non_string_fields_are_preserved(self, http: HttpStub) -> None:
        http.post_response = sentences_response("T")
        proc = GoogleTranslaterProcessor({})

        chapter = proc.process_chapter(make_chapter(title=None, content=None))
        assert chapter["title"] is None
        assert chapter["content"] is None

        book_info = proc.process_book_info(
            make_book_info(book_name=None, summary_brief=None)
        )
        assert book_info["book_name"] is None
        assert book_info["summary_brief"] is None

    def test_input_is_not_mutated(self, http: HttpStub) -> None:
        http.post_response = sentences_response("T")
        proc = GoogleTranslaterProcessor({})

        chapter = make_chapter(title="题", content="文")
        proc.process_chapter(chapter)

        assert chapter["title"] == "题"
        assert chapter["content"] == "文"


# --------------------------------------------------------------------------- #
# edge
# --------------------------------------------------------------------------- #
class TestEdgeSplitText:
    def test_blank_lines_are_dropped(self) -> None:
        assert EdgeTranslaterProcessor._split_text("\n\n  \n") == []

    def test_lines_are_stripped_and_joined(self) -> None:
        assert EdgeTranslaterProcessor._split_text("  a  \nb\n\n  c") == ["a\nb\nc"]

    def test_chunks_respect_max_length(self) -> None:
        chunks = EdgeTranslaterProcessor._split_text("aaaa\nbbbb\ncccc", max_length=9)
        assert chunks == ["aaaa\nbbbb", "cccc"]

    def test_single_overlong_line_stays_intact(self) -> None:
        long_line = "x" * 20
        assert EdgeTranslaterProcessor._split_text(long_line, max_length=5) == [
            long_line
        ]

    def test_empty_input_returns_no_chunks(self) -> None:
        assert EdgeTranslaterProcessor._split_text("") == []


class TestEdge:
    def test_defaults_match_the_documented_values(self) -> None:
        proc = EdgeTranslaterProcessor({})
        assert proc._source == "auto"
        assert proc._target == "zh-Hans"
        assert proc._sleep == 1.0

    def test_paragraphs_are_translated_one_by_one(
        self, http: HttpStub, sleep: SleepRecorder
    ) -> None:
        http.post_response = FakeResponse(200, [{"translations": [{"text": "T"}]}])
        proc = EdgeTranslaterProcessor({})
        prime_edge_token(proc)

        chapter = proc.process_chapter(
            make_chapter(title="章", content="第一段\n\n第二段")
        )

        assert chapter["title"] == "T"
        # Short content fits in one chunk: blank lines are dropped, the rest is
        # sent as a single paragraph (chunking happens above 3000 chars).
        assert chapter["content"] == "T"
        assert [body[0]["text"] for body in http.posted_json] == [
            "章",
            "第一段\n第二段",
        ]
        assert http.get_calls == []  # token was cached
        assert sleep.calls == [1.0, 1.0]

    def test_long_content_is_split_into_chunks(
        self, http: HttpStub, sleep: SleepRecorder
    ) -> None:
        http.post_response = FakeResponse(200, [{"translations": [{"text": "T"}]}])
        proc = EdgeTranslaterProcessor({})
        prime_edge_token(proc)

        content = "\n".join(["甲" * 2000] * 3)
        chapter = proc.process_chapter(make_chapter(title="章", content=content))

        assert chapter["content"] == "T\nT\nT"
        # one request for the title plus one per 2000-char chunk
        assert len(http.post_calls) == 4
        assert [len(body[0]["text"]) for body in http.posted_json[1:]] == [
            2000,
            2000,
            2000,
        ]

    def test_authorization_header_uses_cached_token(self, http: HttpStub) -> None:
        http.post_response = FakeResponse(200, [{"translations": [{"text": "T"}]}])
        proc = EdgeTranslaterProcessor({})
        prime_edge_token(proc)

        proc.process_chapter(make_chapter(title="章", content=""))

        _, kwargs = http.post_calls[0]
        assert kwargs["headers"]["authorization"] == "Bearer cached-token"

    def test_from_parameter_only_set_for_explicit_source(self, http: HttpStub) -> None:
        http.post_response = FakeResponse(200, [{"translations": [{"text": "T"}]}])
        proc = EdgeTranslaterProcessor({"source": "en", "target": "fr"})
        prime_edge_token(proc)

        proc.process_chapter(make_chapter(title="章", content=""))

        url, _ = http.post_calls[0]
        assert "from=en" in url
        assert "to=fr" in url
        assert "api-version=3.0" in url

    def test_auto_source_omits_from_parameter(self, http: HttpStub) -> None:
        http.post_response = FakeResponse(200, [{"translations": [{"text": "T"}]}])
        proc = EdgeTranslaterProcessor({})
        prime_edge_token(proc)

        proc.process_chapter(make_chapter(title="章", content=""))

        url, _ = http.post_calls[0]
        assert "from=" not in url

    def test_token_is_fetched_once_and_cached(self, http: HttpStub) -> None:
        http.get_response = FakeResponse(200, text=jwt_token())
        http.post_response = FakeResponse(200, [{"translations": [{"text": "T"}]}])
        proc = EdgeTranslaterProcessor({})

        proc.process_chapter(make_chapter(title="一", content=""))
        proc.process_chapter(make_chapter(title="二", content=""))

        assert len(http.get_calls) == 1
        assert len(http.post_calls) == 2

    def test_expired_token_is_refreshed(self, http: HttpStub) -> None:
        http.get_response = FakeResponse(200, text=jwt_token())
        http.post_response = FakeResponse(200, [{"translations": [{"text": "T"}]}])
        proc = EdgeTranslaterProcessor({})
        proc._token_cache = {"token": "stale", "expire": datetime(2000, 1, 1)}

        proc.process_chapter(make_chapter(title="章", content=""))

        assert len(http.get_calls) == 1
        assert proc._token_cache is not None
        assert proc._token_cache["token"] != "stale"

    def test_auth_network_failure_degrades_to_original_text(
        self, http: HttpStub, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The token call used to run outside the guarded block and blew up."""
        http.get_raises = requests.ConnectionError("auth down")
        proc = EdgeTranslaterProcessor({})

        with caplog.at_level(logging.ERROR, logger=EDGE_LOGGER):
            chapter = proc.process_chapter(make_chapter(title="题", content="文"))

        assert chapter["title"] == "题"
        assert chapter["content"] == "文"
        assert "Edge authentication failed" in caplog.text
        assert http.post_calls == []

    def test_invalid_auth_token_degrades_to_original_text(
        self, http: HttpStub, caplog: pytest.LogCaptureFixture
    ) -> None:
        http.get_response = FakeResponse(200, text="not-a-jwt")
        proc = EdgeTranslaterProcessor({})

        with caplog.at_level(logging.ERROR, logger=EDGE_LOGGER):
            chapter = proc.process_chapter(make_chapter(title="题", content=""))

        assert chapter["title"] == "题"
        assert "Edge authentication failed" in caplog.text

    def test_auth_http_error_degrades_to_original_text(self, http: HttpStub) -> None:
        http.get_response = FakeResponse(500, text="oops")
        proc = EdgeTranslaterProcessor({})

        assert (
            proc.process_chapter(make_chapter(title="题", content=""))["title"] == "题"
        )

    def test_parse_jwt_extracts_expiry(self) -> None:
        proc = EdgeTranslaterProcessor({})

        parsed = proc._parse_jwt(jwt_token(exp=4_102_444_800))

        assert parsed["expire"] == datetime.fromtimestamp(4_102_444_800)
        assert parsed["token"] == jwt_token(exp=4_102_444_800)

    @pytest.mark.parametrize(
        "token",
        [
            "",
            "nodots",
            "header.not-base64-payload!!.sig",
            "header." + base64.urlsafe_b64encode(b"{}").decode() + ".sig",
            "header."
            + base64.urlsafe_b64encode(json.dumps({"exp": "later"}).encode()).decode()
            + ".sig",
        ],
    )
    def test_parse_jwt_rejects_malformed_tokens(self, token: str) -> None:
        proc = EdgeTranslaterProcessor({})

        with pytest.raises(ValueError):
            proc._parse_jwt(token)

    def test_http_error_returns_original_paragraphs(
        self, http: HttpStub, caplog: pytest.LogCaptureFixture
    ) -> None:
        http.post_response = FakeResponse(500, text="server error")
        proc = EdgeTranslaterProcessor({})
        prime_edge_token(proc)

        with caplog.at_level(logging.WARNING, logger=EDGE_LOGGER):
            chapter = proc.process_chapter(make_chapter(title="题", content="文"))

        assert chapter["title"] == "题"
        assert chapter["content"] == "文"
        assert "HTTP 500" in caplog.text

    def test_unsupported_payload_shape_returns_original(self, http: HttpStub) -> None:
        http.post_response = FakeResponse(200, [])
        proc = EdgeTranslaterProcessor({})
        prime_edge_token(proc)

        assert (
            proc.process_chapter(make_chapter(title="题", content=""))["title"] == "题"
        )

    def test_sleep_runs_even_on_failure(
        self, http: HttpStub, sleep: SleepRecorder
    ) -> None:
        http.post_raises = requests.ConnectionError("boom")
        proc = EdgeTranslaterProcessor({"sleep": 0.25})
        prime_edge_token(proc)

        proc.process_chapter(make_chapter(title="题", content=""))

        assert sleep.calls == [0.25]

    def test_negative_sleep_is_clamped(
        self, http: HttpStub, sleep: SleepRecorder
    ) -> None:
        http.post_response = FakeResponse(200, [{"translations": [{"text": "T"}]}])
        proc = EdgeTranslaterProcessor({"sleep": -1})
        prime_edge_token(proc)

        proc.process_chapter(make_chapter(title="题", content=""))

        assert sleep.calls == [0.0]

    def test_blank_text_skips_network_and_sleep(
        self, http: HttpStub, sleep: SleepRecorder
    ) -> None:
        proc = EdgeTranslaterProcessor({})

        chapter = proc.process_chapter(make_chapter(title="   ", content="\n\n"))

        assert chapter["title"] == "   "
        assert chapter["content"] == ""
        assert http.post_calls == []
        assert http.get_calls == []
        assert sleep.calls == []

    def test_non_string_content_is_preserved(self, http: HttpStub) -> None:
        proc = EdgeTranslaterProcessor({})

        chapter = proc.process_chapter(make_chapter(title=None, content=None))

        assert chapter["title"] is None
        assert chapter["content"] is None
        assert http.post_calls == []

    def test_missing_fields_yield_empty_strings(self, http: HttpStub) -> None:
        proc = EdgeTranslaterProcessor({})

        chapter = proc.process_chapter({"id": "1", "extra": {}})

        assert chapter["title"] == ""
        assert chapter["content"] == ""

    def test_book_info_uses_the_same_translation_path(self, http: HttpStub) -> None:
        http.post_response = FakeResponse(200, [{"translations": [{"text": "T"}]}])
        proc = EdgeTranslaterProcessor({})
        prime_edge_token(proc)

        book_info = proc.process_book_info(make_book_info())

        assert book_info["book_name"] == "T"
        assert book_info["summary"] == "T"
        assert book_info["volumes"][0]["volume_name"] == "T"

    def test_input_is_not_mutated(self, http: HttpStub) -> None:
        http.post_response = FakeResponse(200, [{"translations": [{"text": "T"}]}])
        proc = EdgeTranslaterProcessor({})
        prime_edge_token(proc)

        chapter = make_chapter(title="题", content="文")
        proc.process_chapter(chapter)

        assert chapter["title"] == "题"
        assert chapter["content"] == "文"


# --------------------------------------------------------------------------- #
# youdao
# --------------------------------------------------------------------------- #
class StubYoudaoClient:
    """Stand-in for ``_YoudaoWebFanyi`` used to isolate the wrapper logic."""

    def __init__(self, result: Any = "你好", error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, str, str]] = []

    def translate(self, text: str, src: str = "auto", tgt: str = "zh-CHS") -> str:
        self.calls.append((text, src, tgt))
        if self.error is not None:
            raise self.error
        return self.result


class FakeSession:
    """Fake ``requests.Session`` for the real youdao client."""

    def __init__(
        self,
        key_response: Any = None,
        trans_responses: list[Any] | None = None,
        get_error: Exception | None = None,
    ) -> None:
        self.headers: dict[str, str] = {}
        self.key_response = key_response
        self.trans_responses = list(trans_responses or [])
        self.get_error = get_error
        self.get_calls: list[tuple[str, dict[str, Any]]] = []
        self.post_calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, params: Any = None, timeout: Any = None) -> Any:
        self.get_calls.append((url, dict(params or {})))
        if self.get_error is not None:
            raise self.get_error
        return self.key_response

    def post(self, url: str, data: Any = None, timeout: Any = None) -> Any:
        self.post_calls.append((url, dict(data or {})))
        if self.trans_responses:
            return self.trans_responses.pop(0)
        return FakeResponse(200, {})


def youdao_encrypt(payload: dict[str, Any], aes_key: str, aes_iv: str) -> str:
    """Encrypt a payload exactly the way the youdao web client expects it."""
    key = hashlib.md5(aes_key.encode("utf-8")).digest()
    iv = hashlib.md5(aes_iv.encode("utf-8")).digest()
    cipher = AES.new(key, AES.MODE_CBC, iv)
    blob = cipher.encrypt(pad(json.dumps(payload).encode("utf-8"), AES.block_size))
    return base64.b64encode(blob).decode("ascii")


KEY_PAYLOAD = {
    "code": 0,
    "data": {"secretKey": "secret", "aesKey": "aes-key", "aesIv": "aes-iv"},
}


class TestYoudaoLangMapping:
    @pytest.mark.parametrize(
        "code",
        [
            "zh",
            "zh_cn",
            "zh-CN",
            "zh_cns",
            "zh-chs",
            "zh_hans",
            "zh-Hans",
            "  ZH-CN  ",
        ],
    )
    def test_simplified_aliases(self, code: str) -> None:
        assert _YoudaoWebFanyi._norm_lang(code) == "zh-CHS"

    @pytest.mark.parametrize("code", ["zh_tw", "zh-TW", "zh_cht", "zh-hant"])
    def test_traditional_aliases(self, code: str) -> None:
        assert _YoudaoWebFanyi._norm_lang(code) == "zh-CHT"

    @pytest.mark.parametrize("code", ["", None, "  "])
    def test_empty_means_let_the_server_decide(self, code: Any) -> None:
        assert _YoudaoWebFanyi._norm_lang(code) == ""

    @pytest.mark.parametrize("code", ["en", "ja", "ko", "fr"])
    def test_other_languages_pass_through_unchanged(self, code: str) -> None:
        assert _YoudaoWebFanyi._norm_lang(code) == code


class TestYoudaoWrapper:
    def test_defaults_match_the_documented_values(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(
            "novel_downloader.plugins.processors.translator.youdao._YoudaoWebFanyi",
            lambda: StubYoudaoClient(),
        )
        proc = YoudaoTranslaterProcessor({})

        assert proc._source == "auto"
        assert proc._target == "zh-CHS"
        assert proc._sleep == 1.0

    def test_translates_through_the_client(self, monkeypatch: Any) -> None:
        client = StubYoudaoClient(result="你好")
        monkeypatch.setattr(
            "novel_downloader.plugins.processors.translator.youdao._YoudaoWebFanyi",
            lambda: client,
        )
        proc = YoudaoTranslaterProcessor({"source": "en", "target": "zh-CHS"})

        chapter = proc.process_chapter(make_chapter(title="hello", content="world"))

        assert chapter["title"] == "你好"
        assert chapter["content"] == "你好"
        assert client.calls == [("hello", "en", "zh-CHS"), ("world", "en", "zh-CHS")]

    def test_empty_input_is_not_sent(self, monkeypatch: Any) -> None:
        client = StubYoudaoClient()
        monkeypatch.setattr(
            "novel_downloader.plugins.processors.translator.youdao._YoudaoWebFanyi",
            lambda: client,
        )
        proc = YoudaoTranslaterProcessor({})

        assert proc.process_chapter(make_chapter(title="", content=""))["content"] == ""
        assert client.calls == []

    def test_failure_returns_original_text(
        self, monkeypatch: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = StubYoudaoClient(error=RuntimeError("service down"))
        monkeypatch.setattr(
            "novel_downloader.plugins.processors.translator.youdao._YoudaoWebFanyi",
            lambda: client,
        )
        proc = YoudaoTranslaterProcessor({})

        with caplog.at_level(logging.WARNING, logger=YOUDAO_LOGGER):
            chapter = proc.process_chapter(make_chapter(title="题", content="文"))

        assert chapter["title"] == "题"
        assert chapter["content"] == "文"
        assert "Youdao translate failed" in caplog.text

    def test_sleep_runs_only_after_success(
        self, monkeypatch: Any, sleep: SleepRecorder
    ) -> None:
        monkeypatch.setattr(
            "novel_downloader.plugins.processors.translator.youdao._YoudaoWebFanyi",
            lambda: StubYoudaoClient(result="你好"),
        )
        proc = YoudaoTranslaterProcessor({"sleep": 0.75})

        proc.process_chapter(make_chapter(title="题", content="文"))

        assert sleep.calls == [0.75, 0.75]

    def test_failure_does_not_sleep(
        self, monkeypatch: Any, sleep: SleepRecorder
    ) -> None:
        monkeypatch.setattr(
            "novel_downloader.plugins.processors.translator.youdao._YoudaoWebFanyi",
            lambda: StubYoudaoClient(error=RuntimeError("down")),
        )
        proc = YoudaoTranslaterProcessor({})

        proc.process_chapter(make_chapter(title="题", content=""))

        assert sleep.calls == []

    def test_zero_sleep_skips_the_call(
        self, monkeypatch: Any, sleep: SleepRecorder
    ) -> None:
        monkeypatch.setattr(
            "novel_downloader.plugins.processors.translator.youdao._YoudaoWebFanyi",
            lambda: StubYoudaoClient(result="你好"),
        )
        proc = YoudaoTranslaterProcessor({"sleep": 0})

        proc.process_chapter(make_chapter(title="题", content=""))

        assert sleep.calls == []

    def test_negative_sleep_is_clamped(
        self, monkeypatch: Any, sleep: SleepRecorder
    ) -> None:
        monkeypatch.setattr(
            "novel_downloader.plugins.processors.translator.youdao._YoudaoWebFanyi",
            lambda: StubYoudaoClient(result="你好"),
        )
        proc = YoudaoTranslaterProcessor({"sleep": -2})

        proc.process_chapter(make_chapter(title="题", content=""))

        assert sleep.calls == []

    def test_non_string_fields_are_preserved(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(
            "novel_downloader.plugins.processors.translator.youdao._YoudaoWebFanyi",
            lambda: StubYoudaoClient(),
        )
        proc = YoudaoTranslaterProcessor({})

        chapter = proc.process_chapter(make_chapter(title=None, content=None))

        assert chapter["title"] is None
        assert chapter["content"] is None

    def test_book_info_is_translated(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(
            "novel_downloader.plugins.processors.translator.youdao._YoudaoWebFanyi",
            lambda: StubYoudaoClient(result="T"),
        )
        proc = YoudaoTranslaterProcessor({})

        book_info = proc.process_book_info(
            make_book_info(
                summary_brief="短",
                volumes=[
                    {
                        "volume_name": "卷",
                        "volume_intro": "介",
                        "chapters": [{"title": "章"}],
                    }
                ],
            )
        )

        assert book_info["book_name"] == "T"
        assert book_info["summary_brief"] == "T"
        assert book_info["volumes"][0]["volume_intro"] == "T"

    def test_input_is_not_mutated(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(
            "novel_downloader.plugins.processors.translator.youdao._YoudaoWebFanyi",
            lambda: StubYoudaoClient(result="T"),
        )
        proc = YoudaoTranslaterProcessor({})

        chapter = make_chapter(title="题", content="文")
        proc.process_chapter(chapter)

        assert chapter["title"] == "题"


class TestYoudaoClient:
    def test_successful_roundtrip(self) -> None:
        session = FakeSession(
            key_response=FakeResponse(200, KEY_PAYLOAD),
            trans_responses=[
                FakeResponse(
                    200,
                    {},
                    text=youdao_encrypt(
                        {"code": 0, "translateResult": [[{"tgt": "你好"}]]},
                        "aes-key",
                        "aes-iv",
                    ),
                )
            ],
        )
        client = _YoudaoWebFanyi(session=session)  # type: ignore[arg-type]

        assert client.translate("hello", "auto", "zh-CHS") == "你好"
        assert len(session.get_calls) == 1
        assert len(session.post_calls) == 1
        _, form = session.post_calls[0]
        assert form["i"] == "hello"
        assert form["to"] == "zh-CHS"
        assert form["from"] == "auto"
        assert form["sign"] and form["mysticTime"]

    def test_multiple_segments_are_concatenated(self) -> None:
        payload = {
            "code": 0,
            "translateResult": [[{"tgt": "你"}, {"tgt": "好"}], [{"tgt": "！"}]],
        }
        session = FakeSession(
            key_response=FakeResponse(200, KEY_PAYLOAD),
            trans_responses=[
                FakeResponse(200, {}, text=youdao_encrypt(payload, "aes-key", "aes-iv"))
            ],
        )
        client = _YoudaoWebFanyi(session=session)  # type: ignore[arg-type]

        assert client.translate("hello") == "你好！"

    def test_keys_are_cached_between_calls(self) -> None:
        session = FakeSession(
            key_response=FakeResponse(200, KEY_PAYLOAD),
            trans_responses=[
                FakeResponse(
                    200,
                    {},
                    text=youdao_encrypt(
                        {"code": 0, "translateResult": [[{"tgt": "T"}]]},
                        "aes-key",
                        "aes-iv",
                    ),
                )
                for _ in range(2)
            ],
        )
        client = _YoudaoWebFanyi(session=session)  # type: ignore[arg-type]

        client.translate("a")
        client.translate("b")

        assert len(session.get_calls) == 1

    def test_failed_key_fetch_raises_runtime_error(self) -> None:
        session = FakeSession(get_error=requests.ConnectionError("no key"))
        client = _YoudaoWebFanyi(session=session)  # type: ignore[arg-type]

        with pytest.raises(RuntimeError, match="Failed to fetch Youdao keys"):
            client.translate("hello")

    def test_key_endpoint_error_code_is_rejected(self) -> None:
        session = FakeSession(
            key_response=FakeResponse(200, {"code": 50, "msg": "denied"})
        )
        client = _YoudaoWebFanyi(session=session)  # type: ignore[arg-type]

        with pytest.raises(RuntimeError, match="Failed to fetch Youdao keys"):
            client.translate("hello")

    def test_error_code_triggers_retry_then_raises(self) -> None:
        bad = youdao_encrypt({"code": 40}, "aes-key", "aes-iv")
        session = FakeSession(
            key_response=FakeResponse(200, KEY_PAYLOAD),
            trans_responses=[
                FakeResponse(200, {}, text=bad),
                FakeResponse(200, {}, text=bad),
            ],
        )
        client = _YoudaoWebFanyi(session=session)  # type: ignore[arg-type]

        with pytest.raises(RuntimeError, match="Youdao returned error"):
            client.translate("hello")

        assert len(session.post_calls) == 2
        assert len(session.get_calls) == 2  # forced key refetch between attempts

    def test_non_200_triggers_one_retry(self) -> None:
        session = FakeSession(
            key_response=FakeResponse(200, KEY_PAYLOAD),
            trans_responses=[
                FakeResponse(500, {}, text=""),
                FakeResponse(
                    200,
                    {},
                    text=youdao_encrypt(
                        {"code": 0, "translateResult": [[{"tgt": "T"}]]},
                        "aes-key",
                        "aes-iv",
                    ),
                ),
            ],
        )
        client = _YoudaoWebFanyi(session=session)  # type: ignore[arg-type]

        assert client.translate("hello") == "T"
        assert len(session.post_calls) == 2

    def test_garbage_payload_is_rejected(self) -> None:
        client = _YoudaoWebFanyi(session=FakeSession())  # type: ignore[arg-type]
        client._aes_key_bytes = hashlib.md5(b"k").digest()
        client._aes_iv_bytes = hashlib.md5(b"i").digest()

        with pytest.raises(ValueError):  # binascii.Error subclasses ValueError
            client._decrypt_payload("not-base64!!")

    def test_decrypt_before_keys_raise_runtime_error(self) -> None:
        client = _YoudaoWebFanyi(session=FakeSession())  # type: ignore[arg-type]

        with pytest.raises(RuntimeError, match="AES bytes not ready"):
            client._decrypt_payload("AAAA")

    def test_empty_text_returns_empty_without_network(self) -> None:
        session = FakeSession(key_response=FakeResponse(200, KEY_PAYLOAD))
        client = _YoudaoWebFanyi(session=session)  # type: ignore[arg-type]

        assert client.translate("") == ""
        assert session.get_calls == []
        assert session.post_calls == []
