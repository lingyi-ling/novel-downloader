#!/usr/bin/env python3
"""Boundary tests for HTTP response decoding.

Everything runs offline: response objects are constructed in memory, and the
backend consistency checks use a local ``aiohttp`` server bound to loopback.
"""

from __future__ import annotations

import codecs
import gzip
import json
import zlib
from collections.abc import AsyncIterator
from typing import Any

import aiohttp.cookiejar
import aiohttp.web
import pytest
import pytest_asyncio

from novel_downloader.infra.sessions import create_session
from novel_downloader.infra.sessions.response import (
    BaseResponse,
    ResponseJSONError,
)
from novel_downloader.schemas import FetcherConfig

DECLARED_CASES = [
    ("utf-8", "hello 世界"),
    ("gbk", "中文测试"),
    ("gb18030", "中文\U00020000"),
    ("big5", "繁體中文"),
    ("latin-1", "café"),
]
DECLARED_BODIES = dict(DECLARED_CASES)


# --------------------------------------------------------------------------- #
# Declared charsets
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("encoding", "body"), DECLARED_CASES)
def test_declared_encoding_wins(encoding: str, body: str) -> None:
    resp = BaseResponse(content=body.encode(encoding), encoding=encoding)

    assert resp.encoding == encoding
    assert resp.text == body


@pytest.mark.parametrize(("encoding", "body"), DECLARED_CASES)
@pytest.mark.parametrize("quoted", [False, True])
def test_charset_from_content_type_header(
    encoding: str, body: str, quoted: bool
) -> None:
    value = f'charset="{encoding}"' if quoted else f"charset={encoding}"
    resp = BaseResponse(
        content=body.encode(encoding),
        headers={"Content-Type": f"text/plain; {value}"},
    )

    assert resp.encoding == encoding
    assert resp.text == body


def test_content_type_charset_allows_spaces_and_case() -> None:
    resp = BaseResponse(
        content="中文测试".encode("gbk"),
        headers={"Content-Type": 'text/html; charset = "GBK"'},
    )

    assert resp.encoding == "GBK"
    assert resp.text == "中文测试"


def test_explicit_encoding_is_used_when_header_charset_fails() -> None:
    resp = BaseResponse(
        content="正文".encode("gbk"),
        headers={"Content-Type": "text/plain; charset=utf-8"},
        encoding="gbk",
    )

    assert resp.text == "正文"


def test_quoted_encoding_argument_is_normalised() -> None:
    resp = BaseResponse(content="繁體中文".encode("big5"), encoding='"big5"')

    assert resp.encoding == "big5"
    assert resp.text == "繁體中文"


def test_default_encoding_is_utf8_when_nothing_declared() -> None:
    resp = BaseResponse(content=b"hello")

    assert resp.encoding == "utf-8"
    assert resp.text == "hello"


# --------------------------------------------------------------------------- #
# Unknown / illegal charset names
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "encoding", ["not-a-real-codec", "x-bogus", "gb-zzz", "", None]
)
def test_unknown_encoding_falls_back_to_utf8(encoding: str | None) -> None:
    resp = BaseResponse(content="正文".encode(), encoding=encoding)

    assert resp.text == "正文"


def test_unknown_encoding_falls_back_to_gb_chain() -> None:
    resp = BaseResponse(
        content="中文测试".encode("gbk"),
        encoding="not-a-real-codec",
    )

    assert resp.text == "中文测试"


def test_unknown_encoding_never_raises_lookup_error() -> None:
    # ``GB18030``/``GBK`` reject 0xFF, so every fallback fails and the final
    # replacement decode is exercised.  ``LookupError`` for the declared name
    # must not escape either.
    resp = BaseResponse(content=b"\xff\xff", encoding="not-a-real-codec")

    assert resp.text == "\ufffd\ufffd"


def test_unknown_header_charset_does_not_break_decoding() -> None:
    resp = BaseResponse(
        content="中文测试".encode("gbk"),
        headers={"Content-Type": "text/plain; charset=x-bogus"},
    )

    assert resp.encoding == "x-bogus"
    assert resp.text == "中文测试"


# --------------------------------------------------------------------------- #
# Byte-order marks
# --------------------------------------------------------------------------- #
def test_utf8_bom_is_stripped() -> None:
    resp = BaseResponse(
        content=codecs.BOM_UTF8 + "hello 世界".encode(),
        encoding="utf-8",
    )

    assert resp.text == "hello 世界"


def test_utf8_bom_overrides_declared_charset() -> None:
    resp = BaseResponse(
        content=codecs.BOM_UTF8 + b"hello",
        encoding="gbk",
    )

    assert resp.text == "hello"


@pytest.mark.parametrize(
    "payload",
    [
        "hello 世界".encode("utf-16"),
        codecs.BOM_UTF16_LE + "hello 世界".encode("utf-16-le"),
        codecs.BOM_UTF16_BE + "hello 世界".encode("utf-16-be"),
    ],
)
def test_utf16_bom_is_detected_without_declared_charset(payload: bytes) -> None:
    resp = BaseResponse(content=payload)

    assert resp.text == "hello 世界"


def test_utf16_bom_overrides_declared_charset() -> None:
    resp = BaseResponse(
        content="hello 世界".encode("utf-16"),
        encoding="gbk",
    )

    assert resp.text == "hello 世界"


# --------------------------------------------------------------------------- #
# Missing Content-Type
# --------------------------------------------------------------------------- #
def test_no_headers_utf8_body() -> None:
    resp = BaseResponse(content="hello 世界".encode())

    assert resp.text == "hello 世界"


def test_no_headers_gbk_body_uses_fallback() -> None:
    resp = BaseResponse(content="中文测试".encode("gbk"))

    assert resp.text == "中文测试"


def test_content_type_without_charset_defaults_to_utf8() -> None:
    resp = BaseResponse(
        content=b"hello",
        headers={"Content-Type": "text/html"},
    )

    assert resp.encoding == "utf-8"
    assert resp.text == "hello"


def test_content_type_without_charset_uses_caller_encoding() -> None:
    resp = BaseResponse(
        content="中文测试".encode("gbk"),
        headers={"Content-Type": "text/html"},
        encoding="gbk",
    )

    assert resp.encoding == "gbk"
    assert resp.text == "中文测试"


# --------------------------------------------------------------------------- #
# JSON versus text
# --------------------------------------------------------------------------- #
def test_text_returns_raw_json_and_json_reparses_it() -> None:
    raw = '{"a": [1, 2], "b": "中文"}'
    resp = BaseResponse(
        content=raw.encode(),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )

    assert resp.text == raw
    assert resp.json() == {"a": [1, 2], "b": "中文"}
    # Reading the text must not consume or mutate the body.
    assert resp.text == raw
    assert resp.json() == {"a": [1, 2], "b": "中文"}


def test_json_uses_the_declared_non_utf8_charset() -> None:
    raw = '{"标题": "正文"}'
    resp = BaseResponse(content=raw.encode("gbk"), encoding="gbk")

    assert resp.json() == {"标题": "正文"}


@pytest.mark.parametrize(
    "payload",
    [
        codecs.BOM_UTF8 + b'{"a": 1}',
        '{"a": 1}'.encode("utf-16"),
    ],
    ids=["utf-8-bom", "utf-16-bom"],
)
def test_json_with_bom(payload: bytes) -> None:
    resp = BaseResponse(content=payload)

    assert resp.json() == {"a": 1}


def test_json_error_is_readable_and_typed() -> None:
    resp = BaseResponse(
        content=b"<html>oops</html>",
        headers={"Content-Type": "text/html; charset=utf-8"},
        status=502,
    )

    with pytest.raises(ResponseJSONError) as excinfo:
        resp.json()

    error = excinfo.value
    message = str(error)
    assert isinstance(error, ValueError)
    assert "JSON" in message
    assert "status=502" in message
    assert "text/html" in message
    assert "<html>oops</html>" in message
    assert error.status == 502
    assert error.content_type == "text/html; charset=utf-8"
    assert error.encoding == "utf-8"
    assert error.body_preview == "<html>oops</html>"
    assert isinstance(error.__cause__, json.JSONDecodeError)


def test_json_error_on_empty_body_is_readable() -> None:
    resp = BaseResponse(content=b"")

    assert resp.text == ""
    with pytest.raises(ResponseJSONError, match="body is empty"):
        resp.json()


def test_json_error_preview_is_truncated() -> None:
    resp = BaseResponse(content=("x" * 500).encode())

    with pytest.raises(ResponseJSONError) as excinfo:
        resp.json()

    preview = excinfo.value.body_preview
    assert preview.endswith("...")
    assert len(preview) == 123
    assert len(str(excinfo.value)) < 400


# --------------------------------------------------------------------------- #
# Compressed bodies
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("content_encoding", "payload"),
    [
        ("gzip", gzip.compress("中文测试".encode())),
        ("deflate", zlib.compress("中文测试".encode())),
        ("gzip", b"\x1f\x8bcorrupt-but-not-gzip"),
        ("deflate", b"corrupt-but-not-deflate"),
    ],
)
def test_base_response_never_raises_on_compressed_or_corrupt_bytes(
    content_encoding: str, payload: bytes
) -> None:
    # Backends hand ``BaseResponse`` already-decompressed bytes.  If a body is
    # still (or was never) compressed, decoding must stay total instead of
    # raising, and no double decompression may be attempted from the header.
    resp = BaseResponse(
        content=payload,
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "Content-Encoding": content_encoding,
        },
    )

    assert isinstance(resp.text, str)
    assert resp.encoding == "utf-8"


def test_json_on_compressed_body_gives_readable_error() -> None:
    resp = BaseResponse(
        content=gzip.compress(b'{"a": 1}'),
        headers={"Content-Encoding": "gzip"},
    )

    with pytest.raises(ResponseJSONError, match="JSON"):
        resp.json()


# --------------------------------------------------------------------------- #
# Empty and very large bodies
# --------------------------------------------------------------------------- #
def test_empty_body_text_is_empty_string() -> None:
    resp = BaseResponse(content=b"", encoding="utf-8")

    assert resp.text == ""
    assert resp.ok is True


def test_large_body_round_trip() -> None:
    body = "长文本" * 200_000  # ~1.8 MB as UTF-8
    resp = BaseResponse(content=body.encode(), encoding="utf-8")

    decoded = resp.text
    assert len(decoded) == len(body)
    assert decoded == body


def test_large_json_body_round_trip() -> None:
    payload = {"items": list(range(20_000)), "title": "标题"}
    resp = BaseResponse(content=json.dumps(payload).encode())

    assert resp.json() == payload


# --------------------------------------------------------------------------- #
# Backend wrappers: same contract across aiohttp / httpx / curl_cffi
# --------------------------------------------------------------------------- #
BACKENDS = ["aiohttp", "httpx", "curl_cffi"]


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    """Parametrize over installed backends; skip optional ones gracefully."""
    if request.param != "aiohttp":
        pytest.importorskip(request.param)
    return str(request.param)


@pytest.fixture(autouse=True)
def _allow_ip_cookies(monkeypatch: pytest.MonkeyPatch) -> None:
    """aiohttp refuses cookies for IP hosts unless the jar is "unsafe"."""
    monkeypatch.setattr(aiohttp.cookiejar, "is_ip_address", lambda host: False)


def _cfg(backend: str) -> FetcherConfig:
    return FetcherConfig(
        backend=backend,
        timeout=5.0,
        headers={"User-Agent": "pytest-response-decoding"},
        trust_env=False,
        verify_ssl=False,
        max_connections=5,
        # Only used by curl_cffi; other backends ignore it.
        impersonate="chrome",
    )


def _url(server: Any, path: str) -> str:
    return str(server.make_url(path))


@pytest_asyncio.fixture
async def server(aiohttp_server: Any) -> AsyncIterator[Any]:
    async def declared(request: aiohttp.web.Request) -> aiohttp.web.Response:
        charset = request.match_info["charset"]
        return aiohttp.web.Response(
            body=DECLARED_BODIES[charset].encode(charset),
            content_type="text/plain",
            charset=charset,
        )

    async def unknown_charset(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(
            body="中文测试".encode("gbk"),
            content_type="text/plain",
            charset="x-bogus",
        )

    async def no_charset(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(
            body="中文测试".encode("gbk"),
            content_type="text/plain",
        )

    async def gzip_ok(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(
            body=gzip.compress("中文测试".encode()),
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Encoding": "gzip",
            },
        )

    async def deflate_ok(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(
            body=zlib.compress("中文测试".encode()),
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Encoding": "deflate",
            },
        )

    async def bom_utf8(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(
            body=codecs.BOM_UTF8 + "hello 世界".encode(),
            content_type="text/plain",
        )

    async def bom_utf16(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(
            body="hello 世界".encode("utf-16"),
            content_type="text/plain",
        )

    async def quoted_charset(
        request: aiohttp.web.Request,
    ) -> aiohttp.web.StreamResponse:
        # A raw stream lets us keep the quotes around the charset value.
        response = aiohttp.web.StreamResponse(
            status=200,
            headers={"Content-Type": 'text/plain; charset="big5"'},
        )
        await response.prepare(request)
        await response.write("繁體中文".encode("big5"))
        await response.write_eof()
        return response

    app = aiohttp.web.Application()
    app.router.add_get("/declared/{charset}", declared)
    app.router.add_get("/unknown-charset", unknown_charset)
    app.router.add_get("/no-charset", no_charset)
    app.router.add_get("/gzip", gzip_ok)
    app.router.add_get("/deflate", deflate_ok)
    app.router.add_get("/bom-utf8", bom_utf8)
    app.router.add_get("/bom-utf16", bom_utf16)
    app.router.add_get("/quoted-charset", quoted_charset)

    return await aiohttp_server(app)


@pytest.mark.asyncio
@pytest.mark.parametrize(("charset", "body"), DECLARED_CASES)
async def test_backend_honours_declared_charset(
    backend: str, server: Any, charset: str, body: str
) -> None:
    async with create_session(backend, _cfg(backend)) as session:
        response = await session.get(
            _url(server, f"/declared/{charset}"), encoding="utf-8"
        )

    assert response.encoding == charset
    assert response.text == body


@pytest.mark.asyncio
async def test_backend_unknown_charset_falls_back(backend: str, server: Any) -> None:
    async with create_session(backend, _cfg(backend)) as session:
        response = await session.get(_url(server, "/unknown-charset"))

    # Backends echo the advertised (bogus) name; decoding must still work.
    assert response.encoding == "x-bogus"
    assert response.text == "中文测试"


@pytest.mark.asyncio
async def test_backend_without_charset_uses_caller_encoding(
    backend: str, server: Any
) -> None:
    async with create_session(backend, _cfg(backend)) as session:
        response = await session.get(_url(server, "/no-charset"), encoding="gbk")

    assert response.encoding == "gbk"
    assert response.text == "中文测试"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/gzip", "/deflate"])
async def test_backend_decompresses_transparently(
    backend: str, server: Any, path: str
) -> None:
    async with create_session(backend, _cfg(backend)) as session:
        response = await session.get(_url(server, path))

    # The transport decompresses before BaseResponse sees the bytes; the
    # Content-Encoding header must not trigger a second decompression.
    assert response.headers["content-encoding"] == path.removeprefix("/")
    assert response.text == "中文测试"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/bom-utf8", "/bom-utf16"])
async def test_backend_strips_bom(backend: str, server: Any, path: str) -> None:
    async with create_session(backend, _cfg(backend)) as session:
        response = await session.get(_url(server, path))

    assert response.text == "hello 世界"


@pytest.mark.asyncio
async def test_backend_decodes_quoted_charset(backend: str, server: Any) -> None:
    async with create_session(backend, _cfg(backend)) as session:
        response = await session.get(_url(server, "/quoted-charset"))

    # curl_cffi's charset regex skips the quoted value, so ``encoding`` may
    # still be the caller default; the header-based candidate must decode it.
    assert response.headers["content-type"] == 'text/plain; charset="big5"'
    assert response.text == "繁體中文"


@pytest.mark.asyncio
async def test_backend_json_after_gbk_decoding(backend: str, server: Any) -> None:
    # A JSON endpoint is not needed: reuse the declared gbk body as a sanity
    # check that ``json()`` reads the same decoded text as ``text``.
    async with create_session(backend, _cfg(backend)) as session:
        response = await session.get(_url(server, "/declared/gbk"))

    assert response.text == "中文测试"
    with pytest.raises(ResponseJSONError):
        response.json()
