#!/usr/bin/env python3
"""
tests.apps.test_openapi_auth
----------------------------

Contract tests for how the REST API declares authentication in OpenAPI.

The document must describe the two header forms
:mod:`novel_downloader.apps.api.auth` actually accepts:

* ``Authorization: Bearer <token>`` -> ``BearerAuth`` (``http``/``bearer``);
* ``X-API-Key: <token>`` -> ``ApiKeyAuth`` (``apiKey`` in a header).

``security`` is declared per operation, never document-wide: an operation is
marked protected only when its FastAPI dependency tree contains
``require_token``. The tests derive that set from ``app.routes`` and the
``Dependant`` tree rather than from a hardcoded path list, so moving a route
between routers updates both the document and this gate.

Public surfaces (``/docs``, ``/redoc``, ``/openapi.json`` and the
``/downloads`` mount) are outside the ``/api/v1`` router; the tests pin their
runtime behaviour separately. The Android/desktop embedded engine is
loopback-only and does not implement token authentication, so it never returns
``401`` -- that is asserted too, because the REST contract does not apply to
it.

No network access: the REST schema is generated in-process, HTTP checks use
``httpx.ASGITransport`` and the embedded engine binds ``127.0.0.1``.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api.app import (
    AUTH_SECURITY_REQUIREMENT,
    _install_openapi_metadata,
)
from novel_downloader.apps.api.auth import (
    AUTH_ERROR_DETAIL,
    require_token,
)
from novel_downloader.apps.embedded import server as embedded_server

#: Methods that may appear in an OpenAPI path item.
HTTP_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
)

#: FastAPI documentation routes; served outside the protected router.
DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")

#: Both header forms are alternatives, so they share one requirement list.
EXPECTED_SECURITY: list[dict[str, list[str]]] = [
    {"BearerAuth": []},
    {"ApiKeyAuth": []},
]


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep config/state outside the real profile and drop stray tokens."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)


@pytest.fixture
def app(tmp_path: Any) -> FastAPI:
    """Build the API app without touching the real download directory."""
    return create_app(output_dir=tmp_path / "downloads")


@pytest.fixture
def spec(app: FastAPI) -> dict[str, Any]:
    """Return the generated OpenAPI document."""
    return app.openapi()


def _operations(spec: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield ``(path, method, operation)`` for every documented operation."""
    for path, item in spec.get("paths", {}).items():
        for method, operation in item.items():
            if method in HTTP_METHODS and isinstance(operation, dict):
                yield path, method, operation


def _operation(spec: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    """Return one documented operation, asserting it exists."""
    operation = spec["paths"][path][method]
    assert isinstance(operation, dict)
    return operation


def _walk_dependants(dependant: Any) -> Iterator[Any]:
    """Yield a FastAPI dependant and every dependency below it."""
    pending = [dependant]
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        pending.extend(getattr(node, "dependencies", ()) or ())


def _iter_api_routes(routes: Any) -> Iterator[Any]:
    """Yield routes that expose ``path``/``methods``/``dependant``.

    ``include_router`` is a flat list in some FastAPI versions and a lazy
    ``_IncludedRouter`` in others; both end up as ``effective_candidates()``.
    """
    for route in routes:
        if getattr(route, "dependant", None) is not None and hasattr(route, "path"):
            yield route
            continue
        candidates = getattr(route, "effective_candidates", None)
        if callable(candidates):
            yield from _iter_api_routes(candidates())


def _protected_endpoints(app: FastAPI) -> set[tuple[str, str]]:
    """Derive protected ``(path, method)`` pairs from the dependency tree."""
    protected: set[tuple[str, str]] = set()
    for route in _iter_api_routes(app.routes):
        guarded = any(
            getattr(node, "call", None) is require_token
            for node in _walk_dependants(route.dependant)
        )
        if not guarded:
            continue
        for method in getattr(route, "methods", ()) or ():
            protected.add((route.path, method.lower()))
    return protected


def _embedded_request(
    port: int, path: str, headers: dict[str, str] | None = None
) -> tuple[int, bytes]:
    """One loopback request to the embedded engine (no redirects, no retries)."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers=headers or {}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


# ---------------------------------------------------------------------------
# Security schemes
# ---------------------------------------------------------------------------


def test_security_schemes_describe_both_accepted_headers(spec: dict[str, Any]) -> None:
    """The schemes must mirror what ``auth.extract_token`` reads at runtime."""
    schemes = spec["components"]["securitySchemes"]

    bearer = schemes["BearerAuth"]
    assert bearer["type"] == "http"
    assert bearer["scheme"] == "bearer"
    assert str(bearer.get("description", "")).strip()

    api_key = schemes["ApiKeyAuth"]
    assert api_key["type"] == "apiKey"
    assert api_key["in"] == "header"
    assert api_key["name"] == "X-API-Key"
    assert str(api_key.get("description", "")).strip()


def test_security_requirement_offers_both_schemes_as_alternatives() -> None:
    """One requirement list means OR: either header form authenticates."""
    assert AUTH_SECURITY_REQUIREMENT == EXPECTED_SECURITY
    assert len(AUTH_SECURITY_REQUIREMENT) == 2


# ---------------------------------------------------------------------------
# Per-operation declaration
# ---------------------------------------------------------------------------


def test_protected_endpoints_are_derived_from_the_dependency_tree(
    app: FastAPI,
) -> None:
    """``require_token`` is found on the real router, not assumed from paths."""
    protected = _protected_endpoints(app)

    assert protected, "the dependency walker found no protected route"
    # Health is not public: it sits inside the token-protected /api/v1 router.
    assert ("/api/v1/health", "get") in protected
    assert ("/api/v1/search", "post") in protected


def test_protected_operations_declare_both_schemes(
    spec: dict[str, Any], app: FastAPI
) -> None:
    """Every operation behind ``require_token`` carries the requirement."""
    protected = _protected_endpoints(app)
    documented = {(path, method) for path, method, _op in _operations(spec)}
    protected_documented = protected & documented

    assert protected_documented, "no protected operation appears in the document"
    for path, method in sorted(protected_documented):
        operation = _operation(spec, path, method)
        assert operation.get("security") == EXPECTED_SECURITY, (
            f"{method.upper()} {path}"
        )


def test_public_operations_declare_no_security(
    spec: dict[str, Any], app: FastAPI
) -> None:
    """Anything outside the protected dependency tree must be explicit."""
    protected = _protected_endpoints(app)

    for path, method, operation in _operations(spec):
        if (path, method) in protected:
            continue
        assert operation.get("security", []) == [], f"{method.upper()} {path}"


def test_document_has_no_global_security_default(spec: dict[str, Any]) -> None:
    """A root requirement would silently protect future public operations."""
    assert "security" not in spec


def test_mixed_router_derives_security_per_operation() -> None:
    """A public route added beside the protected router stays public."""
    application = FastAPI()
    protected_router = APIRouter(
        prefix="/api/v1", dependencies=[Depends(require_token)]
    )

    @protected_router.get("/protected")
    async def protected_endpoint() -> dict[str, bool]:
        return {"ok": True}

    @application.get("/public")
    async def public_endpoint() -> dict[str, bool]:
        return {"ok": True}

    application.include_router(protected_router)
    _install_openapi_metadata(application)
    document = application.openapi()

    assert (
        document["paths"]["/api/v1/protected"]["get"]["security"] == EXPECTED_SECURITY
    )
    assert document["paths"]["/public"]["get"]["security"] == []


# ---------------------------------------------------------------------------
# 401 documentation
# ---------------------------------------------------------------------------


def test_protected_operations_document_401(spec: dict[str, Any], app: FastAPI) -> None:
    """Clients learn the failure mode and both accepted headers from the doc."""
    protected = _protected_endpoints(app)
    documented = {(path, method) for path, method, _op in _operations(spec)}
    assert protected & documented

    for path, method in sorted(protected & documented):
        response = _operation(spec, path, method)["responses"].get("401")
        assert response is not None, f"{method.upper()} {path} documents no 401"
        description = str(response.get("description", ""))
        assert "Authorization: Bearer" in description
        assert "X-API-Key" in description
        schema = response["content"]["application/json"]["schema"]
        assert schema["properties"]["detail"]["type"] == "string"


def test_public_operations_do_not_document_401(
    spec: dict[str, Any], app: FastAPI
) -> None:
    """A public operation must not advertise an authentication failure."""
    protected = _protected_endpoints(app)

    for path, method, operation in _operations(spec):
        if (path, method) in protected:
            continue
        assert "401" not in operation["responses"], f"{method.upper()} {path}"


# ---------------------------------------------------------------------------
# Public surfaces outside the router
# ---------------------------------------------------------------------------


def test_docs_paths_are_not_protected_operations(spec: dict[str, Any]) -> None:
    """Documentation routes are not part of the schema at all."""
    for path in DOCS_PATHS:
        assert path not in spec["paths"]


@pytest.mark.asyncio
async def test_docs_paths_stay_public_when_a_token_is_configured(
    tmp_path: Any,
) -> None:
    """Reading the schema never requires the token it describes."""
    app = create_app(output_dir=tmp_path / "downloads", token="secret-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        for path in DOCS_PATHS:
            response = await client.get(path)
            assert response.status_code == 200, path


@pytest.mark.asyncio
async def test_downloads_mount_requires_the_token_but_is_not_in_openapi(
    tmp_path: Any,
) -> None:
    """The static mount is protected by middleware, not a router dependency."""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "book.txt").write_text("hello", encoding="utf-8")
    app = create_app(output_dir=downloads, token="secret-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        unauthorised = await client.get("/downloads/book.txt")
        authorised = await client.get(
            "/downloads/book.txt", headers={"X-API-Key": "secret-token"}
        )

    assert unauthorised.status_code == 401
    assert authorised.status_code == 200
    assert "/downloads/book.txt" not in app.openapi()["paths"]


# ---------------------------------------------------------------------------
# Embedded engine
# ---------------------------------------------------------------------------


def test_embedded_engine_has_no_token_authentication(tmp_path: Any) -> None:
    """The embedded engine is loopback-only and never returns 401.

    ``start_server`` accepts no token and the handler chain has no auth layer,
    matching the documentation note ("embedded engine ... binds 127.0.0.1 only
    and serves native clients"). The REST ``401`` contract does not apply, so
    bogus credentials are ignored rather than rejected.
    """
    home = tmp_path / "embedded-home"
    home.mkdir()
    original_cwd = os.getcwd()
    port = embedded_server.start_server(str(home), 0)
    try:
        plain_status, plain_body = _embedded_request(port, "/api/v1/health")
        bogus_status, bogus_body = _embedded_request(
            port,
            "/api/v1/health",
            headers={
                "Authorization": "Bearer not-the-token",
                "X-API-Key": "not-the-token",
            },
        )
    finally:
        embedded_server.stop_server()
        # ``_prepare_home`` chdirs into the app home; restore it so later
        # tests never run from a temporary directory.
        os.chdir(original_cwd)

    assert plain_status == 200
    assert bogus_status == 200
    assert bogus_body == plain_body
    assert b"Invalid or missing API token" not in bogus_body
    assert AUTH_ERROR_DETAIL.encode() not in bogus_body
