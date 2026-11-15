#!/usr/bin/env python3
"""
tests.apps.test_api_request_id_contract
---------------------------------------

Anti-drift tests for the ``X-Request-ID`` contract shared by the OpenAPI
document, the FastAPI runtime and the Android embedded engine.

The REST middleware sanitises the header before routing, the OpenAPI document
advertises the same rules, and the embedded stdlib server mirrors them.  These
tests pin the three implementations together:

* ``components.parameters.RequestId`` and ``components.headers.RequestId``
  exist and describe the optional request header / always-present response
  header without promising a ``422`` that the runtime never sends;
* every documented operation references both components, including the search,
  tasks, login and SSE endpoints;
* a safe client id is echoed verbatim, while empty, overlong, control-character
  and non-ASCII values are replaced by a UUID4 on JSON and SSE responses;
* the embedded server answers the same way for the same raw header bytes.

No network access: REST requests use ``httpx.ASGITransport`` or a direct ASGI
call, embedded requests use a loopback socket created by the test itself.
"""

from __future__ import annotations

import asyncio
import re
import socket
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api.middleware import (
    MAX_REQUEST_ID_LENGTH,
    REQUEST_ID_HEADER,
)
from novel_downloader.apps.core.task_manager import manager
from novel_downloader.apps.embedded import server as eserver

#: Local JSON-pointer refs the generator is expected to use.
PARAMETER_REF = "#/components/parameters/RequestId"
RESPONSE_HEADER_REF = "#/components/headers/RequestId"

#: Methods that may appear in an OpenAPI path item.
HTTP_METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)

#: A server-generated id is a canonical lowercase UUID4.
UUID4_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)

#: The raw-byte form of a safe client-supplied id (same rule as both servers).
SAFE_ID_RE = re.compile(
    rf"[A-Za-z0-9._:-]{{1,{MAX_REQUEST_ID_LENGTH}}}".encode("ascii")
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep application state/config outside the real user profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)
    monkeypatch.delenv("NOVEL_API_WEBHOOK", raising=False)


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    """The real REST application, with exports written to a temp directory."""
    return create_app(output_dir=tmp_path / "downloads", rate_limit=None)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


def _operations(spec: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield ``(path, method, operation)`` for every documented operation."""
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method in HTTP_METHODS and isinstance(operation, dict):
                yield path, method, operation


def _resolve_ref(spec: dict[str, Any], ref: str) -> dict[str, Any]:
    """Resolve a local ``#/...`` JSON pointer reference."""
    assert ref.startswith("#/"), ref
    node: Any = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        assert isinstance(node, dict) and part in node, f"unresolvable ref {ref}"
        node = node[part]
    assert isinstance(node, dict)
    return node


def _request_id_parameters(
    spec: dict[str, Any], operation: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return every resolved ``X-Request-ID`` request parameter."""
    resolved: list[dict[str, Any]] = []
    for parameter in operation.get("parameters", []):
        if "$ref" in parameter:
            parameter = _resolve_ref(spec, parameter["$ref"])
        if (
            parameter.get("in") == "header"
            and str(parameter.get("name", "")).lower() == REQUEST_ID_HEADER.lower()
        ):
            resolved.append(parameter)
    return resolved


def _response_header(
    spec: dict[str, Any], response: dict[str, Any]
) -> dict[str, Any] | None:
    """Return the resolved ``X-Request-ID`` response header for one response."""
    for name, header in response.get("headers", {}).items():
        if name.lower() != REQUEST_ID_HEADER.lower():
            continue
        if "$ref" in header:
            return _resolve_ref(spec, header["$ref"])
        return header
    return None


def _asgi_scope(
    path: str, query: str, raw_headers: list[tuple[bytes, bytes]]
) -> dict[str, Any]:
    """Minimal HTTP scope for a direct ASGI call (no socket, no test client)."""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": list(raw_headers),
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }


async def _asgi_start(
    app: FastAPI, target: str, raw_headers: list[tuple[bytes, bytes]]
) -> tuple[int, dict[str, str]]:
    """
    Drive the real app until ``http.response.start`` arrives.

    The second ``receive`` returns ``http.disconnect`` so an infinite SSE
    stream is cancelled by Starlette instead of hanging the test.  This keeps
    the raw header bytes under test control (a control character or non-ASCII
    byte cannot be sent through ``httpx``) while still exercising the complete
    middleware stack, not a hand-built pipeline.
    """
    sent: list[dict[str, Any]] = []
    received = False

    async def receive() -> dict[str, Any]:
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    path, _, query = target.partition("?")
    await asyncio.wait_for(
        app(_asgi_scope(path, query, raw_headers), receive, send), timeout=10
    )
    start = next(
        message for message in sent if message["type"] == "http.response.start"
    )
    headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in start["headers"]
    }
    return int(start["status"]), headers


def _embedded_request(
    port: int,
    path: str,
    raw_header: bytes | None,
) -> tuple[int, dict[str, str], bytes]:
    """One raw HTTP request to the embedded server, with exact header bytes."""
    request = (
        f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n"
    ).encode("ascii")
    if raw_header is not None:
        request += b"X-Request-ID: " + raw_header + b"\r\n"
    request += b"\r\n"

    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(request)
        chunks = bytearray()
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.extend(chunk)

    head, _, body = bytes(chunks).partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ")[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(b":")
        if not sep:
            continue
        key = name.decode("latin-1").strip().lower()
        headers.setdefault(key, value.decode("latin-1").strip())
    return status, headers, body


@pytest.fixture
def embedded_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SimpleNamespace]:
    """Start the embedded server with a no-op task manager and a temp home."""
    home = tmp_path / "embedded_home"
    home.mkdir()
    original_cwd = Path.cwd()

    async def noop_start() -> None:
        return None

    monkeypatch.setattr(manager, "start", noop_start)
    monkeypatch.setenv("NOVEL_HOME", str(home))
    port = eserver.start_server(str(home), 0)
    try:
        yield SimpleNamespace(port=port)
    finally:
        eserver.stop_server()
        monkeypatch.chdir(original_cwd)


# ---------------------------------------------------------------------------
# OpenAPI components
# ---------------------------------------------------------------------------


def test_openapi_declares_request_id_components(app: FastAPI) -> None:
    """Both reusable components exist with the documented header contract."""
    spec = app.openapi()
    components = spec["components"]

    parameter = components["parameters"]["RequestId"]
    assert parameter["name"] == REQUEST_ID_HEADER
    assert parameter["in"] == "header"
    assert parameter["required"] is False
    assert parameter["schema"] == {"type": "string"}
    # The middleware replaces unsafe values; it never answers 422 for them, so
    # the schema must not advertise a validating pattern or length bound.
    assert "pattern" not in parameter["schema"]
    assert "maxLength" not in parameter["schema"]
    description = str(parameter["description"])
    assert str(MAX_REQUEST_ID_LENGTH) in description
    assert "optional" in description.lower()
    assert "echoed" in description.lower()
    assert "uuid4" in description.lower()

    response_header = components["headers"]["RequestId"]
    assert response_header["schema"] == {"type": "string"}
    description = str(response_header["description"])
    assert "x-request-id" in description.lower()
    assert "uuid4" in description.lower()


def test_every_operation_references_the_request_id_components(
    app: FastAPI,
) -> None:
    """No operation may inline or omit the shared parameter/response header."""
    spec = app.openapi()
    problems: list[str] = []

    for path, method, operation in _operations(spec):
        label = f"{method.upper()} {path}"
        parameter_refs = [
            parameter.get("$ref")
            for parameter in operation.get("parameters", [])
            if isinstance(parameter, dict)
        ]
        if parameter_refs.count(PARAMETER_REF) != 1:
            problems.append(f"{label}: request parameter ref")
        if len(_request_id_parameters(spec, operation)) != 1:
            problems.append(f"{label}: request parameter documented")

        for code, response in operation["responses"].items():
            raw_headers = response.get("headers", {})
            header = raw_headers.get(REQUEST_ID_HEADER)
            if header != {"$ref": RESPONSE_HEADER_REF}:
                problems.append(f"{label} {code}: response header ref")
            if _response_header(spec, response) is None:
                problems.append(f"{label} {code}: response header documented")

    assert not problems, "\n".join(problems)


def test_required_endpoints_are_covered(app: FastAPI) -> None:
    """Pin the route families named by the contract (search/tasks/login/SSE)."""
    spec = app.openapi()
    required = {
        ("get", "/api/v1/search/stream"),
        ("post", "/api/v1/search"),
        ("get", "/api/v1/tasks"),
        ("post", "/api/v1/tasks"),
        ("get", "/api/v1/tasks/stream"),
        ("get", "/api/v1/login/requests"),
        ("get", "/api/v1/login/requests/stream"),
    }
    documented = {(method, path) for path, method, _operation in _operations(spec)}

    assert required <= documented
    for method, path in required:
        operation = spec["paths"][path][method]
        refs = [
            parameter.get("$ref")
            for parameter in operation["parameters"]
            if isinstance(parameter, dict)
        ]
        assert refs.count(PARAMETER_REF) == 1, f"{method.upper()} {path}"
        assert operation["responses"]["200"]["headers"][REQUEST_ID_HEADER] == {
            "$ref": RESPONSE_HEADER_REF
        }, f"{method.upper()} {path}"


def test_sse_operations_document_the_event_stream_and_the_header(
    app: FastAPI,
) -> None:
    """SSE responses are not JSON; the header ref must still be attached."""
    spec = app.openapi()
    for path in (
        "/api/v1/search/stream",
        "/api/v1/tasks/stream",
        "/api/v1/login/requests/stream",
    ):
        response = spec["paths"][path]["get"]["responses"]["200"]
        assert "text/event-stream" in response["content"]
        assert response["headers"][REQUEST_ID_HEADER] == {"$ref": RESPONSE_HEADER_REF}


# ---------------------------------------------------------------------------
# Runtime: REST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_id_is_echoed_on_success_and_validation_errors(
    app: FastAPI,
) -> None:
    spec = app.openapi()
    safe_id = "contract-check_1.2:3"

    async with _client(app) as client:
        healthy = await client.get(
            "/api/v1/health", headers={REQUEST_ID_HEADER: safe_id}
        )
        invalid = await client.post(
            "/api/v1/search", json={}, headers={REQUEST_ID_HEADER: safe_id}
        )

    assert healthy.status_code == 200
    assert invalid.status_code == 422
    for response in (healthy, invalid):
        assert response.headers["x-request-id"] == safe_id
        code = str(response.status_code)
        header = _response_header(
            spec, spec["paths"]["/api/v1/health"]["get"]["responses"][code]
        )
        if header is None:
            header = _response_header(
                spec, spec["paths"]["/api/v1/search"]["post"]["responses"][code]
            )
        assert header is not None
        assert header["schema"] == {"type": "string"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/search/stream?keyword=x&sites=",
        "/api/v1/tasks/stream",
        "/api/v1/login/requests/stream",
    ],
)
async def test_every_sse_endpoint_carries_the_response_header(
    app: FastAPI, path: str
) -> None:
    status, headers = await _asgi_start(
        app, path, [(b"x-request-id", b"sse-request-1")]
    )

    assert status == 200
    assert headers["x-request-id"] == "sse-request-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_header",
    [
        b"",
        b"a" * (MAX_REQUEST_ID_LENGTH + 1),
        b"control\x01character",
        b"caf\xc3\xa9",
        b"\xff\xfe",
    ],
    ids=["empty", "overlong", "control", "utf8", "non-ascii"],
)
async def test_invalid_ids_are_replaced_with_a_uuid4(
    app: FastAPI, raw_header: bytes
) -> None:
    status, headers = await _asgi_start(
        app, "/api/v1/health", [(b"x-request-id", raw_header)]
    )

    assert status == 200
    generated = headers["x-request-id"]
    assert UUID4_RE.fullmatch(generated), generated
    assert generated.encode("ascii") != raw_header


@pytest.mark.asyncio
async def test_missing_id_is_replaced_with_a_uuid4(app: FastAPI) -> None:
    status, headers = await _asgi_start(app, "/api/v1/health", [])

    assert status == 200
    assert UUID4_RE.fullmatch(headers["x-request-id"])


# ---------------------------------------------------------------------------
# Runtime: embedded/REST parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_header",
    [
        b"trace-abc_1.2:3",
        b"",
        b"a" * (MAX_REQUEST_ID_LENGTH + 1),
        b"control\x01character",
        b"caf\xc3\xa9",
    ],
    ids=["safe", "empty", "overlong", "control", "utf8"],
)
async def test_embedded_server_matches_the_rest_contract(
    app: FastAPI, embedded_server: SimpleNamespace, raw_header: bytes
) -> None:
    rest_status, rest_headers = await _asgi_start(
        app, "/api/v1/health", [(b"x-request-id", raw_header)]
    )
    embedded_status, embedded_headers, _body = await asyncio.to_thread(
        _embedded_request, embedded_server.port, "/api/v1/health", raw_header
    )

    assert rest_status == embedded_status == 200
    rest_id = rest_headers["x-request-id"]
    embedded_id = embedded_headers["x-request-id"]

    if SAFE_ID_RE.fullmatch(raw_header):
        # A safe value is echoed verbatim by both transports.
        assert rest_id == embedded_id == raw_header.decode("ascii")
    else:
        # Unsafe values are independently replaced by a generated UUID4.
        assert UUID4_RE.fullmatch(rest_id), rest_id
        assert UUID4_RE.fullmatch(embedded_id), embedded_id
