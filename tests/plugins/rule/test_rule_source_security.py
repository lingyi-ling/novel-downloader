#!/usr/bin/env python3
"""Security tests for loading rule sources from disk and from a URL.

Threat model: a rule document is *configuration*, but it can arrive from an
untrusted place (a downloaded file, a community URL, a shared rules directory).
Loading one must never:

* read a file outside the configured rule directories (``..``, absolute paths,
  a symlink that escapes the directory, a device/FIFO instead of a file),
* perform a server-side request to localhost, the private network or a cloud
  metadata endpoint (including through a redirect),
* let an oversized / deeply nested / key-flooded document or an enormous regex
  pin or exhaust the process,
* echo a secret (a URL token, a regex pattern) in an error message.

The tests below use no socket: DNS lookups are monkeypatched and ``urlopen`` is
a stub, so they also run in an offline sandbox.
"""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

import novel_downloader.plugins.rule as rule_pkg
from novel_downloader.plugins.rule import RuleError, install_rule, reload_rules
from novel_downloader.plugins.rule import remote as rule_remote
from novel_downloader.plugins.rule.model import (
    Field,
    loads_rule_json,
    parse_rule,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _rule(site_key: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "site_key": site_key,
        "base_url": "https://demo.example",
        "hosts": ["demo.example"],
        "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"},
    }
    payload.update(overrides)
    return payload


class _Response:
    """Minimal response stand-in (with an optional final/redirect URL)."""

    def __init__(
        self,
        body: bytes = b"{}",
        *,
        content_type: str | None = "application/json",
        status: int = 200,
        content_length: int | None = None,
        final_url: str | None = None,
    ) -> None:
        self._body = body
        self._pos = 0
        self.headers: dict[str, str] = {}
        if content_type is not None:
            self.headers["Content-Type"] = content_type
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.status = status
        self._final_url = final_url
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunk = self._body[self._pos :]
            self._pos = len(self._body)
        else:
            chunk = self._body[self._pos : self._pos + size]
            self._pos += len(chunk)
        return chunk

    def geturl(self) -> str:
        return self._final_url or ""

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        self.closed = True


class _Opener:
    def __init__(self, response: _Response | None = None) -> None:
        self.response = response
        self.request: Any = None

    def __call__(self, request: Any, timeout: float | None = None) -> _Response:
        self.request = request
        assert self.response is not None
        return self.response


@pytest.fixture
def opener(monkeypatch: pytest.MonkeyPatch) -> _Opener:
    """Replace ``urlopen`` with a stub and make DNS resolution deterministic."""
    stub = _Opener(_Response())
    monkeypatch.setattr(rule_remote, "urlopen", stub)
    return stub


@pytest.fixture(autouse=True)
def _no_network_resolution(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Never touch the resolver in these tests unless a test replaces it."""

    def _fail(host: str, *args: Any, **kwargs: Any) -> Any:
        raise socket.gaierror(socket.EAI_NONAME, "name not known")

    monkeypatch.setattr(rule_remote.socket, "getaddrinfo", _fail)
    yield


@pytest.fixture
def rules_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    directory = tmp_path / "rules"
    directory.mkdir()
    monkeypatch.setenv("NOVEL_RULES_DIR", str(directory))
    monkeypatch.setattr(rule_pkg, "_user_dirs", lambda: [directory])
    reload_rules()
    yield directory
    reload_rules()


def _write(directory: Path, name: str, payload: Any) -> Path:
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# A. remote URL: scheme, credentials and local/private targets
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/r.json",
        "http://localhost./r.json",
        "http://localhost.localdomain/r.json",
        "http://app.localhost/r.json",
        "http://127.0.0.1/r.json",
        "http://127.1/r.json",
        "http://0.0.0.0/r.json",
        "http://[::1]/r.json",
        "http://10.0.0.7/r.json",
        "http://172.16.5.4/r.json",
        "http://192.168.1.20/r.json",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.100.100.200/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://service.internal/r.json",
        "http://printer.local/r.json",
        "http://[fd00:ec2::254]/latest/meta-data/",
        "http://[fe80::1]/r.json",
    ],
)
def test_fetch_rule_refuses_local_and_private_targets(
    url: str, opener: _Opener
) -> None:
    with pytest.raises(RuleError, match="non-public|private|local"):
        rule_remote.fetch_rule(url)

    assert opener.request is None


def test_fetch_rule_refuses_a_hostname_resolving_to_a_private_address(
    monkeypatch: pytest.MonkeyPatch, opener: _Opener
) -> None:
    """A public-looking name must not be trusted before its addresses are known."""

    def _resolve(host: str, port: Any, *args: Any, **kwargs: Any) -> list[Any]:
        assert host == "rebind.example.org"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 443))]

    monkeypatch.setattr(rule_remote.socket, "getaddrinfo", _resolve)

    with pytest.raises(RuleError, match="non-public|private|local"):
        rule_remote.fetch_rule("https://rebind.example.org/r.json")

    assert opener.request is None


def test_fetch_rule_allows_a_hostname_with_only_public_addresses(
    monkeypatch: pytest.MonkeyPatch, opener: _Opener
) -> None:
    def _resolve(host: str, port: Any, *args: Any, **kwargs: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(rule_remote.socket, "getaddrinfo", _resolve)
    opener.response = _Response(json.dumps(_rule("public-site")).encode())

    assert rule_remote.fetch_rule("https://public.example.org/r.json")["site_key"] == (
        "public-site"
    )


def test_fetch_rule_refuses_credentials_in_the_url(opener: _Opener) -> None:
    with pytest.raises(RuleError, match="credentials|userinfo|user:pass"):
        rule_remote.fetch_rule("https://user:pass@rules.example/r.json")

    assert opener.request is None


def test_fetch_rule_refuses_an_overlong_url(opener: _Opener) -> None:
    with pytest.raises(RuleError, match="too long"):
        rule_remote.fetch_rule("https://rules.example/" + "a" * 20_000)

    assert opener.request is None


# --------------------------------------------------------------------------- #
# B. redirects
# --------------------------------------------------------------------------- #
def test_fetch_rule_refuses_a_redirect_to_a_private_target(
    monkeypatch: pytest.MonkeyPatch, opener: _Opener
) -> None:
    opener.response = _Response(
        json.dumps(_rule("redirected")).encode(),
        final_url="http://169.254.169.254/latest/meta-data/",
    )

    with pytest.raises(RuleError, match="redirect|non-public|private|local"):
        rule_remote.fetch_rule("https://rules.example/r.json")


def test_fetch_rule_refuses_a_redirect_downgrade(
    monkeypatch: pytest.MonkeyPatch, opener: _Opener
) -> None:
    opener.response = _Response(
        json.dumps(_rule("redirected")).encode(),
        final_url="http://rules.example/r.json",
    )

    with pytest.raises(RuleError, match="downgrade|https.*http|scheme"):
        rule_remote.fetch_rule("https://rules.example/r.json")


def test_fetch_rule_refuses_a_redirect_to_a_non_http_scheme(
    monkeypatch: pytest.MonkeyPatch, opener: _Opener
) -> None:
    opener.response = _Response(
        json.dumps(_rule("redirected")).encode(),
        final_url="ftp://rules.example/r.json",
    )

    with pytest.raises(RuleError, match="scheme|http"):
        rule_remote.fetch_rule("https://rules.example/r.json")


# --------------------------------------------------------------------------- #
# C. secrets must not leak into errors
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "response",
    [
        _Response(b'{"site_key": "x"}', content_length=10_000_000),
        _Response(b"<html>no</html>", content_type="text/html"),
        _Response(b"{not json"),
        _Response(b"{}", status=500),
    ],
)
def test_fetch_rule_never_echoes_a_url_token(
    response: _Response, opener: _Opener
) -> None:
    opener.response = response

    with pytest.raises(RuleError) as excinfo:
        rule_remote.fetch_rule("https://rules.example/r.json?token=SUPER-SECRET")

    assert "SUPER-SECRET" not in str(excinfo.value)


def test_fetch_rule_never_echoes_a_token_on_http_error(
    monkeypatch: pytest.MonkeyPatch, opener: _Opener
) -> None:
    opener.response = _Response(status=404)

    with pytest.raises(RuleError) as excinfo:
        rule_remote.fetch_rule("https://rules.example/r.json?token=SUPER-SECRET")

    assert "SUPER-SECRET" not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# D. size limits
# --------------------------------------------------------------------------- #
def test_fetch_rule_refuses_an_unbounded_max_bytes(opener: _Opener) -> None:
    with pytest.raises(RuleError, match="max_bytes"):
        rule_remote.fetch_rule("https://rules.example/r.json", max_bytes=10**9)

    assert opener.request is None


# --------------------------------------------------------------------------- #
# E. local rule files: no escape, no special files, no crash
# --------------------------------------------------------------------------- #
def test_loader_skips_a_symlink_that_escapes_the_rules_dir(
    rules_dir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_rule("outside-site")), encoding="utf-8")
    link = rules_dir / "leak.json"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")

    _write(rules_dir, "local.json", _rule("local-site"))
    rules = reload_rules()

    assert "outside-site" not in rules
    assert "local-site" in rules
    assert outside.is_file()  # reading/loading never deletes or rewrites it


def test_loader_follows_a_symlink_inside_the_rules_dir(
    rules_dir: Path,
) -> None:
    target = _write(rules_dir, "real.json", _rule("inside-site"))
    link = rules_dir / "alias.json"
    try:
        link.symlink_to(target.name)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")

    assert "inside-site" in reload_rules()


def test_loader_skips_a_directory_named_like_a_rule(rules_dir: Path) -> None:
    (rules_dir / "folder.json").mkdir()
    _write(rules_dir, "good.json", _rule("good-site"))

    rules = reload_rules()

    assert "good-site" in rules


def test_loader_treats_invalid_utf8_as_one_bad_file_not_a_startup_failure(
    rules_dir: Path,
) -> None:
    (rules_dir / "bad.json").write_bytes(b'{"site_key": "\xff\xfe"}')
    _write(rules_dir, "good.json", _rule("good-site"))

    rules = reload_rules()

    assert "good-site" in rules


def test_install_rule_refuses_a_source_that_is_not_a_regular_file(
    tmp_path: Path, rules_dir: Path
) -> None:
    directory = tmp_path / "not-a-file.json"
    directory.mkdir()

    with pytest.raises(RuleError):
        install_rule(directory)

    assert not any(rules_dir.iterdir())


@pytest.mark.parametrize(
    "name",
    [
        "C:\\Windows\\System32\\evil",
        "\\\\server\\share\\evil",
        "/etc/passwd",
        "../../escape",
    ],
)
def test_install_names_cannot_select_an_absolute_unc_or_parent_path(
    rules_dir: Path, name: str
) -> None:
    target = install_rule(_rule("safe-site"), name=name)

    assert target.parent == rules_dir
    assert target.is_file()


# --------------------------------------------------------------------------- #
# F. JSON document / regex size limits
# --------------------------------------------------------------------------- #
def test_loads_rule_json_rejects_a_key_flood() -> None:
    payload = {"site_key": "flood"}
    payload.update({f"k{index}": 1 for index in range(5000)})

    with pytest.raises(RuleError, match="too many keys"):
        loads_rule_json(json.dumps(payload))


def test_loads_rule_json_rejects_excessive_nesting() -> None:
    node: Any = {}
    for _ in range(100):
        node = {"n": node}

    with pytest.raises(RuleError, match="nested too deeply|nesting deeper"):
        loads_rule_json(json.dumps(node))


def test_parse_rule_rejects_an_overlong_regex() -> None:
    with pytest.raises(RuleError, match="too long"):
        parse_rule(
            {
                "site_key": "demo",
                "chapter": {"url": "u", "drop": ["a" * 9000]},
            }
        )


def test_field_rejects_an_overlong_regex() -> None:
    with pytest.raises(RuleError, match="too long"):
        Field.from_dict({"selector": "a", "regex": "a" * 9000})


def test_regex_errors_do_not_echo_the_pattern() -> None:
    with pytest.raises(RuleError) as excinfo:
        Field(regex="SUPER-SECRET-TOKEN(")

    assert "SUPER-SECRET-TOKEN" not in str(excinfo.value)

    with pytest.raises(RuleError) as excinfo:
        parse_rule(
            {
                "site_key": "demo",
                "chapter": {"url": "u", "drop": ["SUPER-SECRET-TOKEN("]},
            }
        )

    assert "SUPER-SECRET-TOKEN" not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# G. overriding a built-in source stays explicit (unchanged behaviour)
# --------------------------------------------------------------------------- #
def test_installing_over_a_builtin_is_allowed_but_logged(
    rules_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The override remains supported, but it is never silent."""
    import logging

    with caplog.at_level(logging.WARNING):
        install_rule(
            _rule("biquge5r", base_url="https://shadow.example"),
            overwrite=True,
        )

    assert "biquge5r" in caplog.text


# --------------------------------------------------------------------------- #
# H. a failed download leaves nothing behind (behaviour kept)
# --------------------------------------------------------------------------- #
def test_fetch_rule_does_not_write_anything(
    opener: _Opener, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opener.response = _Response(json.dumps(_rule("memory-only")).encode())
    monkeypatch.chdir(tmp_path)

    rule_remote.fetch_rule("https://rules.example/r.json")

    assert list(tmp_path.iterdir()) == []


def test_http_error_is_reported_without_the_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = HTTPError(
        "https://rules.example/r.json?token=SUPER-SECRET",
        403,
        "Forbidden",
        {},
        None,
    )

    def _raise(request: Any, timeout: float | None = None) -> Any:
        raise error

    monkeypatch.setattr(rule_remote, "urlopen", _raise)

    with pytest.raises(RuleError) as excinfo:
        rule_remote.fetch_rule("https://rules.example/r.json?token=SUPER-SECRET")

    assert "SUPER-SECRET" not in str(excinfo.value)


def test_install_dir_file_collision_does_not_escape(
    tmp_path: Path, rules_dir: Path
) -> None:
    """A sanitized name is always joined to the configured directory."""
    target = install_rule(_rule("safe-site"), name="..\\..\\..\\evil.json")
    assert os.path.commonpath([target, rules_dir]) == str(rules_dir)


# --------------------------------------------------------------------------- #
# I. the CLI's own downloader applies the same policy
# --------------------------------------------------------------------------- #
def test_cli_sources_add_refuses_an_internal_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novel_downloader.apps.cli.commands import sources

    calls: list[Any] = []
    monkeypatch.setattr(
        sources.urllib_request, "urlopen", lambda *args, **kwargs: calls.append(args)
    )

    with pytest.raises(ValueError):
        sources._download_rule("http://169.254.169.254/latest/meta-data/")
    assert calls == []


def test_cli_sources_add_refuses_a_redirect_to_an_internal_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novel_downloader.apps.cli.commands import sources

    class _Redirected:
        def read(self, size: int = -1) -> bytes:
            return b"{}"

        def geturl(self) -> str:
            return "http://127.0.0.1/r.json"

        def __enter__(self) -> _Redirected:
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

    monkeypatch.setattr(
        sources.urllib_request, "urlopen", lambda *args, **kwargs: _Redirected()
    )

    with pytest.raises(ValueError):
        sources._download_rule("https://rules.example/r.json")
