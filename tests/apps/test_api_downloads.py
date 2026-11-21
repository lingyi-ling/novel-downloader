#!/usr/bin/env python3
"""
tests.apps.test_api_downloads
-----------------------------

Contract tests for the ``/downloads`` static-file surface of both servers:
the REST API's ``StaticFiles`` mount (guarded by
:class:`~novel_downloader.apps.api.middleware.DownloadsAuthMiddleware`) and the
Android/desktop embedded engine's ``_serve_download`` handler.

Contract pinned here (constants live in ``apps/api/middleware.py`` and
``apps/embedded/server.py``):

==========================================  =================================
Case                                        Expected
==========================================  =================================
no token configured                         ``200`` — localhost default
token configured, no/wrong credential       ``401`` + ``WWW-Authenticate``,
                                            body never reveals whether the
                                            file exists
token configured, correct Bearer/X-API-Key  ``200``
traversal (``..``, ``%2e%2e``, backslash,   ``400``/``404``, body is a JSON
absolute path, NUL byte)                    ``{"detail": ...}`` and never
                                            contains the target file
directory request                           ``404`` (no listing)
missing file                                ``404``
symlink/junction escaping the export dir    ``404``
``Content-Type``                            REST: by extension; embedded:
                                            ``application/octet-stream``
``Content-Length``                          exact byte count
``Content-Disposition``                     REST: absent; embedded:
                                            ``attachment; filename=...``
``Cache-Control``                           REST: ``public, max-age=3600``;
                                            embedded: absent (documented)
``Content-Security-Policy``                 ``sandbox; default-src 'none'``
``X-Content-Type-Options``                  ``nosniff``
``Referrer-Policy``                         ``no-referrer``
``X-Request-ID``                            present (echoed when safe)
``Range``                                   REST: ``206``/``416``/``400``;
                                            embedded: ignored, full ``200``
``HEAD``                                    headers only, no body
large file                                  streamed in ``FileResponse``
                                            chunks, never one buffer
==========================================  =================================

The embedded engine deliberately differs where noted; those differences are
*recorded* rather than "fixed" — it serves native clients over loopback with
a stdlib-only stack. Everything is offline: REST requests go through
``httpx.ASGITransport`` (or a raw ASGI call when a path must bypass httpx's
own ``..`` normalisation), and the embedded checks use a loopback listener
the test starts itself.
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.applications import Starlette

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api.middleware import (
    CACHE_CONTROL_NO_STORE,
    CACHE_CONTROL_PUBLIC,
    DOWNLOADS_CSP,
    REQUEST_ID_HEADER,
    SECURITY_HEADERS,
    routed_path,
)
from novel_downloader.apps.common import history_files
from novel_downloader.apps.core.task_manager import manager
from novel_downloader.apps.embedded import server as eserver

#: Marker written to a file *outside* the export directory. Any response that
#: contains it proves a traversal leak.
SECRET_MARKER = "SECRET_MARKER_DO_NOT_SERVE"

#: Credential used by the token tests.
TOKEN = "downloads-test-token"

#: Paths that must never resolve to a file outside the export directory.
TRAVERSAL_PATHS = [
    "/downloads/../secret.txt",
    "/downloads/%2e%2e/secret.txt",
    "/downloads/%2E%2E/secret.txt",
    "/downloads/..%2fsecret.txt",
    "/downloads/..%5csecret.txt",
    "/downloads/%2e%2e%5csecret.txt",
    "/downloads/..\\secret.txt",
    "/downloads/C:\\Windows\\win.ini",
    "/downloads//etc/passwd",
    "/downloads/book.txt\x00.png",
    "/downloads/\x00.txt",
]

#: Statuses accepted for a refused traversal (Starlette's own 404, or a 400
#: from a lower layer).
REFUSED = (400, 404)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep config/state out of the real user profile and clear the token."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)


@pytest.fixture
def export_dir(tmp_path: Path) -> Path:
    """The served directory, plus one file outside it holding the marker."""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "book.txt").write_text("hello", encoding="utf-8")
    (downloads / "book.epub").write_bytes(b"PK\x03\x04epub")
    (downloads / "sub").mkdir()
    (downloads / "sub" / "inner.txt").write_text("inner", encoding="utf-8")
    (tmp_path / "secret.txt").write_text(SECRET_MARKER, encoding="utf-8")
    return downloads


@pytest.fixture
def make_app(tmp_path: Path) -> Any:
    """Factory building apps whose ``/downloads`` points at the fixture dir."""

    def _make(**kwargs: Any) -> FastAPI:
        kwargs.setdefault("output_dir", tmp_path / "downloads")
        kwargs.setdefault("rate_limit", None)
        return create_app(**kwargs)

    return _make


@asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Serve ``app`` in-process without a socket."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def _raw(
    app: Any,
    path: str,
    *,
    method: str = "GET",
    headers: tuple[tuple[bytes, bytes], ...] = (),
    root_path: str = "",
    body: bytes = b"",
    spec_version: str = "2.4",
) -> tuple[int, dict[str, str], bytes, list[bytes]]:
    """
    Drive the ASGI app with an exact path and collect the response.

    ``httpx`` normalises ``..`` while building a request, so a traversal test
    that needs the literal path on the wire cannot use it. Returns
    ``(status, headers, body, body_chunks)``; the chunk list is what the
    streaming test inspects.
    """
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {
        "app": app,
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8", "surrogateescape"),
        "query_string": b"",
        "root_path": root_path,
        "headers": [(b"host", b"test"), *headers],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }
    await app(scope, receive, send)

    start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    response_headers = {
        name.decode("latin-1").lower(): value.decode("latin-1")
        for name, value in start["headers"]
    }
    chunks = [
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    ]
    return int(start["status"]), response_headers, b"".join(chunks), chunks


def _assert_security_headers(headers: dict[str, str]) -> None:
    """Every ``/downloads`` response carries the baseline headers and the CSP."""
    for name, value in SECURITY_HEADERS:
        assert headers[name.lower()] == value
    assert headers["content-security-policy"] == DOWNLOADS_CSP
    assert headers.get(REQUEST_ID_HEADER.lower(), "").strip()


def _make_link(link: Path, target: Path) -> bool:
    """
    Create a directory link (junction on Windows, symlink elsewhere).

    Returns ``False`` when the platform/privileges do not allow it, so the
    symlink tests skip instead of failing on a hardened machine.
    """
    try:
        if sys.platform.startswith("win"):
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                check=False,
            )
            return result.returncode == 0 and link.is_dir()
        os.symlink(target, link, target_is_directory=True)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_without_a_token_downloads_are_public(
    export_dir: Path, make_app: Any
) -> None:
    """The localhost default keeps the static mount unauthenticated."""
    async with _client(make_app()) as client:
        response = await client.get("/downloads/book.txt")

    assert response.status_code == 200
    assert response.text == "hello"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"X-API-Key": "wrong-token"},
        {"Authorization": "Basic " + TOKEN},
    ],
    ids=["missing", "wrong-bearer", "wrong-key", "wrong-scheme"],
)
async def test_token_rejects_missing_or_wrong_credentials(
    export_dir: Path, make_app: Any, headers: dict[str, str]
) -> None:
    """A missing or wrong credential is a 401, never a file."""
    async with _client(make_app(token=TOKEN)) as client:
        response = await client.get("/downloads/book.txt", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"].startswith("Invalid or missing API token")
    assert "hello" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": f"Bearer {TOKEN}"},
        {"X-API-Key": TOKEN},
        {"Authorization": f"bearer {TOKEN}"},
    ],
    ids=["bearer", "api-key", "lowercase-scheme"],
)
async def test_token_accepts_both_header_forms(
    export_dir: Path, make_app: Any, headers: dict[str, str]
) -> None:
    async with _client(make_app(token=TOKEN)) as client:
        response = await client.get("/downloads/book.txt", headers=headers)

    assert response.status_code == 200
    assert response.text == "hello"


@pytest.mark.asyncio
async def test_401_does_not_reveal_whether_the_file_exists(
    export_dir: Path, make_app: Any
) -> None:
    """Existing and missing files must be indistinguishable without a token."""
    async with _client(make_app(token=TOKEN)) as client:
        existing = await client.get("/downloads/book.txt")
        missing = await client.get("/downloads/does-not-exist.txt")

    assert existing.status_code == missing.status_code == 401
    assert existing.content == missing.content
    assert existing.headers["content-type"] == missing.headers["content-type"]


@pytest.mark.asyncio
async def test_token_still_rejects_traversal_attempts(
    export_dir: Path, make_app: Any
) -> None:
    """Authentication must not weaken the traversal protection."""
    app = make_app(token=TOKEN)
    headers = ((b"authorization", f"Bearer {TOKEN}".encode()),)

    for path in TRAVERSAL_PATHS:
        status, response_headers, body, _ = await _raw(app, path, headers=headers)
        assert status in REFUSED, (path, status)
        assert SECRET_MARKER.encode() not in body, path
        assert response_headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_auth_applies_under_a_mounted_prefix(tmp_path: Path) -> None:
    """
    Regression: a sub-mount must not skip the token gate.

    ``scope["path"]`` keeps the mount prefix while the router matches the
    path with ``root_path`` removed. A prefix test on the raw path used to
    treat ``/noveldl/downloads/book.txt`` as unguarded and serve the file
    without a token; every path-based middleware now uses
    :func:`routed_path`.
    """
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "book.txt").write_text(SECRET_MARKER, encoding="utf-8")

    api = create_app(output_dir=downloads, rate_limit=None, token=TOKEN)
    parent = Starlette()
    parent.mount("/noveldl", api)

    transport = httpx.ASGITransport(app=parent)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        anonymous = await client.get("/noveldl/downloads/book.txt")
        authorized = await client.get(
            "/noveldl/downloads/book.txt",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        health = await client.get("/noveldl/api/v1/health")

    assert anonymous.status_code == 401
    assert SECRET_MARKER not in anonymous.text
    assert authorized.status_code == 200
    assert authorized.text == SECRET_MARKER
    assert health.status_code == 401
    # The sandbox CSP and cache policy follow the request under the mount too.
    assert authorized.headers["content-security-policy"] == DOWNLOADS_CSP
    assert authorized.headers["cache-control"] == CACHE_CONTROL_PUBLIC


@pytest.mark.asyncio
async def test_body_limit_applies_under_a_mounted_prefix(tmp_path: Path) -> None:
    """Regression: a sub-mount must not skip the 413 body cap."""
    api = create_app(
        output_dir=tmp_path / "downloads",
        rate_limit=None,
        token=TOKEN,
        max_body_bytes=64,
    )
    parent = Starlette()
    parent.mount("/noveldl", api)

    transport = httpx.ASGITransport(app=parent)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.post(
            "/noveldl/api/v1/search",
            content=b'{"keyword": "' + b"x" * 500 + b'"}',
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {TOKEN}",
            },
        )

    assert response.status_code == 413
    assert response.headers["cache-control"] == CACHE_CONTROL_NO_STORE


def test_routed_path_mirrors_starlette() -> None:
    """Unit coverage for the prefix helper used by every middleware."""
    assert routed_path({"path": "/downloads/x"}) == "/downloads/x"
    assert routed_path({"path": "/downloads/x", "root_path": ""}) == "/downloads/x"
    assert routed_path({"path": "/noveldl/downloads/x", "root_path": "/noveldl"}) == (
        "/downloads/x"
    )
    assert routed_path({"path": "/noveldl", "root_path": "/noveldl"}) == ""
    # A look-alike prefix must not be stripped (no ``/`` boundary).
    assert routed_path({"path": "/noveldlX/a", "root_path": "/noveldl"}) == (
        "/noveldlX/a"
    )
    # A path that does not carry the prefix is returned unchanged.
    assert routed_path({"path": "/other", "root_path": "/noveldl"}) == "/other"


# ---------------------------------------------------------------------------
# Traversal, directories, missing files, links
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", TRAVERSAL_PATHS)
async def test_traversal_is_refused_without_leaking(
    export_dir: Path, make_app: Any, path: str
) -> None:
    """No spelling of ``..`` may read outside the export directory."""
    status, headers, body, _ = await _raw(make_app(), path)

    assert status in REFUSED, (path, status)
    assert SECRET_MARKER.encode() not in body, path
    assert b"root:" not in body, path
    # The refusal is the standard JSON error, not a directory listing or a
    # Python traceback that would describe the filesystem.
    assert headers["content-type"].startswith("application/json")
    assert json.loads(body)["detail"] == "Not Found"
    assert str(export_dir.parent).encode() not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/downloads/sub", "/downloads/sub/", "/downloads/"])
async def test_directory_requests_are_not_listed(
    export_dir: Path, make_app: Any, path: str
) -> None:
    """A directory URL is a 404, never an index of the export directory."""
    status, _, body, _ = await _raw(make_app(), path)

    assert status in REFUSED, (path, status)
    assert b"inner.txt" not in body
    assert json.loads(body)["detail"] == "Not Found"


@pytest.mark.asyncio
async def test_missing_file_is_a_json_404(export_dir: Path, make_app: Any) -> None:
    async with _client(make_app()) as client:
        response = await client.get("/downloads/missing.txt")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert response.headers["cache-control"] == CACHE_CONTROL_NO_STORE


@pytest.mark.asyncio
async def test_link_escaping_the_export_directory_is_not_followed(
    export_dir: Path, tmp_path: Path, make_app: Any
) -> None:
    """A junction/symlink planted in the export dir must not escape it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.txt").write_text(SECRET_MARKER, encoding="utf-8")
    if not _make_link(export_dir / "escape", outside):
        pytest.skip("directory links are not constructible here")

    app = make_app()
    for path in ("/downloads/escape/leak.txt", "/downloads/escape"):
        status, _, body, _ = await _raw(app, path)
        assert status in REFUSED, (path, status)
        assert SECRET_MARKER.encode() not in body, path


def test_in_tree_link_is_still_served(export_dir: Path) -> None:
    """Containment must not break a link that stays inside the export dir."""
    link = export_dir / "alias"
    if not _make_link(link, export_dir / "sub"):
        pytest.skip("directory links are not constructible here")

    from novel_downloader.apps.embedded.server import _resolve_download_file

    resolved = _resolve_download_file(link, "inner.txt")
    assert resolved is not None
    assert resolved.read_text(encoding="utf-8") == "inner"


# ---------------------------------------------------------------------------
# Response headers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "content_type"),
    [("book.txt", "text/plain; charset=utf-8"), ("book.epub", "application/epub+zip")],
)
async def test_content_type_and_length_are_exact(
    export_dir: Path, make_app: Any, name: str, content_type: str
) -> None:
    async with _client(make_app()) as client:
        response = await client.get(f"/downloads/{name}")

    assert response.status_code == 200
    assert response.headers["content-type"] == content_type
    assert int(response.headers["content-length"]) == len(response.content)
    assert response.headers["accept-ranges"] == "bytes"
    # REST serves the file inline; the embedded engine is the one that sets an
    # attachment disposition (recorded difference, pinned below).
    assert "content-disposition" not in response.headers


@pytest.mark.asyncio
async def test_success_carries_the_downloads_header_contract(
    export_dir: Path, make_app: Any
) -> None:
    status, headers, _, _ = await _raw(make_app(), "/downloads/book.txt")

    assert status == 200
    _assert_security_headers(headers)
    assert headers["cache-control"] == CACHE_CONTROL_PUBLIC


@pytest.mark.asyncio
async def test_error_carries_the_downloads_header_contract(
    export_dir: Path, make_app: Any
) -> None:
    """Errors keep the CSP and security headers but must never be cached."""
    status, headers, _, _ = await _raw(make_app(), "/downloads/missing.txt")

    assert status == 404
    _assert_security_headers(headers)
    assert headers["cache-control"] == CACHE_CONTROL_NO_STORE


@pytest.mark.asyncio
async def test_401_carries_the_downloads_header_contract(
    export_dir: Path, make_app: Any
) -> None:
    status, headers, _, _ = await _raw(make_app(token=TOKEN), "/downloads/book.txt")

    assert status == 401
    _assert_security_headers(headers)
    assert headers["cache-control"] == CACHE_CONTROL_NO_STORE


@pytest.mark.asyncio
async def test_client_request_id_is_echoed_on_downloads(
    export_dir: Path, make_app: Any
) -> None:
    status, headers, _, _ = await _raw(
        make_app(),
        "/downloads/book.txt",
        headers=((b"x-request-id", b"trace-123"),),
    )

    assert status == 200
    assert headers["x-request-id"] == "trace-123"


@pytest.mark.asyncio
async def test_unsafe_request_id_is_replaced_on_downloads(
    export_dir: Path, make_app: Any
) -> None:
    status, headers, _, _ = await _raw(
        make_app(),
        "/downloads/book.txt",
        headers=((b"x-request-id", b"bad id!"),),
    )

    assert status == 200
    assert headers["x-request-id"] != "bad id!"
    assert headers["x-request-id"].strip()


@pytest.mark.asyncio
async def test_head_returns_headers_only(export_dir: Path, make_app: Any) -> None:
    async with _client(make_app()) as client:
        response = await client.head("/downloads/book.txt")

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == "5"
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["cache-control"] == CACHE_CONTROL_PUBLIC
    assert response.headers["content-security-policy"] == DOWNLOADS_CSP
    assert response.headers[REQUEST_ID_HEADER]


@pytest.mark.asyncio
async def test_conditional_request_is_revalidated(
    export_dir: Path, make_app: Any
) -> None:
    """A revalidated download stays cacheable and keeps the sandbox."""
    async with _client(make_app()) as client:
        first = await client.get("/downloads/book.txt")
        second = await client.get(
            "/downloads/book.txt",
            headers={"If-None-Match": first.headers["etag"]},
        )

    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["cache-control"] == CACHE_CONTROL_PUBLIC
    assert second.headers["content-security-policy"] == DOWNLOADS_CSP


# ---------------------------------------------------------------------------
# Range requests and HEAD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_range_returns_206(export_dir: Path, make_app: Any) -> None:
    async with _client(make_app()) as client:
        response = await client.get(
            "/downloads/book.txt", headers={"Range": "bytes=0-2"}
        )

    assert response.status_code == 206
    assert response.content == b"hel"
    assert response.headers["content-range"] == "bytes 0-2/5"
    assert response.headers["content-length"] == "3"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-security-policy"] == DOWNLOADS_CSP


@pytest.mark.asyncio
async def test_unsatisfiable_range_returns_416(export_dir: Path, make_app: Any) -> None:
    status, headers, _, _ = await _raw(
        make_app(),
        "/downloads/book.txt",
        headers=((b"range", b"bytes=100-200"),),
    )

    assert status == 416
    assert headers["content-range"] == "bytes */5"
    assert headers["cache-control"] == CACHE_CONTROL_NO_STORE
    assert headers["content-security-policy"] == DOWNLOADS_CSP


@pytest.mark.asyncio
async def test_malformed_range_returns_400(export_dir: Path, make_app: Any) -> None:
    status, headers, _, _ = await _raw(
        make_app(),
        "/downloads/book.txt",
        headers=((b"range", b"bytes=zz"),),
    )

    assert status == 400
    assert headers["cache-control"] == CACHE_CONTROL_NO_STORE


@pytest.mark.asyncio
async def test_head_with_range_returns_206_without_a_body(
    export_dir: Path, make_app: Any
) -> None:
    async with _client(make_app()) as client:
        response = await client.head(
            "/downloads/book.txt", headers={"Range": "bytes=0-1"}
        )

    assert response.status_code == 206
    assert response.content == b""
    assert response.headers["content-length"] == "2"
    assert response.headers["content-range"] == "bytes 0-1/5"


@pytest.mark.asyncio
async def test_unsupported_method_is_405_with_allow(
    export_dir: Path, make_app: Any
) -> None:
    async with _client(make_app()) as client:
        response = await client.post("/downloads/book.txt")

    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_large_download_is_streamed_in_chunks(
    tmp_path: Path, make_app: Any
) -> None:
    """
    A large export must not be read into one buffer before sending.

    ``FileResponse`` reads ``chunk_size`` (64 KiB) at a time and emits one
    ``http.response.body`` message per chunk. A single body message covering
    the whole file would mean the file was buffered in memory first.
    """
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    size = 200 * 1024
    (downloads / "big.txt").write_bytes(b"x" * size)

    status, headers, body, chunks = await _raw(make_app(), "/downloads/big.txt")

    assert status == 200
    assert len(body) == size
    assert int(headers["content-length"]) == size
    assert len(chunks) > 1
    assert max(len(chunk) for chunk in chunks) <= 64 * 1024
    assert all(len(chunk) < size for chunk in chunks)


def test_embedded_large_download_is_written_in_chunks(
    embedded: SimpleNamespace, tmp_path: Path
) -> None:
    """
    The embedded handler copies in 64 KiB reads instead of slurping the file.

    Its ``Content-Length`` is announced first, so the client can read the body
    incrementally; the response must also be byte-complete (a short copy would
    desynchronise a keep-alive connection).
    """
    size = 200 * 1024
    (embedded.downloads / "big.txt").write_bytes(b"y" * size)

    status, headers, body = _embedded(
        embedded.port,
        "GET",
        "/downloads/big.txt",
        {"Authorization": f"Bearer {TOKEN}"},
    )

    assert status == 200
    assert int(headers["content-length"]) == size
    assert body == b"y" * size


# ---------------------------------------------------------------------------
# Embedded engine: parity and recorded differences
# ---------------------------------------------------------------------------


@pytest.fixture
def embedded(
    tmp_path: Path, export_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SimpleNamespace]:
    """Start the embedded engine on a loopback port over the fixture dir."""
    home = tmp_path / "embedded_home"
    home.mkdir()

    async def noop_start() -> None:
        return None

    monkeypatch.setattr(manager, "start", noop_start)
    monkeypatch.setattr(history_files, "_output_dir", lambda: export_dir)
    monkeypatch.setenv("NOVEL_HOME", str(home))

    original_cwd = os.getcwd()
    port = eserver.start_server(str(home), 0, token=TOKEN)
    try:
        yield SimpleNamespace(port=port, home=home, downloads=export_dir)
    finally:
        eserver.stop_server()
        os.chdir(original_cwd)


def _embedded(
    port: int,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """One request against the real embedded listener."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        raw = response.read()
        return (
            response.status,
            {name.lower(): value for name, value in response.getheaders()},
            raw,
        )
    finally:
        connection.close()


def test_embedded_requires_the_token(embedded: SimpleNamespace) -> None:
    status, headers, body = _embedded(embedded.port, "GET", "/downloads/book.txt")
    assert status == 401
    assert headers["www-authenticate"] == "Bearer"
    assert b"hello" not in body

    status, _, body = _embedded(
        embedded.port,
        "GET",
        "/downloads/book.txt",
        {"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 200
    assert body == b"hello"


@pytest.mark.parametrize(
    "path",
    [
        "/downloads/../secret.txt",
        "/downloads/%2e%2e/secret.txt",
        "/downloads/..%2fsecret.txt",
        "/downloads/..%5csecret.txt",
        "/downloads/C:%5cWindows%5cwin.ini",
        "/downloads//etc/passwd",
        "/downloads/book.txt%00.txt",
        "/downloads/sub",
        "/downloads/sub/",
        "/downloads/missing.txt",
    ],
)
def test_embedded_refuses_traversal_and_missing_files(
    embedded: SimpleNamespace, path: str
) -> None:
    status, headers, body = _embedded(
        embedded.port, "GET", path, {"Authorization": f"Bearer {TOKEN}"}
    )

    assert status in REFUSED, (path, status)
    assert SECRET_MARKER.encode() not in body, path
    assert b"inner.txt" not in body, path
    assert headers["content-type"].startswith("application/json")


def test_embedded_serves_the_shared_header_contract(
    embedded: SimpleNamespace,
) -> None:
    """Headers the two transports share must stay identical."""
    status, headers, body = _embedded(
        embedded.port, "GET", "/downloads/book.txt", {"X-API-Key": TOKEN}
    )

    assert status == 200
    assert body == b"hello"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["content-security-policy"] == DOWNLOADS_CSP
    assert headers["x-request-id"].strip()
    assert int(headers["content-length"]) == len(body)


def test_embedded_differences_from_rest_are_recorded(
    embedded: SimpleNamespace,
) -> None:
    """
    Documented gaps of the stdlib engine.

    * no ``Cache-Control`` (native clients, no HTTP cache);
    * no ``Range`` support and no ``Accept-Ranges`` advertisement;
    * always ``application/octet-stream`` and an ``attachment`` disposition;
    * ``/downloads`` (no trailing slash) is a 405 there, a 307 redirect in
      REST (Starlette's mount redirect).
    """
    headers = {"Authorization": f"Bearer {TOKEN}"}

    status, download_headers, body = _embedded(
        embedded.port, "GET", "/downloads/book.txt", headers
    )
    assert status == 200
    assert "cache-control" not in download_headers
    assert "accept-ranges" not in download_headers
    assert download_headers["content-type"] == "application/octet-stream"
    assert download_headers["content-disposition"] == 'attachment; filename="book.txt"'

    ranged_status, ranged_headers, ranged_body = _embedded(
        embedded.port, "GET", "/downloads/book.txt", {**headers, "Range": "bytes=0-2"}
    )
    assert ranged_status == 200
    assert ranged_body == body == b"hello"
    assert "content-range" not in ranged_headers

    bare_status, bare_headers, _ = _embedded(
        embedded.port, "GET", "/downloads", headers
    )
    assert bare_status == 405
    assert bare_headers["allow"] == "GET, HEAD"


def test_encoded_slash_difference_is_recorded(embedded: SimpleNamespace) -> None:
    """
    Recorded difference: ``%2f`` inside the name.

    The REST mount decodes the path before matching, so
    ``/downloads%2fbook.txt`` reaches the static app and is served. The
    embedded router matches the *undecoded* path, so that spelling misses the
    ``/downloads/`` prefix and is a 404. Neither leaks anything (the name is
    still reduced to one component), and no client builds such a URL — the
    handlers emit ``quote(..., safe="")``, which never produces ``%2f``.
    """
    headers = {"Authorization": f"Bearer {TOKEN}"}

    status, _, _ = _embedded(embedded.port, "GET", "/downloads%2fbook.txt", headers)
    assert status == 404

    # The ordinary encoded-name form works on both transports.
    status, _, body = _embedded(embedded.port, "GET", "/downloads/book%2Etxt", headers)
    assert status == 200
    assert body == b"hello"


def test_embedded_head_has_no_body(embedded: SimpleNamespace) -> None:
    status, headers, body = _embedded(
        embedded.port,
        "HEAD",
        "/downloads/book.txt",
        {"Authorization": f"Bearer {TOKEN}"},
    )

    assert status == 200
    assert body == b""
    assert headers["content-length"] == "5"


@pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="Windows device names are a Windows-only filesystem quirk",
)
def test_embedded_does_not_serve_device_names(
    embedded: SimpleNamespace, tmp_path: Path
) -> None:
    """
    Regression: ``NUL.txt`` / ``CON.txt`` used to be a ``200`` with no body.

    ``stat`` reports these as zero-length character devices, so the old
    ``st_size`` check let them through as an empty download. They must be a
    plain 404 like any other non-regular file.
    """
    for name in ("NUL.txt", "CON.txt"):
        status, _, body = _embedded(
            embedded.port,
            "GET",
            f"/downloads/{name}",
            {"Authorization": f"Bearer {TOKEN}"},
        )
        assert status == 404, name
        assert json.loads(body)["detail"] == "Not Found"


def test_embedded_does_not_serve_a_directory_named_like_a_file(
    embedded: SimpleNamespace,
) -> None:
    """Regression: ``folder.txt`` was a ``200`` with a zero-length body."""
    (embedded.downloads / "folder.txt").mkdir()

    status, _, body = _embedded(
        embedded.port,
        "GET",
        "/downloads/folder.txt",
        {"Authorization": f"Bearer {TOKEN}"},
    )

    assert status == 404
    assert json.loads(body)["detail"] == "Not Found"


def test_embedded_nul_byte_is_404_not_500(embedded: SimpleNamespace) -> None:
    """Regression: ``%00`` raised ``ValueError`` and escaped as a 500."""
    status, _, body = _embedded(
        embedded.port,
        "GET",
        "/downloads/book.txt%00.txt",
        {"Authorization": f"Bearer {TOKEN}"},
    )

    assert status == 404
    assert json.loads(body)["detail"] == "Not Found"


def test_embedded_link_escaping_the_export_directory_is_not_followed(
    embedded: SimpleNamespace, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.txt").write_text(SECRET_MARKER, encoding="utf-8")
    if not _make_link(embedded.downloads / "escape", outside):
        pytest.skip("directory links are not constructible here")

    status, _, body = _embedded(
        embedded.port,
        "GET",
        "/downloads/escape/leak.txt",
        {"Authorization": f"Bearer {TOKEN}"},
    )

    assert status in REFUSED
    assert SECRET_MARKER.encode() not in body
