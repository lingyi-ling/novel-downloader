#!/usr/bin/env python3
"""
tests.apps.test_api_openapi
---------------------------

Offline validation of the OpenAPI document FastAPI generates for the REST API.

The document is treated as a contract:

* it must serialise as strict JSON (no ``NaN``, no circular references) and
  declare a supported OpenAPI version;
* every registered ``/api/v1`` route must appear exactly once, with a unique
  ``operationId``, a summary, a description and a documented success response;
* the shared ``401`` / ``422`` / ``429`` error responses must carry
  descriptions, because clients switch on them;
* the declared security schemes must match the headers
  :mod:`novel_downloader.apps.api.auth` actually accepts;
* the key request fields of the search / task / music schemas must be present;
* ``/docs``, ``/redoc`` and ``/openapi.json`` must follow ``expose_docs``.

No network access: the schema is generated in-process and the HTTP checks use
``httpx.ASGITransport``.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from novel_downloader import __version__
from novel_downloader.apps.api import create_app
from novel_downloader.apps.api.auth import AUTH_ERROR_DETAIL
from novel_downloader.apps.api.middleware import RATE_LIMIT_EXEMPT_PATHS
from novel_downloader.apps.api.routes import OPENAPI_TAGS

#: Methods that may appear in an OpenAPI path item.
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head")

#: Request/response fields clients depend on. Each is checked against the
#: generated component schema so a renamed or dropped field is caught here
#: instead of by the desktop, CLI or Android clients.
EXPECTED_SCHEMA_FIELDS: dict[str, set[str]] = {
    "SearchRequest": {
        "keyword",
        "sites",
        "per_site_limit",
        "timeout",
        "sort",
        "order",
        "limit",
        "group",
    },
    "BrowseRequest": {"site", "category_id", "gender", "limit"},
    "TaskCreateRequest": {"title", "site", "book_id"},
    "WatchlistAddRequest": {"site", "book_id", "title"},
    "WatchlistCheckRequest": {"download"},
    "LoginSubmitRequest": {"values"},
    "MusicSearchRequest": {"platform", "keyword", "limit", "offset"},
    "MusicDownloadRequest": {
        "platform",
        "tracks",
        "search_id",
        "level",
        "out_dir",
    },
    "MusicExportRequest": {"platform", "tracks", "out_path"},
    "MusicTrack": {
        "provider",
        "id",
        "name",
        "artists",
        "album",
        "duration_s",
        "cover_url",
        "download_url",
        "http_headers",
        "path",
        "lyric",
        "sources",
    },
}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep config/state outside the real user profile and drop stray tokens."""
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
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method in HTTP_METHODS:
                yield path, method, operation


def _member_schema(spec: dict[str, Any], name: str) -> dict[str, Any]:
    """Return one component schema, following ``$ref`` when needed."""
    schema = spec["components"]["schemas"][name]
    ref = schema.get("$ref")
    if ref:
        return _resolve_ref(spec, ref)
    return schema


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


def _iter_app_routes(
    routes: Any,
) -> Iterator[tuple[str, set[str], bool]]:
    """Yield ``(path, methods, include_in_schema)`` from ``app.routes``.

    FastAPI versions differ in how ``include_router`` is represented (a flat
    list of ``APIRoute`` objects versus a lazily expanded included router), so
    both shapes are walked.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route.path, route.methods, route.include_in_schema
            continue
        candidates = getattr(route, "effective_candidates", None)
        if callable(candidates):
            yield from _iter_app_routes(candidates())
        elif getattr(route, "methods", None):
            yield (
                route.path,
                route.methods,
                getattr(route, "include_in_schema", True),
            )


def _iter_refs(value: Any) -> Iterator[str]:
    """Yield every ``$ref`` string in a nested document."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "$ref":
                assert isinstance(item, str)
                yield item
            else:
                yield from _iter_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_refs(item)


def _iter_numbers(value: Any) -> Iterator[float]:
    """Yield every numeric default/example in a nested document."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"default", "example"} and isinstance(item, (int, float)):
                if not isinstance(item, bool):
                    yield float(item)
            else:
                yield from _iter_numbers(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_numbers(item)


# ---------------------------------------------------------------------------
# Document integrity
# ---------------------------------------------------------------------------


def test_document_serialises_as_strict_json(spec: dict[str, Any]) -> None:
    """``NaN``/``Infinity`` and reference cycles would break every client."""
    # ``allow_nan=False`` rejects non-finite floats and a circular reference
    # raises "Circular reference detected" instead of recursing forever.
    text = json.dumps(spec, ensure_ascii=False, allow_nan=False)

    assert json.loads(text) == spec


def test_openapi_version_is_supported(spec: dict[str, Any]) -> None:
    version = spec["openapi"]

    assert re.fullmatch(r"3\.\d+\.\d+", version), version
    major, minor, _patch = (int(part) for part in version.split("."))
    assert major == 3
    assert minor >= 1, "OpenAPI 3.1 is required for JSON Schema null unions"


def test_info_matches_the_application(spec: dict[str, Any], app: FastAPI) -> None:
    info = spec["info"]

    assert info["title"] == app.title
    assert info["version"] == __version__
    assert info["description"].strip()
    assert app.version == __version__


def test_every_ref_resolves(spec: dict[str, Any]) -> None:
    refs = list(_iter_refs(spec))
    assert refs, "the document should reference component schemas"

    for ref in refs:
        _resolve_ref(spec, ref)


def test_no_nan_or_infinite_defaults(spec: dict[str, Any]) -> None:
    offenders = [value for value in _iter_numbers(spec) if not math.isfinite(value)]

    assert not offenders, f"non-finite defaults/examples: {offenders}"


# ---------------------------------------------------------------------------
# Routes and operations
# ---------------------------------------------------------------------------


def test_documented_routes_match_registered_routes(
    spec: dict[str, Any], app: FastAPI
) -> None:
    expected = {
        (path, method.lower())
        for path, methods, include_in_schema in _iter_app_routes(app.routes)
        if include_in_schema
        for method in methods
        if method.lower() in HTTP_METHODS
    }
    actual = {(path, method) for path, method, _operation in _operations(spec)}

    assert actual == expected


def test_operation_ids_are_unique_and_non_empty(
    spec: dict[str, Any],
) -> None:
    operation_ids = [
        operation.get("operationId") for _path, _method, operation in _operations(spec)
    ]

    assert all(operation_ids)
    assert len(operation_ids) == len(set(operation_ids))


def test_every_operation_documents_summary_and_description(
    spec: dict[str, Any],
) -> None:
    undocumented = [
        f"{method.upper()} {path}"
        for path, method, operation in _operations(spec)
        if not str(operation.get("summary", "")).strip()
        or not str(operation.get("description", "")).strip()
    ]

    assert not undocumented, f"missing summary/description: {undocumented}"


def test_every_operation_has_a_documented_success_response(
    spec: dict[str, Any],
) -> None:
    for path, method, operation in _operations(spec):
        responses = operation["responses"]
        success = [code for code in responses if str(code).startswith("2")]
        assert success, f"{method.upper()} {path} has no 2xx response"
        for code in success:
            response = responses[code]
            assert str(response.get("description", "")).strip(), (
                f"{method.upper()} {path} {code} has no description"
            )


def test_operations_use_declared_tags(spec: dict[str, Any]) -> None:
    declared = {tag["name"] for tag in OPENAPI_TAGS}

    for path, method, operation in _operations(spec):
        tags = operation.get("tags")
        assert tags, f"{method.upper()} {path} has no tag"
        assert set(tags) <= declared, f"{method.upper()} {path} has unknown tags"


def test_path_parameters_are_documented(spec: dict[str, Any]) -> None:
    for path, method, operation in _operations(spec):
        declared = {
            parameter["name"]
            for parameter in operation.get("parameters", [])
            if parameter.get("in") == "path"
        }
        in_template = set(re.findall(r"\{([^{}]+)\}", path))
        assert declared == in_template, f"{method.upper()} {path} path params"

        for parameter in operation.get("parameters", []):
            if parameter.get("in") == "path":
                assert parameter.get("required") is True


def test_request_bodies_reference_schemas(spec: dict[str, Any]) -> None:
    for path, method, operation in _operations(spec):
        body = operation.get("requestBody")
        if body is None:
            continue
        content = body["content"]["application/json"]
        schema = content["schema"]
        assert schema, f"{method.upper()} {path} has an empty request schema"
        if "$ref" in schema:
            _resolve_ref(spec, schema["$ref"])


# ---------------------------------------------------------------------------
# Error responses
# ---------------------------------------------------------------------------


def test_validation_errors_are_documented(spec: dict[str, Any]) -> None:
    for path, method, operation in _operations(spec):
        response = operation["responses"].get("422")
        assert response is not None, f"{method.upper()} {path} documents no 422"
        assert str(response.get("description", "")).strip()
        schema = response["content"]["application/json"]["schema"]
        assert schema["$ref"] == "#/components/schemas/HTTPValidationError"


def test_authentication_errors_are_documented(spec: dict[str, Any]) -> None:
    """Every router route sits behind ``require_token`` when a token is set."""
    assert "Authorization: Bearer" in AUTH_ERROR_DETAIL
    assert "X-API-Key" in AUTH_ERROR_DETAIL

    for path, method, operation in _operations(spec):
        response = operation["responses"].get("401")
        assert response is not None, f"{method.upper()} {path} documents no 401"
        assert str(response.get("description", "")).strip()


def test_rate_limit_errors_are_documented(spec: dict[str, Any]) -> None:
    """Rate-limited routes document ``429`` and its ``Retry-After`` header."""
    for path, method, operation in _operations(spec):
        response = operation["responses"].get("429")
        if path in RATE_LIMIT_EXEMPT_PATHS:
            assert response is None, f"{method.upper()} {path} documents a 429"
            continue
        assert response is not None, f"{method.upper()} {path} documents no 429"
        assert str(response.get("description", "")).strip()
        assert "Retry-After" in response.get("headers", {})


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------


def test_security_schemes_match_the_api_key_auth(spec: dict[str, Any]) -> None:
    schemes = spec["components"]["securitySchemes"]

    bearer = schemes["BearerAuth"]
    assert bearer["type"] == "http"
    assert bearer["scheme"] == "bearer"

    api_key = schemes["ApiKeyAuth"]
    assert api_key["type"] == "apiKey"
    assert api_key["in"] == "header"
    assert api_key["name"] == "X-API-Key"


def test_security_requirement_references_only_declared_schemes(
    spec: dict[str, Any],
) -> None:
    declared = set(spec["components"]["securitySchemes"])
    requirements = spec["security"]

    assert requirements
    referenced = {scheme for requirement in requirements for scheme in requirement}
    assert referenced <= declared


def test_operations_document_the_headers_the_auth_layer_reads(
    spec: dict[str, Any],
) -> None:
    """The FastAPI dependency contributes both accepted auth headers."""
    for path, method, operation in _operations(spec):
        headers = {
            parameter["name"]
            for parameter in operation.get("parameters", [])
            if parameter.get("in") == "header"
        }
        assert {"authorization", "X-API-Key"} <= headers, (
            f"{method.upper()} {path} does not document the auth headers"
        )


# ---------------------------------------------------------------------------
# Component schemas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(EXPECTED_SCHEMA_FIELDS))
def test_key_schema_fields_are_present(spec: dict[str, Any], name: str) -> None:
    schema = _member_schema(spec, name)
    properties = set(schema.get("properties", {}))

    missing = EXPECTED_SCHEMA_FIELDS[name] - properties
    assert not missing, f"{name} is missing fields: {sorted(missing)}"


def test_schema_required_fields_are_stable(spec: dict[str, Any]) -> None:
    task = _member_schema(spec, "TaskCreateRequest")
    assert set(task["required"]) == {"site", "book_id"}

    download = _member_schema(spec, "MusicDownloadRequest")
    assert set(download["required"]) == {"tracks"}

    music_search = _member_schema(spec, "MusicSearchRequest")
    assert set(music_search["required"]) == {"keyword"}


def test_numeric_bounds_are_documented(spec: dict[str, Any]) -> None:
    """Map-like responses must stay strict JSON; request limits stay bounded."""
    music_search = _member_schema(spec, "MusicSearchRequest")
    assert music_search["properties"]["limit"]["maximum"] == 100
    assert music_search["properties"]["limit"]["minimum"] == 1

    search = _member_schema(spec, "SearchRequest")
    assert search["properties"]["timeout"]["maximum"] == 120.0
    assert search["properties"]["timeout"]["exclusiveMinimum"] == 0


# ---------------------------------------------------------------------------
# Documentation exposure
# ---------------------------------------------------------------------------


def test_docs_urls_follow_expose_docs(tmp_path: Any) -> None:
    exposed = create_app(output_dir=tmp_path / "exposed")
    assert exposed.docs_url == "/docs"
    assert exposed.redoc_url == "/redoc"
    assert exposed.openapi_url == "/openapi.json"

    hidden = create_app(output_dir=tmp_path / "hidden", expose_docs=False)
    assert hidden.docs_url is None
    assert hidden.redoc_url is None
    assert hidden.openapi_url is None
    # The schema can still be generated in-process.
    assert hidden.openapi()["paths"]


@pytest.mark.asyncio
async def test_docs_endpoints_serve_the_generated_schema(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        docs = await client.get("/docs")
        redoc = await client.get("/redoc")
        response = await client.get("/openapi.json")

    assert docs.status_code == 200
    assert "text/html" in docs.headers["content-type"]
    assert redoc.status_code == 200
    assert response.status_code == 200
    assert response.json() == app.openapi()


@pytest.mark.asyncio
async def test_docs_endpoints_are_absent_when_disabled(tmp_path: Any) -> None:
    app = create_app(output_dir=tmp_path / "downloads", expose_docs=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        docs = await client.get("/docs")
        redoc = await client.get("/redoc")
        schema = await client.get("/openapi.json")

    assert docs.status_code == 404
    assert redoc.status_code == 404
    assert schema.status_code == 404
