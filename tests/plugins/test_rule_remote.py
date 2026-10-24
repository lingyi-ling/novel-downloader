#!/usr/bin/env python3
"""
Offline tests for remote rule fetching and user-rule installation.

``urlopen`` is always monkeypatched: no test opens a socket.  Installation tests
write into ``tmp_path`` through ``NOVEL_RULES_DIR`` and ``refresh_rules`` is
exercised against the real builtin rule (``biquge5r``) plus temporary user files.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

from novel_downloader.plugins.rule import (
    RuleError,
    fetch_rule,
    install_rule,
    load_rules,
    refresh_rules,
    reload_rules,
)
from novel_downloader.plugins.rule import remote as rule_remote


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolated_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point rule discovery/installation at a per-test temporary directory."""
    rules_dir = tmp_path / "user_rules"
    rules_dir.mkdir()
    monkeypatch.setenv("NOVEL_RULES_DIR", str(rules_dir))
    monkeypatch.setattr("novel_downloader.plugins.rule._user_dirs", lambda: [rules_dir])
    reload_rules()
    yield rules_dir
    reload_rules()


def _rule(site_key: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "site_key": site_key,
        "base_url": "https://demo.example",
        "hosts": ["demo.example"],
        "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"},
    }
    payload.update(overrides)
    return payload


def _write(directory: Path, filename: str, payload: Any) -> Path:
    path = directory / filename
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class _FakeResponse:
    """Minimal ``HTTPResponse`` stand-in used by the ``urlopen`` monkeypatch."""

    def __init__(
        self,
        body: bytes | str = b"",
        *,
        content_type: str | None = "application/json",
        status: int = 200,
        content_length: int | None = None,
    ) -> None:
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self._pos = 0
        self.headers: dict[str, str] = {}
        if content_type is not None:
            self.headers["Content-Type"] = content_type
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.status = status
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._body[self._pos :]
            self._pos = len(self._body)
        else:
            chunk = self._body[self._pos : self._pos + size]
            self._pos += len(chunk)
        return chunk

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        self.closed = True


class _Opener:
    """Callable ``urlopen`` replacement that records its arguments."""

    def __init__(
        self,
        response: _FakeResponse | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.request: Any = None
        self.timeout: float | None = None

    def __call__(self, request: Any, timeout: float | None = None) -> _FakeResponse:
        self.request = request
        self.timeout = timeout
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


# --------------------------------------------------------------------------- #
# fetch_rule
# --------------------------------------------------------------------------- #
def test_fetch_rule_returns_the_json_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps(_rule("remote-site")).encode("utf-8")
    opener = _Opener(_FakeResponse(body))
    monkeypatch.setattr(rule_remote, "urlopen", opener)

    data = fetch_rule("https://rules.example/site.json", timeout=7)

    assert data["site_key"] == "remote-site"
    assert opener.timeout == 7
    assert opener.request is not None
    assert opener.request.full_url == "https://rules.example/site.json"
    headers = {key.lower(): value for key, value in opener.request.header_items()}
    assert "user-agent" in headers
    assert headers["accept-encoding"] == "identity"


def test_fetch_rule_accepts_a_utf8_bom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"\xef\xbb\xbf" + json.dumps(_rule("bom-site")).encode("utf-8")
    monkeypatch.setattr(
        rule_remote, "urlopen", _Opener(_FakeResponse(body, content_type="text/plain"))
    )

    assert fetch_rule("https://rules.example/bom.json")["site_key"] == "bom-site"


@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "",
        "application/json; charset=utf-8",
        "application/vnd.api+json",
        "application/x-json",
        "text/json",
        "text/plain",
        "application/octet-stream",
        "text/unknown-but-lenient",
    ],
)
def test_fetch_rule_accepts_lenient_content_types(
    monkeypatch: pytest.MonkeyPatch, content_type: str | None
) -> None:
    body = json.dumps(_rule("ct-site")).encode("utf-8")
    monkeypatch.setattr(
        rule_remote,
        "urlopen",
        _Opener(_FakeResponse(body, content_type=content_type)),
    )

    assert fetch_rule("https://rules.example/ct.json")["site_key"] == "ct-site"


@pytest.mark.parametrize(
    "content_type",
    [
        "text/html",
        "application/xhtml+xml",
        "application/xml",
        "text/xml",
        "image/png",
        "application/zip",
    ],
)
def test_fetch_rule_rejects_obvious_non_json_content_types(
    monkeypatch: pytest.MonkeyPatch, content_type: str
) -> None:
    monkeypatch.setattr(
        rule_remote,
        "urlopen",
        _Opener(_FakeResponse(b"{}", content_type=content_type)),
    )

    with pytest.raises(RuleError, match="Content-Type"):
        fetch_rule("https://rules.example/not-json")


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("timed out"),
        URLError(TimeoutError("timed out")),
        URLError("read timeout"),
    ],
)
def test_fetch_rule_reports_timeouts(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    monkeypatch.setattr(rule_remote, "urlopen", _Opener(error=error))

    with pytest.raises(RuleError, match="timed out"):
        fetch_rule("https://rules.example/slow.json", timeout=2)


def test_fetch_rule_reports_generic_network_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rule_remote, "urlopen", _Opener(error=URLError("connection refused"))
    )

    with pytest.raises(RuleError, match="connection refused"):
        fetch_rule("https://rules.example/down.json")


def test_fetch_rule_reports_an_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = HTTPError("https://rules.example/missing.json", 404, "Not Found", {}, None)
    monkeypatch.setattr(rule_remote, "urlopen", _Opener(error=error))

    with pytest.raises(RuleError, match="404"):
        fetch_rule("https://rules.example/missing.json")


def test_fetch_rule_rejects_a_non_2xx_status_from_the_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rule_remote,
        "urlopen",
        _Opener(_FakeResponse(b"{}", status=500)),
    )

    with pytest.raises(RuleError, match="500"):
        fetch_rule("https://rules.example/error.json")


def test_fetch_rule_caps_the_body_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeResponse(b'{"site_key": "too-big"}')
    monkeypatch.setattr(rule_remote, "urlopen", _Opener(response))

    with pytest.raises(RuleError, match="too large"):
        fetch_rule("https://rules.example/big.json", max_bytes=8)
    assert response.closed is True


def test_fetch_rule_trusts_an_oversized_content_length_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeResponse(b"{}", content_length=999_999)
    monkeypatch.setattr(rule_remote, "urlopen", _Opener(response))

    with pytest.raises(RuleError, match="too large"):
        fetch_rule("https://rules.example/big.json", max_bytes=1024)


def test_fetch_rule_accepts_a_body_exactly_at_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps(_rule("exact-site")).encode("utf-8")
    monkeypatch.setattr(rule_remote, "urlopen", _Opener(_FakeResponse(body)))

    assert (
        fetch_rule("https://rules.example/exact.json", max_bytes=len(body))["site_key"]
        == "exact-site"
    )


@pytest.mark.parametrize(
    "body",
    [b"<html>not json</html>", b"{not json", b""],
)
def test_fetch_rule_reports_invalid_json(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    monkeypatch.setattr(
        rule_remote,
        "urlopen",
        _Opener(_FakeResponse(body, content_type="text/plain")),
    )

    with pytest.raises(RuleError, match="invalid JSON"):
        fetch_rule("https://rules.example/broken.json")


def test_fetch_rule_requires_a_json_object_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rule_remote,
        "urlopen",
        _Opener(_FakeResponse(b'[{"site_key": "x"}]')),
    )

    with pytest.raises(RuleError, match="JSON object"):
        fetch_rule("https://rules.example/list.json")


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "ftp://rules.example/a.json", "data:application/json,{}"],
)
def test_fetch_rule_rejects_non_http_schemes(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    opener = _Opener(_FakeResponse(b"{}"))
    monkeypatch.setattr(rule_remote, "urlopen", opener)

    with pytest.raises(RuleError, match="scheme"):
        fetch_rule(url)
    assert opener.request is None


@pytest.mark.parametrize("url", ["", "   ", "https:///no-host", "not-a-url"])
def test_fetch_rule_rejects_invalid_urls(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    opener = _Opener(_FakeResponse(b"{}"))
    monkeypatch.setattr(rule_remote, "urlopen", opener)

    with pytest.raises(RuleError):
        fetch_rule(url)
    assert opener.request is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": 0},
        {"timeout": -1},
        {"timeout": True},
        {"max_bytes": 0},
        {"max_bytes": -5},
        {"max_bytes": True},
    ],
)
def test_fetch_rule_rejects_invalid_limits(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]
) -> None:
    opener = _Opener(_FakeResponse(b"{}"))
    monkeypatch.setattr(rule_remote, "urlopen", opener)

    with pytest.raises(RuleError, match="positive"):
        fetch_rule("https://rules.example/a.json", **kwargs)


# --------------------------------------------------------------------------- #
# install_rule
# --------------------------------------------------------------------------- #
def test_install_mapping_uses_the_site_key_as_filename(
    _isolated_rules: Path,
) -> None:
    target = install_rule(_rule("mapping-site"))

    assert target == _isolated_rules / "mapping-site.json"
    assert target.is_file()
    assert json.loads(target.read_text(encoding="utf-8"))["site_key"] == "mapping-site"
    assert "mapping-site" in load_rules()
    assert not any(p.name.startswith(".rule-") for p in _isolated_rules.iterdir())


def test_install_path_keeps_the_source_stem(
    tmp_path: Path, _isolated_rules: Path
) -> None:
    source = _write(tmp_path, "my-source.json", _rule("path-site"))

    target = install_rule(source)

    assert target == _isolated_rules / "my-source.json"
    assert json.loads(target.read_text(encoding="utf-8"))["site_key"] == "path-site"


def test_install_name_overrides_the_filename(_isolated_rules: Path) -> None:
    target = install_rule(_rule("named-site"), name="chosen.json")

    assert target == _isolated_rules / "chosen.json"
    assert target.is_file()


@pytest.mark.parametrize(
    ("unsafe", "expected"),
    [
        ("../../evil", "evil.json"),
        ("..\\..\\evil.json", "evil.json"),
        ("a/b/c", "c.json"),
        ("CON", "rule_CON.json"),
        ("_draft", "draft.json"),
        (".hidden", "hidden.json"),
        ("..", "safe-site.json"),
        ("   ", "safe-site.json"),
    ],
)
def test_install_sanitizes_unsafe_names(
    _isolated_rules: Path, unsafe: str, expected: str
) -> None:
    target = install_rule(_rule("safe-site"), name=unsafe)

    assert target.name == expected
    assert target.parent == _isolated_rules
    assert target.is_file()


def test_install_truncates_extremely_long_names(_isolated_rules: Path) -> None:
    target = install_rule(_rule("long-site"), name="x" * 500)

    assert target.parent == _isolated_rules
    assert target.suffix == ".json"
    assert len(target.stem) <= 100


def test_install_refuses_to_overwrite_by_default(_isolated_rules: Path) -> None:
    first = install_rule(_rule("overwrite-site", base_url="https://one.example"))

    with pytest.raises(RuleError, match="already exists"):
        install_rule(_rule("overwrite-site", base_url="https://two.example"))

    assert "one.example" in first.read_text(encoding="utf-8")
    assert len(list(_isolated_rules.glob("*.json"))) == 1


def test_install_overwrites_when_asked(_isolated_rules: Path) -> None:
    target = install_rule(_rule("overwrite-site", base_url="https://one.example"))

    returned = install_rule(
        _rule("overwrite-site", base_url="https://two.example"), overwrite=True
    )

    assert returned == target
    assert "two.example" in target.read_text(encoding="utf-8")


def test_install_force_is_a_backwards_compatible_alias(_isolated_rules: Path) -> None:
    target = install_rule(_rule("force-site", base_url="https://one.example"))

    install_rule(_rule("force-site", base_url="https://two.example"), force=True)

    assert "two.example" in target.read_text(encoding="utf-8")


def test_install_validation_failure_writes_nothing(_isolated_rules: Path) -> None:
    with pytest.raises(RuleError):
        install_rule({"site_key": "incomplete"})

    assert list(_isolated_rules.iterdir()) == []


def test_install_accepts_an_existing_target_as_a_symlink(
    _isolated_rules: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_rule("hijacked")), encoding="utf-8")
    link = _isolated_rules / "hijacked.json"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")

    with pytest.raises(RuleError, match="already exists"):
        install_rule(_rule("hijacked"))

    assert outside.read_text(encoding="utf-8") == json.dumps(
        _rule("hijacked"), ensure_ascii=False
    )


def test_install_invalid_path_or_json_writes_nothing(
    tmp_path: Path, _isolated_rules: Path
) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(RuleError, match="cannot read"):
        install_rule(missing)
    assert list(_isolated_rules.iterdir()) == []

    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    with pytest.raises(RuleError, match="invalid JSON"):
        install_rule(broken)
    assert list(_isolated_rules.iterdir()) == []


def test_install_rejects_a_non_mapping_or_path() -> None:
    with pytest.raises(RuleError, match="mapping or a file path"):
        install_rule(123)  # type: ignore[arg-type]


def test_install_reports_an_unserializable_mapping(_isolated_rules: Path) -> None:
    payload = _rule("unserializable")
    payload["extra"] = {"object": object()}

    with pytest.raises(RuleError, match="not JSON-serializable"):
        install_rule(payload)

    assert list(_isolated_rules.iterdir()) == []


# --------------------------------------------------------------------------- #
# refresh_rules
# --------------------------------------------------------------------------- #
def test_refresh_classifies_added_updated_and_failed(
    _isolated_rules: Path,
) -> None:
    _write(_isolated_rules, "new.json", _rule("brand-new"))
    _write(
        _isolated_rules,
        "shadow.json",
        _rule("biquge5r", base_url="https://shadow.example"),
    )
    (_isolated_rules / "broken.json").write_text("{ not json", encoding="utf-8")

    report = refresh_rules()

    assert report["added"] == ["brand-new"]
    assert report["updated"] == ["biquge5r"]
    assert set(report["failed"]) == {"broken"}
    assert "invalid JSON" in report["failed"]["broken"]
    # The reload took effect for both the new and the overridden source.
    assert load_rules()["brand-new"].base_url == "https://demo.example"
    assert load_rules()["biquge5r"].base_url == "https://shadow.example"


def test_refresh_is_empty_without_user_rules(_isolated_rules: Path) -> None:
    assert refresh_rules() == {"added": [], "updated": [], "failed": {}}


def test_refresh_filters_by_filename_and_site_key(_isolated_rules: Path) -> None:
    _write(_isolated_rules, "new.json", _rule("brand-new"))
    _write(
        _isolated_rules,
        "shadow.json",
        _rule("biquge5r", base_url="https://shadow.example"),
    )

    by_file = refresh_rules(names=["shadow.json"])
    assert by_file == {"added": [], "updated": ["biquge5r"], "failed": {}}

    by_key = refresh_rules(names=["brand-new"])
    assert by_key == {"added": ["brand-new"], "updated": [], "failed": {}}


def test_refresh_reports_unknown_names_but_tolerates_builtins(
    _isolated_rules: Path,
) -> None:
    unknown = refresh_rules(names=["ghost-site"])
    assert set(unknown["failed"]) == {"ghost-site"}
    assert "unknown" in unknown["failed"]["ghost-site"].lower()

    builtin_only = refresh_rules(names=["biquge5r"])
    assert builtin_only == {"added": [], "updated": [], "failed": {}}


def test_refresh_never_uses_the_network(
    _isolated_rules: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(_isolated_rules, "new.json", _rule("offline-site"))

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("refresh_rules must not perform network I/O")

    monkeypatch.setattr(rule_remote, "urlopen", _boom)

    report = refresh_rules()

    assert report["added"] == ["offline-site"]


def test_refresh_skips_template_files_and_duplicates(
    _isolated_rules: Path,
) -> None:
    _write(_isolated_rules, "_template.json", _rule("template-site"))
    _write(_isolated_rules, "a.json", _rule("dupe-site"))
    _write(_isolated_rules, "b.json", _rule("dupe-site"))

    report = refresh_rules()

    assert report["added"] == ["dupe-site"]
    assert "template-site" not in report["added"]
