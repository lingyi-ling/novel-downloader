#!/usr/bin/env python3
"""
tests.apps.test_embedded_auth
-----------------------------

Authentication contract of the Android embedded engine.

The engine binds ``127.0.0.1``, which every app on the device can reach, so
the Kotlin launcher hands it a per-process random bearer token.  These tests
pin the behaviour that makes that useful:

* with a token configured, every ``/api/v1`` and ``/downloads`` request
  without (or with a wrong) credential is a ``401`` carrying the REST API's
  exact ``detail``, a ``WWW-Authenticate`` challenge and the correlation
  header — and that happens *before* routing, so route enumeration and body
  buffering are impossible without the credential;
* ``Authorization: Bearer`` and ``X-API-Key`` are both accepted, the scheme is
  case-insensitive, and the comparison goes through
  :func:`hmac.compare_digest`;
* the token never reaches a log line or a response body;
* without a token the engine behaves exactly as before (desktop/tests);
* the token source (environment variable name, accepted headers) matches the
  Kotlin launcher, so the two halves of the hand-off cannot drift.
"""

from __future__ import annotations

import hmac
import http.client
import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.core.task_manager import manager
from novel_downloader.apps.embedded import server as eserver

TOKEN = "Zm9vYmFyYmF6cXV1eC1zZWNyZXQtdG9rZW4tMTIzNDU2Nzg5MA"

_ROOT = Path(__file__).resolve().parents[2]
_EMBEDDED_ENGINE_KT = (
    _ROOT / "android/app/src/main/java/com/lingyi/noveldl/data/EmbeddedEngine.kt"
)


def _request(
    port: int,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """One request through ``http.client``; returns (status, headers, body)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        raw = response.read()
        return response.status, dict(response.getheaders()), raw
    finally:
        conn.close()


def _json_body(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8"))


@pytest.fixture()
def embedded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Start the engine; ``embedded.start(token=..., env=...)`` per test.

    The fixture only prepares the home and the no-op task manager.  Each test
    starts the server itself so it can choose between the explicit argument,
    the environment variable and no token at all.
    """
    home = tmp_path / "embedded_home"
    home.mkdir()

    async def noop_start() -> None:
        return None

    monkeypatch.setattr(manager, "start", noop_start)
    # A developer's shell must not silently authenticate the "no token" tests.
    monkeypatch.delenv(eserver.TOKEN_ENV_VAR, raising=False)
    monkeypatch.setenv("NOVEL_HOME", str(home))

    def start(*, token: str | None = None, env: str | None = None) -> int:
        if env is not None:
            monkeypatch.setenv(eserver.TOKEN_ENV_VAR, env)
        return eserver.start_server(str(home), 0, token)

    try:
        yield SimpleNamespace(home=home, start=start)
    finally:
        eserver.stop_server()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# configured token
# ---------------------------------------------------------------------------


def test_missing_token_is_rejected_with_the_rest_contract(embedded: Any) -> None:
    port = embedded.start(token=TOKEN)

    status, headers, raw = _request(port, "GET", "/api/v1/health")

    assert status == 401
    body = _json_body(raw)
    assert body == {
        "detail": (
            "Invalid or missing API token. "
            "Send 'Authorization: Bearer <token>' or 'X-API-Key: <token>'."
        )
    }
    assert headers["WWW-Authenticate"] == "Bearer"
    # The 401 is a normal response: it carries the correlation id like any
    # other, so a client can quote it in a bug report.
    assert headers["X-Request-ID"]
    assert TOKEN not in raw.decode("utf-8")


def test_wrong_token_is_rejected(embedded: Any) -> None:
    port = embedded.start(token=TOKEN)

    status, _, raw = _request(port, "GET", "/api/v1/health", _bearer(TOKEN + "x"))

    assert status == 401
    assert "Invalid or missing API token" in _json_body(raw)["detail"]


def test_correct_bearer_token_is_accepted(embedded: Any) -> None:
    port = embedded.start(token=TOKEN)

    status, _, raw = _request(port, "GET", "/api/v1/health", _bearer(TOKEN))

    assert status == 200
    assert _json_body(raw)["status"] == "ok"


def test_correct_api_key_header_is_accepted(embedded: Any) -> None:
    port = embedded.start(token=TOKEN)

    status, _, raw = _request(port, "GET", "/api/v1/health", {"X-API-Key": TOKEN})

    assert status == 200
    assert _json_body(raw)["status"] == "ok"


def test_bearer_scheme_is_case_insensitive(embedded: Any) -> None:
    port = embedded.start(token=TOKEN)

    status, _, _ = _request(
        port, "GET", "/api/v1/health", {"Authorization": f"bearer {TOKEN}"}
    )

    assert status == 200


def test_bearer_value_whitespace_is_trimmed(embedded: Any) -> None:
    port = embedded.start(token=TOKEN)

    status, _, _ = _request(
        port, "GET", "/api/v1/health", {"Authorization": f"Bearer  {TOKEN}  "}
    )

    assert status == 200


def test_non_bearer_authorization_is_rejected(embedded: Any) -> None:
    port = embedded.start(token=TOKEN)

    status, _, _ = _request(
        port, "GET", "/api/v1/health", {"Authorization": f"Basic {TOKEN}"}
    )

    assert status == 401


def test_downloads_mount_is_guarded(embedded: Any) -> None:
    downloads = embedded.home / "downloads"
    downloads.mkdir()
    (downloads / "book.txt").write_text("hello", encoding="utf-8")
    port = embedded.start(token=TOKEN)

    status, _, raw = _request(port, "GET", "/downloads/book.txt")
    assert status == 401
    assert TOKEN.encode() not in raw

    status, _, raw = _request(port, "GET", "/downloads/book.txt", _bearer(TOKEN))
    assert status == 200


def test_auth_runs_before_routing(embedded: Any) -> None:
    """An unauthenticated caller cannot enumerate routes or methods.

    Unknown paths, wrong verbs and malformed bodies all answer ``401`` rather
    than leaking ``404``/``405``/``422`` differences to a caller that has no
    credential.
    """
    port = embedded.start(token=TOKEN)

    for method, path in (
        ("GET", "/api/v1/does-not-exist"),
        ("PUT", "/api/v1/health"),
        ("POST", "/api/v1/tasks"),
        ("GET", "/downloads/nope.txt"),
    ):
        status, _, _ = _request(port, method, path)
        assert status == 401, (method, path)


@pytest.mark.parametrize(
    "target",
    [
        "/api/v1/health",
        "/api/v1/health/",
        "///api/v1/health",
        "//api/v1/health",
        "/downloads/book.txt",
        "/downloads//book.txt",
    ],
)
def test_guarded_paths_cannot_be_spelled_past_the_gate(
    embedded: Any, target: str
) -> None:
    """Path spellings the router accepts must not look unguarded.

    ``_pattern_matches`` and ``_serve_download`` both normalise the path with
    ``strip("/")``, so ``//api/v1/health`` reaches the same handler as
    ``/api/v1/health``.  A prefix test on the raw path would have let that
    spelling through unauthenticated.
    """
    downloads = embedded.home / "downloads"
    downloads.mkdir(exist_ok=True)
    (downloads / "book.txt").write_text("hello", encoding="utf-8")
    port = embedded.start(token=TOKEN)

    status, _, _ = _request(port, "GET", target)

    assert status == 401, target


def test_auth_rejects_before_reading_the_body(embedded: Any) -> None:
    """A hostile payload is refused without the engine buffering it."""
    port = embedded.start(token=TOKEN)

    status, _, _ = _request(
        port,
        "POST",
        "/api/v1/search",
        {"Content-Type": "application/json"},
        body=b'{"keyword": "x"}',
    )

    assert status == 401


def test_unguarded_paths_keep_their_old_answers(embedded: Any) -> None:
    """Only ``/api/v1`` and ``/downloads`` are guarded."""
    port = embedded.start(token=TOKEN)

    status, _, raw = _request(port, "GET", "/")

    assert status == 404
    assert _json_body(raw) == {"detail": "Not Found"}


def test_client_request_id_is_echoed_on_401(embedded: Any) -> None:
    port = embedded.start(token=TOKEN)

    status, headers, _ = _request(
        port, "GET", "/api/v1/health", {"X-Request-ID": "abc-123"}
    )

    assert status == 401
    assert headers["X-Request-ID"] == "abc-123"


def test_unsafe_client_request_id_is_replaced_on_401(embedded: Any) -> None:
    port = embedded.start(token=TOKEN)

    status, headers, _ = _request(
        port, "GET", "/api/v1/health", {"X-Request-ID": "bad id!"}
    )

    assert status == 401
    echoed = headers["X-Request-ID"]
    assert echoed != "bad id!"
    assert len(echoed) == 36  # generated UUID4


def test_comparison_uses_hmac_compare_digest(
    embedded: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = embedded.start(token=TOKEN)
    seen: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(eserver.hmac, "compare_digest", spy)

    status, _, _ = _request(port, "GET", "/api/v1/health", _bearer(TOKEN))

    assert status == 200
    assert seen == [(TOKEN.encode(), TOKEN.encode())]


def test_non_ascii_token_is_compared_as_bytes(embedded: Any) -> None:
    """``compare_digest`` raises ``TypeError`` on non-ASCII ``str``.

    Tokens are base64url in practice, but the comparison must not turn a
    misconfigured (non-ASCII) token into a 500.
    """
    token = "tökén-ünicode"
    port = embedded.start(token=token)

    assert _request(port, "GET", "/api/v1/health")[0] == 401
    assert _request(port, "GET", "/api/v1/health", _bearer(token))[0] == 200


def test_token_never_appears_in_logs(
    embedded: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    port = embedded.start(token=TOKEN)

    _request(port, "GET", "/api/v1/health")
    _request(port, "GET", "/api/v1/health", _bearer(TOKEN))
    _request(port, "GET", f"/api/v1/health?token={TOKEN}", _bearer(TOKEN))

    assert caplog.records, "requests should have produced log records"
    for record in caplog.records:
        message = record.getMessage()
        assert TOKEN not in message
        assert TOKEN not in str(record.args)


def test_token_never_appears_in_an_error_body(embedded: Any) -> None:
    port = embedded.start(token=TOKEN)

    _, _, raw = _request(
        port, "GET", "/api/v1/health", {"Authorization": "Bearer wrong"}
    )

    assert TOKEN.encode() not in raw


# ---------------------------------------------------------------------------
# token sources
# ---------------------------------------------------------------------------


def test_token_is_read_from_the_environment(embedded: Any) -> None:
    """The launcher's preferred channel: the argument list stays clean."""
    port = embedded.start(env=TOKEN)

    assert _request(port, "GET", "/api/v1/health")[0] == 401
    assert _request(port, "GET", "/api/v1/health", _bearer(TOKEN))[0] == 200


def test_explicit_argument_wins_over_the_environment(embedded: Any) -> None:
    port = embedded.start(token=TOKEN, env="a-different-token")

    assert _request(port, "GET", "/api/v1/health", _bearer(TOKEN))[0] == 200
    assert (
        _request(port, "GET", "/api/v1/health", _bearer("a-different-token"))[0] == 401
    )


def test_blank_argument_disables_the_environment_token(embedded: Any) -> None:
    port = embedded.start(token="", env=TOKEN)

    assert _request(port, "GET", "/api/v1/health")[0] == 200


def test_blank_environment_token_disables_auth(embedded: Any) -> None:
    port = embedded.start(env="   ")

    assert _request(port, "GET", "/api/v1/health")[0] == 200


def test_stop_server_forgets_the_token(embedded: Any) -> None:
    """A restarted engine must not inherit the previous run's credential."""
    port = embedded.start(token=TOKEN)
    assert _request(port, "GET", "/api/v1/health")[0] == 401
    eserver.stop_server()

    port = eserver.start_server(str(embedded.home), 0)

    assert _request(port, "GET", "/api/v1/health")[0] == 200


# ---------------------------------------------------------------------------
# unconfigured token: the historical behaviour is untouched
# ---------------------------------------------------------------------------


def test_without_a_token_requests_are_unchanged(embedded: Any) -> None:
    port = embedded.start()

    status, _, raw = _request(port, "GET", "/api/v1/health")

    assert status == 200
    assert _json_body(raw)["status"] == "ok"
    # A credential is not required but must not break the request either.
    assert _request(port, "GET", "/api/v1/health", _bearer("anything"))[0] == 200


def test_without_a_token_unknown_routes_still_answer_404(embedded: Any) -> None:
    port = embedded.start()

    status, _, raw = _request(port, "GET", "/api/v1/does-not-exist")

    assert status == 404
    assert _json_body(raw) == {"detail": "Not Found"}


def test_without_a_token_method_mismatch_still_answers_405(embedded: Any) -> None:
    port = embedded.start()

    status, headers, _ = _request(port, "PUT", "/api/v1/health")

    assert status == 405
    assert headers["Allow"] == "GET"


def test_without_a_token_downloads_stay_public(embedded: Any) -> None:
    downloads = embedded.home / "downloads"
    downloads.mkdir()
    (downloads / "book.txt").write_text("hello", encoding="utf-8")
    port = embedded.start()

    status, _, raw = _request(port, "GET", "/downloads/book.txt")

    assert status == 200
    assert raw == b"hello"


# ---------------------------------------------------------------------------
# cross-language hand-off
# ---------------------------------------------------------------------------


def test_the_kotlin_launcher_uses_the_same_environment_variable() -> None:
    """The Kotlin constant and the Python constant must not drift.

    The launcher writes the token into ``os.environ`` under a literal, and the
    server reads it from :data:`eserver.TOKEN_ENV_VAR`.  A rename on either
    side would silently disable authentication (the engine would see no token
    and fall back to its open default), so the literal is pinned here.
    """
    source = _EMBEDDED_ENGINE_KT.read_text(encoding="utf-8")
    match = re.search(
        r'EMBEDDED_TOKEN_ENV_VAR\s*=\s*"([^"]+)"',
        source,
    )
    assert match is not None, "EmbeddedEngine.kt must declare EMBEDDED_TOKEN_ENV_VAR"
    assert match.group(1) == eserver.TOKEN_ENV_VAR


def test_the_kotlin_launcher_hands_the_token_over_before_starting() -> None:
    """The token must be installed before ``start_server`` reads it."""
    source = _EMBEDDED_ENGINE_KT.read_text(encoding="utf-8")
    environ_index = source.index('get("environ")')
    start_index = source.index('callAttr("start_server"')
    assert environ_index < start_index


def test_the_kotlin_launcher_never_logs_the_token() -> None:
    source = _EMBEDDED_ENGINE_KT.read_text(encoding="utf-8")
    for line in source.splitlines():
        if "Log." in line:
            assert "token" not in line.lower()


def test_the_kotlin_launcher_never_passes_the_token_as_an_argument() -> None:
    """A command-line argument would be visible in ``/proc/<pid>/cmdline``."""
    source = _EMBEDDED_ENGINE_KT.read_text(encoding="utf-8")
    call = source[source.index('callAttr("start_server"') :].split(")", 1)[0]
    assert "token" not in call.lower()
