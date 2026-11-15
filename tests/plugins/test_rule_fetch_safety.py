#!/usr/bin/env python3
"""SSRF / redirect / protocol allow-list tests for the rule fetch path.

Threat model
------------
A rule document is *local configuration*, but every URL it ends up fetching can
be influenced by page data:

* a chapter link is scraped from an untrusted page (``href``/``src``),
* a rendered template can be nothing but a placeholder
  (``"chapter": {"url": "{chapter_id}"}``), so page data becomes the whole URL,
* a cover URL is page data and is downloaded into the media directory.

The backends disagree about what is safe: curl reads ``file:///`` off the local
disk and follows ``ftp:``/``gopher:`` redirects, while aiohttp/httpx refuse
those but happily send ``user:pass@`` as Basic auth.  These tests pin the
shared allow-list that every rule fetch now goes through, and document the
remaining risk (loopback/private hosts are allowed on purpose).

The only network used here is a loopback mock server on an ephemeral port.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.infra.sessions import available_backends, create_session
from novel_downloader.plugins.rule import remote as rule_remote
from novel_downloader.plugins.rule.engine import abs_url, http_url, is_fetchable_url
from novel_downloader.plugins.rule.fetcher import RuleFetcher
from novel_downloader.plugins.rule.model import Rule, RuleError, parse_rule
from novel_downloader.plugins.rule.parser import RuleParser
from novel_downloader.plugins.rule.searcher import RuleSearcher
from novel_downloader.plugins.utils.url_safety import (
    MAX_FETCH_URL_LENGTH,
    has_url_control_chars,
    is_http_fetch_url,
)
from novel_downloader.schemas import FetcherConfig, ParserConfig

BASE = "https://demo.example"

#: Schemes that must never reach an HTTP backend.
NON_HTTP_URLS = [
    "file:///etc/passwd",
    "FILE:///C:/Windows/win.ini",
    "ftp://host.example/file.txt",
    "gopher://host.example/_test",
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "data:image/png;base64,iVBORw0KGgo=",
]

#: Loopback / private / link-local / cloud-metadata targets.  These are
#: *allowed* by the rule fetch policy (see the test that documents it); the
#: remote-rule path is the one that must refuse them.
PRIVATE_URLS = [
    "http://127.0.0.1/x",
    "http://localhost/x",
    "http://[::1]/x",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.7.7/x",
    "http://172.16.5.4/x",
    "http://192.168.1.20/x",
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _rule(**sections: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "site_key": "demo",
        "base_url": BASE,
        "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"},
    }
    payload.update(sections)
    return payload


def _parser(rule: Rule) -> RuleParser:
    cls = type(
        f"RuleParser_{rule.site_key}",
        (RuleParser,),
        {"_rule": rule, "site_name": rule.site_key},
    )
    return cls(ParserConfig())


def _searcher(rule: Rule) -> RuleSearcher:
    cls = type(
        f"RuleSearcher_{rule.site_key}",
        (RuleSearcher,),
        {"_rule": rule, "site_name": rule.site_key, "BASE_URL": rule.base_url},
    )
    return cls(session=None)  # type: ignore[arg-type]


def _fetcher(rule: Rule, *, backend: str = "aiohttp") -> RuleFetcher:
    cls = type(
        f"RuleFetcher_{rule.site_key}",
        (RuleFetcher,),
        {
            "_rule": rule,
            "site_name": rule.site_key,
            "DEFAULT_BASE_URL": rule.base_url,
        },
    )
    return cls(
        FetcherConfig(
            backend=backend,
            max_rps=0,
            retry_times=0,
            request_interval=0,
            timeout=5,
        )
    )


class _RecordingFetcher(RuleFetcher):
    """A rule fetcher whose HTTP layer records exactly what it was handed."""

    def __init__(self, rule: Rule) -> None:
        self._rule = rule
        self._base_url = ""
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def fetch(self, url: str, encoding: str = "utf-8", **kwargs: Any) -> str:
        self.calls.append((url, encoding, dict(kwargs)))
        return "<html><body>ok</body></html>"


@dataclass
class _Site:
    base: str
    server: Any

    @property
    def seen(self) -> list[tuple[str, dict[str, str]]]:
        return self.server.seen


class _Handler(BaseHTTPRequestHandler):
    """Serve the routes configured on the server instance."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler hook
        server: Any = self.server
        path = self.path.split("?", 1)[0]
        server.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        status, location, body = server.routes.get(path, (404, "", b"missing"))
        if status in (301, 302, 303, 307, 308):
            self.send_response(status)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Keep the test output free of per-request access logs."""


@pytest.fixture
def site() -> Iterator[_Site]:
    """A loopback mock site on an ephemeral port (never a real network)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    server.seen = []  # type: ignore[attr-defined]
    server.routes = {  # type: ignore[attr-defined]
        "/ok": (200, "", b"<html><body>ok</body></html>"),
        "/to-file": (302, "file:///C:/Windows/win.ini", b""),
        "/to-gopher": (302, "gopher://127.0.0.1:1/_x", b""),
        "/to-ftp": (302, "ftp://127.0.0.1:1/x", b""),
        "/to-loopback": (302, f"{base}/ok", b""),
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Site(base=base, server=server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# 1. non-HTTP schemes in rules and in page data
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url", NON_HTTP_URLS)
def test_rule_url_templates_reject_non_http_schemes(url: str) -> None:
    for sections in (
        {"chapter": {"url": url}},
        {"book": {"url": url, "chapters": {"item": "a"}}},
        {"book": {"url": "u", "chapters": {"item": "a"}, "pages": {"url": url}}},
        {"search": {"url": url}, "chapter": {"url": "u"}},
        {"base_url": url, "chapter": {"url": "u"}},
    ):
        with pytest.raises(RuleError, match="unsupported URL scheme"):
            parse_rule({"site_key": "demo", **sections})


@pytest.mark.parametrize("url", NON_HTTP_URLS)
def test_page_supplied_non_http_links_are_dropped(url: str) -> None:
    assert http_url(BASE, url) == ""
    assert is_fetchable_url(url) is False
    assert is_http_fetch_url(url) is False


@pytest.mark.parametrize("url", NON_HTTP_URLS)
def test_fetcher_refuses_a_rendered_non_http_url(url: str) -> None:
    """``{chapter_id}`` can be the entire URL, so the render is re-checked."""
    rule = parse_rule({"site_key": "demo", "chapter": {"url": "{chapter_id}"}})
    fetcher = _RecordingFetcher(rule)

    with pytest.raises(RuleError, match="non-HTTP"):
        _run(fetcher.fetch_chapter_content("7", url))
    assert fetcher.calls == []


def test_chapter_links_with_non_http_schemes_are_skipped() -> None:
    rule = parse_rule(
        {
            "site_key": "demo",
            "base_url": BASE,
            "book": {"url": "{base_url}/book/{book_id}/", "chapters": {"item": "a"}},
        }
    )
    page = (
        "<div>"
        '<a href="javascript:alert(1)">js</a>'
        '<a href="file:///etc/passwd">file</a>'
        '<a href="data:text/html,x">data</a>'
        '<a href="ftp://host/x">ftp</a>'
        '<a href="/c/1.html">ok</a>'
        "</div>"
    )

    info = _parser(rule).parse_book_info([page])

    assert info is not None
    assert [c["url"] for c in info["volumes"][0]["chapters"]] == [f"{BASE}/c/1.html"]


def test_search_results_with_non_http_schemes_are_skipped() -> None:
    rule = parse_rule(
        {
            "site_key": "demo",
            "base_url": BASE,
            "search": {
                "url": "{base_url}/s?q={keyword}",
                "item": "div.r",
                "fields": {
                    "title": {"selector": "a", "attr": "text"},
                    "url": {"selector": "a", "attr": "href"},
                    "cover": {"selector": "img", "attr": "src"},
                },
            },
            "chapter": {"url": "{base_url}/c/{chapter_id}.html"},
        }
    )
    html = (
        '<div class="r"><a href="javascript:alert(1)">bad</a>'
        '<img src="data:image/png;base64,AAAA"></div>'
        '<div class="r"><a href="/b/1/">ok</a>'
        '<img src="https://cdn.example/1.jpg"></div>'
    )

    results = _searcher(rule)._parse_html(html)

    assert [r["title"] for r in results] == ["ok"]
    assert results[0]["cover_url"] == "https://cdn.example/1.jpg"


def test_book_cover_with_a_non_http_scheme_is_cleared() -> None:
    """A scraped cover reaches ``fetch_image``, so it gets the same allow-list."""
    rule = parse_rule(
        {
            "site_key": "demo",
            "base_url": BASE,
            "book": {
                "url": "{base_url}/book/{book_id}/",
                "fields": {"cover_url": {"selector": "img", "attr": "src"}},
                "chapters": {"item": "a"},
            },
        }
    )
    page = '<a href="/c/1.html">1</a><img class="x" src="file:///etc/passwd">'

    info = _parser(rule).parse_book_info([page])

    assert info is not None
    assert info["cover_url"] == ""


# --------------------------------------------------------------------------- #
# 2. loopback / private / metadata targets: documented policy
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url", PRIVATE_URLS)
def test_private_targets_are_allowed_for_local_rules(url: str) -> None:
    """A rule is local configuration; LAN/self-hosted sources are legitimate.

    This is a deliberate, documented trade-off, not an oversight: the
    untrusted path (downloading a rule from a URL) applies a public-only
    policy, and the media fetcher only reaches these hosts if the local rule
    asks for them.  A malicious *page* can still point a chapter/cover at a
    loopback service; that is recorded in the audit report as residual risk.
    """
    assert is_fetchable_url(url) is True
    assert http_url(BASE, url) == url


@pytest.mark.parametrize("url", PRIVATE_URLS)
def test_remote_rule_loading_refuses_private_targets(url: str) -> None:
    """The untrusted path is stricter than the local rule path."""
    with pytest.raises(RuleError):
        rule_remote.check_rule_url(url)


def test_loopback_base_url_still_parses_for_local_test_sites() -> None:
    rule = parse_rule(
        {
            "site_key": "local",
            "base_url": "http://127.0.0.1:8080",
            "chapter": {"url": "{base_url}/c/{chapter_id}.html"},
        }
    )

    assert rule.base_url == "http://127.0.0.1:8080"


# --------------------------------------------------------------------------- #
# 3. redirects (loopback mock server)
# --------------------------------------------------------------------------- #
def _session(backend: str) -> Any:
    return create_session(
        backend,
        FetcherConfig(
            backend=backend,
            max_rps=0,
            retry_times=0,
            request_interval=0,
            timeout=5,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", available_backends())
@pytest.mark.parametrize("route", ["/to-file", "/to-gopher"])
async def test_redirect_to_a_non_http_scheme_is_refused(
    site: _Site, backend: str, route: str
) -> None:
    session = _session(backend)
    await session.init()
    try:
        with pytest.raises((ConnectionError, ValueError, OSError)):
            await session.get(site.base + route)
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend", [name for name in available_backends() if name != "curl_cffi"]
)
async def test_redirect_to_ftp_is_refused(site: _Site, backend: str) -> None:
    """aiohttp/httpx refuse ``ftp:`` before opening a socket.

    curl_cffi is excluded here: libcurl *does* try to speak FTP, and waiting
    for that connection would slow the suite down.  Its protocol allow-list
    (``PROTOCOLS_STR`` / ``REDIR_PROTOCOLS_STR``) is covered by
    :func:`test_curl_backend_refuses_a_direct_file_url` and by the
    ``file:``/``gopher:`` redirect cases above.
    """
    session = _session(backend)
    await session.init()
    try:
        with pytest.raises((ConnectionError, ValueError, OSError)):
            await session.get(site.base + "/to-ftp")
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    "curl_cffi" not in available_backends(), reason="curl_cffi not installed"
)
async def test_curl_backend_refuses_a_direct_file_url() -> None:
    """The curl backend used to read ``file://`` straight off the disk."""
    session = _session("curl_cffi")
    await session.init()
    try:
        with pytest.raises((ConnectionError, ValueError, OSError)):
            await session.get("file:///C:/Windows/win.ini")
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_http_redirect_to_loopback_is_followed(site: _Site) -> None:
    """Risk record: an HTTP 302 to a loopback host is followed by the session.

    Blocking that would also break legitimate self-hosted/local rule sites and
    proxies, so the policy stops at scheme validation.  Callers that consume
    untrusted remote rules re-validate the final URL (see
    ``rule.remote.check_rule_redirect``).
    """
    session = _session("aiohttp")
    await session.init()
    try:
        response = await session.get(site.base + "/to-loopback")
    finally:
        await session.close()

    assert response.status == 200
    assert "ok" in response.text


def test_remote_rule_redirect_to_private_target_is_rejected() -> None:
    with pytest.raises(RuleError, match="redirect rejected"):
        rule_remote.check_rule_redirect(
            "https://rules.example/r.json", "http://127.0.0.1/r.json"
        )


# --------------------------------------------------------------------------- #
# 4. relative / protocol-relative / absolute links share the allow-list
# --------------------------------------------------------------------------- #
def test_relative_protocol_relative_and_absolute_links_resolve() -> None:
    assert http_url(BASE, "/c/1.html") == f"{BASE}/c/1.html"
    assert http_url(BASE, "c/1.html") == f"{BASE}/c/1.html"
    assert http_url(BASE, "//cdn.example/a.png") == "https://cdn.example/a.png"
    assert http_url(BASE, "https://other.example/a") == "https://other.example/a"
    assert http_url(BASE, "http://other.example/a") == "http://other.example/a"


def test_protocol_relative_links_are_still_policy_checked() -> None:
    assert http_url(BASE, "//user:pass@cdn.example/a.png") == ""
    assert http_url(BASE, "//127.0.0.1/x") == "https://127.0.0.1/x"
    assert http_url(BASE, "//[::1]/x") == "https://[::1]/x"
    assert http_url(BASE, "//") == ""


def test_absolute_link_with_host_confusion_is_rejected() -> None:
    # ``urlsplit`` reads these as ``evil.com`` / ``127.0.0.1`` while a browser
    # may not; both spellings carry userinfo, which the policy refuses.
    assert http_url(BASE, "http://127.0.0.1\\@evil.com/x") == ""
    assert http_url(BASE, "http://evil.com\\@127.0.0.1/x") == ""
    assert http_url(BASE, "http://user:pass@127.0.0.1/x") == ""


# --------------------------------------------------------------------------- #
# 5. over-long / userinfo / control-character URLs
# --------------------------------------------------------------------------- #
def test_overlong_urls_are_rejected() -> None:
    long_url = "https://demo.example/" + "a" * MAX_FETCH_URL_LENGTH

    assert is_fetchable_url(long_url) is False
    assert is_http_fetch_url(long_url) is False
    assert http_url(BASE, long_url) == ""
    assert abs_url(BASE, long_url) == ""

    with pytest.raises(RuleError, match="too long"):
        parse_rule({"site_key": "demo", "chapter": {"url": long_url}})


def test_userinfo_in_a_rule_url_is_rejected_at_load_time() -> None:
    with pytest.raises(RuleError, match="credentials"):
        parse_rule(
            {
                "site_key": "demo",
                "chapter": {"url": "https://user:pass@demo.example/c/1.html"},
            }
        )
    with pytest.raises(RuleError, match="credentials"):
        parse_rule(
            {
                "site_key": "demo",
                "base_url": "https://token@demo.example",
                "chapter": {"url": "u"},
            }
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://demo.example/a\r\nX-Injected: 1",
        "https://demo.example/a\nX-Injected: 1",
        "https://demo.example/a\x00b",
        "https://demo.example/a\x01b",
        "https://demo.example/a\x0bb",
        "https://demo.example/a\x7fb",
    ],
)
def test_control_characters_in_urls_are_rejected(url: str) -> None:
    assert has_url_control_chars(url) is True
    assert is_fetchable_url(url) is False
    assert http_url(BASE, url) == ""
    with pytest.raises(RuleError, match="control characters"):
        parse_rule({"site_key": "demo", "chapter": {"url": url}})


# --------------------------------------------------------------------------- #
# 6. header injection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "headers",
    [
        {"X-Test": "a\r\nX-Injected: 1"},
        {"X-Test": "a\nX-Injected: 1"},
        {"X-Test": "a\x00b"},
        {"X-Test": "a\x0bb"},
        {"X-Test": "a\x0cb"},
        {"X-Test": "a\x01b"},
        {"X-Test": "a\x7fb"},
        {"X-Test\r\nX-Injected": "1"},
        {"X: Y": "z"},
    ],
)
def test_rule_headers_with_control_characters_are_rejected(
    headers: dict[str, str],
) -> None:
    with pytest.raises(RuleError, match="control characters"):
        parse_rule({"site_key": "demo", "chapter": {"url": "u", "headers": headers}})


@pytest.mark.asyncio
async def test_rendered_header_cannot_inject_an_extra_header(site: _Site) -> None:
    """``{book_id}`` is page data; its CRLF must never become a new header."""
    rule = parse_rule(
        {
            "site_key": "demo",
            "base_url": site.base,
            "chapter": {
                "url": "{base_url}/ok",
                "headers": {"X-Book": "{book_id}", "Referer": "{base_url}/"},
            },
        }
    )
    fetcher = _fetcher(rule)
    await fetcher.init()
    try:
        pages = await fetcher.fetch_chapter_content("7\r\nX-Injected: 1", "5")
    finally:
        await fetcher.close()

    assert pages and "ok" in pages[0]
    _, sent = site.seen[-1]
    assert "x-injected" not in sent
    assert sent["x-book"] == "7X-Injected: 1"
    assert sent["referer"] == f"{site.base}/"
    assert "authorization" not in sent


# --------------------------------------------------------------------------- #
# 7. proxy / timeout cannot be selected by the URL
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_url_query_cannot_override_proxy_or_timeout(site: _Site) -> None:
    """A URL is data; it must not become a transport option.

    If ``?proxy=...`` or ``?timeout=...`` were interpreted as request options,
    this request would either go to the closed port or time out immediately.
    """
    rule = parse_rule(
        {
            "site_key": "demo",
            "base_url": site.base,
            "chapter": {"url": "{base_url}/ok?timeout=0.001&proxy=http://127.0.0.1:9"},
        }
    )
    fetcher = _fetcher(rule)
    await fetcher.init()
    try:
        pages = await fetcher.fetch_chapter_content("7", "5")
    finally:
        await fetcher.close()

    assert pages and "ok" in pages[0]
    assert site.seen[-1][0].startswith("/ok?timeout=0.001&proxy=")


@pytest.mark.asyncio
async def test_rule_fetcher_forwards_no_transport_options() -> None:
    rule = parse_rule(
        {
            "site_key": "demo",
            "base_url": BASE,
            "chapter": {
                "url": "{base_url}/ok?timeout=0&proxy=http://127.0.0.1:9",
                "headers": {"X-Test": "1"},
            },
        }
    )
    fetcher = _RecordingFetcher(rule)

    await fetcher.fetch_chapter_content("7", "5")

    url, encoding, kwargs = fetcher.calls[0]
    assert url == f"{BASE}/ok?timeout=0&proxy=http://127.0.0.1:9"
    assert encoding == "utf-8"
    assert set(kwargs) == {"headers"}
    assert "timeout" not in kwargs and "proxy" not in kwargs
    assert kwargs["headers"] == {"X-Test": "1"}


# --------------------------------------------------------------------------- #
# 8. media downloads are gated too
# --------------------------------------------------------------------------- #
def test_media_fetch_refuses_non_http_urls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """curl reads ``file://`` off disk; the media path must refuse it first."""
    from tests.plugins.test_base_fetcher import _make

    calls: list[str] = []

    def handler(url: str, kwargs: dict[str, Any]) -> Any:
        calls.append(url)
        raise AssertionError("the session must not be reached")

    fetcher, _ = _make(monkeypatch, handler)

    for url in NON_HTTP_URLS:
        assert _run(fetcher.fetch_image(url, tmp_path)) is None
        assert _run(fetcher.fetch_data(url)) is None
    assert calls == []
    assert list(tmp_path.iterdir()) == []


def test_media_fetch_still_accepts_http_urls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from novel_downloader.infra.sessions.response import BaseResponse
    from tests.plugins.test_base_fetcher import _make

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    fetcher, _ = _make(
        monkeypatch,
        lambda url, kw: BaseResponse(
            content=png, headers={"Content-Type": "image/png"}
        ),
    )

    path = _run(fetcher.fetch_image("https://cdn.example/a.png", tmp_path))

    assert path is not None and path.read_bytes() == png


# --------------------------------------------------------------------------- #
# helper: run a coroutine without an event loop (sync tests)
# --------------------------------------------------------------------------- #
def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)
