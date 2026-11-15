#!/usr/bin/env python3
"""
tests.apps.test_openapi_lint
----------------------------

Documentation-quality gate for the REST API's generated OpenAPI document.

The document is the public contract every client (desktop, CLI, Android) reads
and this module keeps it from rotting:

* every documented operation has a ``summary``, at least one ``tag`` and a
  documented ``2xx`` response;
* every ``4xx`` / ``5xx`` response has a ``description`` and either a schema
  for each declared media type or is explicitly content-free (the OpenAPI way
  to document a ``204``-style response);
* every ``components.schemas`` entry is reachable from an operation (dead
  schemas are removed, or listed in :data:`ALLOWED_UNUSED_SCHEMAS` with the
  reason they are kept);
* every local ``$ref`` resolves, proving the document has no dangling links;
* every parameter (path, query or header) has a ``description`` and a
  ``schema``;
* the document declares OpenAPI ``3.1.x``;
* every path/method the stdlib-only embedded engine serves is also documented
  by the REST API (the embedded engine may implement a subset, but never an
  undocumented endpoint).

The checks run offline against ``create_app().openapi()``; no server is
started and no network is touched.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI

from novel_downloader.apps.api import create_app
from novel_downloader.apps.embedded import server as embedded_server

#: Methods that may appear in an OpenAPI path item.
HTTP_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
)

#: Paths FastAPI registers for documentation, not part of the API contract.
DOCUMENTATION_PATHS = frozenset({"/docs", "/redoc", "/openapi.json"})

#: Static-file mounts, served outside the JSON router.
STATIC_PATH_PREFIXES: tuple[str, ...] = ("/downloads",)

#: Component schemas deliberately kept although no operation reaches them.
#:
#: Every entry must state why the schema is not dead code. Keeping the mapping
#: empty is the goal: a schema that no operation references is normally a
#: leftover that should be deleted. Add an entry only when the schema is a
#: stable public name that clients import or a deliberately reserved response
#: shape; the lint below also fails if an allow-listed schema disappears.
ALLOWED_UNUSED_SCHEMAS: dict[str, str] = {}

_SCHEMA_REF_PREFIX = "#/components/schemas/"


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
    for path, item in spec.get("paths", {}).items():
        for method, operation in item.items():
            if method in HTTP_METHODS and isinstance(operation, dict):
                yield path, method, operation


def _is_excluded_path(path: str) -> bool:
    """Whether ``path`` is a documentation route or a static-file mount."""
    if path in DOCUMENTATION_PATHS:
        return True
    return any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in STATIC_PATH_PREFIXES
    )


def _resolve_ref(spec: dict[str, Any], ref: str) -> Any:
    """Resolve a local ``#/...`` JSON pointer reference."""
    assert ref.startswith("#/"), f"non-local $ref: {ref}"
    node: Any = spec
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        assert isinstance(node, dict) and part in node, f"unresolvable $ref: {ref}"
        node = node[part]
    return node


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


def _iter_parameters(
    spec: dict[str, Any], path_item: dict[str, Any], operation: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    """Yield every parameter on a path item or operation, resolving ``$ref``."""
    for parameter in [
        *path_item.get("parameters", []),
        *operation.get("parameters", []),
    ]:
        if not isinstance(parameter, dict):
            continue
        if "$ref" in parameter:
            parameter = _resolve_ref(spec, parameter["$ref"])
            assert isinstance(parameter, dict)
        yield parameter


def _response_has_schema(response: dict[str, Any]) -> bool:
    """Whether every declared media type on a response carries a schema."""
    content = response.get("content")
    if not isinstance(content, dict) or not content:
        return False
    return all(
        isinstance(media, dict)
        and isinstance(media.get("schema"), dict)
        and bool(media["schema"])
        for media in content.values()
    )


def _is_documented_no_content(response: dict[str, Any]) -> bool:
    """
    Whether a response is explicitly content-free.

    OpenAPI represents a response with no body by omitting ``content`` (or
    declaring it empty); the required ``description`` already explains what the
    status means. A response that declares a media type but forgets its schema
    is *not* content-free and is caught as a gap.
    """
    return response.get("content") in (None, {})


def _reachable_schema_names(spec: dict[str, Any]) -> set[str]:
    """Return the component schemas reachable from at least one operation."""
    schemas = spec.get("components", {}).get("schemas", {})
    if not isinstance(schemas, dict):
        return set()

    pending = [
        ref
        for _path, _method, operation in _operations(spec)
        for ref in _iter_refs(operation)
        if ref.startswith(_SCHEMA_REF_PREFIX)
    ]
    reached: set[str] = set()
    while pending:
        ref = pending.pop()
        name = ref[len(_SCHEMA_REF_PREFIX) :]
        if name in reached:
            continue
        reached.add(name)
        pending.extend(
            nested
            for nested in _iter_refs(schemas.get(name, {}))
            if nested.startswith(_SCHEMA_REF_PREFIX)
        )
    return reached


# ---------------------------------------------------------------------------
# Document version and operations
# ---------------------------------------------------------------------------


def test_openapi_version_is_3_1(spec: dict[str, Any]) -> None:
    """The document must use OpenAPI 3.1.x (JSON Schema null unions, etc.)."""
    version = str(spec.get("openapi", ""))

    assert re.fullmatch(r"3\.1\.\d+", version), version


def test_every_operation_has_summary_tags_and_success(spec: dict[str, Any]) -> None:
    """An operation missing one of these is unusable from generated clients."""
    failures: list[str] = []
    checked = 0
    for path, method, operation in _operations(spec):
        if _is_excluded_path(path):
            continue
        checked += 1
        label = f"{method.upper()} {path}"
        if not str(operation.get("summary", "")).strip():
            failures.append(f"{label}: missing summary")
        tags = operation.get("tags")
        if not isinstance(tags, list) or not [tag for tag in tags if str(tag).strip()]:
            failures.append(f"{label}: missing tags")
        responses = operation.get("responses", {})
        if not any(str(code).startswith("2") for code in responses):
            failures.append(f"{label}: no 2xx response")

    assert checked, "the document should contain at least one API operation"
    assert not failures, "\n".join(failures)


def test_error_responses_have_description_and_schema(spec: dict[str, Any]) -> None:
    """Clients switch on 4xx/5xx; an undocumented one is an untestable contract."""
    failures: list[str] = []
    checked = 0
    for path, method, operation in _operations(spec):
        if _is_excluded_path(path):
            continue
        for code, response in operation.get("responses", {}).items():
            status = str(code)
            if not (status.startswith("4") or status.startswith("5")):
                continue
            checked += 1
            if not isinstance(response, dict):
                failures.append(f"{method.upper()} {path} {status}: not an object")
                continue
            if not str(response.get("description", "")).strip():
                failures.append(f"{method.upper()} {path} {status}: no description")
            if not (
                _response_has_schema(response) or _is_documented_no_content(response)
            ):
                failures.append(
                    f"{method.upper()} {path} {status}: no schema and not "
                    "explicitly content-free"
                )

    assert checked, "the document should document error responses"
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# References and components
# ---------------------------------------------------------------------------


def test_every_ref_resolves(spec: dict[str, Any]) -> None:
    """A dangling ``$ref`` breaks schema rendering and generated clients."""
    refs = list(_iter_refs(spec))
    assert refs, "the document should contain references"

    for ref in refs:
        _resolve_ref(spec, ref)


def test_component_schemas_are_all_referenced(spec: dict[str, Any]) -> None:
    """Dead schemas are either deleted or explicitly allow-listed with a reason."""
    schemas = spec.get("components", {}).get("schemas", {})
    assert isinstance(schemas, dict)
    assert schemas, "the document should declare request/response component schemas"

    reachable = _reachable_schema_names(spec)
    unused = set(schemas) - reachable
    unexpected = unused - set(ALLOWED_UNUSED_SCHEMAS)
    stale_allowlist = set(ALLOWED_UNUSED_SCHEMAS) - set(schemas)

    assert not unexpected, (
        "component schemas not referenced by any operation: "
        f"{sorted(unexpected)}; delete them or add ALLOWED_UNUSED_SCHEMAS "
        "entries with the reason they are kept"
    )
    assert not stale_allowlist, (
        "ALLOWED_UNUSED_SCHEMAS names schemas that no longer exist: "
        f"{sorted(stale_allowlist)}"
    )


def test_every_parameter_has_description_and_schema(spec: dict[str, Any]) -> None:
    """Path/query/header parameters are part of the contract and need docs."""
    failures: list[str] = []
    checked = 0
    for path, item in spec.get("paths", {}).items():
        if _is_excluded_path(path):
            continue
        for method, operation in item.items():
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            for parameter in _iter_parameters(spec, item, operation):
                checked += 1
                location = parameter.get("in", "?")
                name = parameter.get("name", "?")
                label = f"{method.upper()} {path} {location} {name}"
                if not str(parameter.get("description", "")).strip():
                    failures.append(f"{label}: missing description")
                schema = parameter.get("schema")
                if not isinstance(schema, dict) or not schema:
                    failures.append(f"{label}: missing schema")

    assert checked, "the document should declare parameters"
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Embedded-engine parity
# ---------------------------------------------------------------------------


def test_embedded_routes_are_documented_by_rest_api(
    spec: dict[str, Any],
) -> None:
    """The embedded engine may implement a subset, never an undocumented path."""
    embedded = {
        (str(method).upper(), str(path)) for method, path in embedded_server.ROUTES
    }
    assert embedded, "the embedded engine should declare at least one route"

    documented = {
        (method.upper(), path) for path, method, _operation in _operations(spec)
    }
    undocumented = sorted(embedded - documented)

    assert not undocumented, (
        "embedded routes missing from the OpenAPI document: "
        f"{undocumented}; document them in the REST router or remove them"
    )
