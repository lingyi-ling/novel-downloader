#!/usr/bin/env python3
"""
tests.apps.test_desktop_link_safety
-----------------------------------

Security tests for every path that can move the user out of the pywebview
front end: the ``open_url`` bridge method, the release-page payload and the
static DOM link handlers.

The bridge filters URLs itself so a page (or a compromised script running in
it) cannot hand ``file:``, ``javascript:``, ``data:`` or ``vbscript:`` to the
OS handler.  The front end repeats the same filter because site-returned book
and cover URLs are rendered before any Python code sees them, and because a
link click must never turn into an in-app navigation: the remote document
would inherit the window's ``js_api`` bridge.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from novel_downloader.apps.common.updater import UpdateInfo
from novel_downloader.apps.desktop import webview_app as wa
from novel_downloader.apps.desktop.web_assets import APP_HTML

# ---------------------------------------------------------------------------
# Python URL validator
# ---------------------------------------------------------------------------

_ACCEPTED = [
    ("https://example.com/book/1", "https://example.com/book/1"),
    ("http://example.com/a?x=1#frag", "http://example.com/a?x=1#frag"),
    (" HTTPS://Example.COM/Path ", "https://Example.COM/Path"),
    ("https://example.com:8443/a", "https://example.com:8443/a"),
    ("https://例え.jp/本", "https://例え.jp/本"),
]

_REJECTED = [
    "",
    "   ",
    "book/1",
    "/book/1",
    "//example.com/book",
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "file:///C:/Windows/win.ini",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "ftp://example.com/book",
    "mailto:user@example.com",
    "https://user:pass@example.com/book",
    "https://user@example.com/book",
    "https://example.com%0a@evil.example/book",
    "https://good.example\\@evil.example/book",
    "https://example.com/\\book",
    "https://example.com/a\x00b",
    "https://example.com/a\nb",
    "https://example.com/a b",
    "https://example.com/a\tb",
    "https://example.com:bad/book",
    "https://example.com:0/book",
    "https://example.com:65536/book",
    "https://",
    "https:///book",
    "https://example.com/" + "a" * 3000,
]


@pytest.mark.parametrize(("raw", "expected"), _ACCEPTED)
def test_safe_external_url_accepts_and_normalises_web_urls(
    raw: str, expected: str
) -> None:
    assert wa._safe_external_url(raw) == expected


@pytest.mark.parametrize("raw", _REJECTED)
def test_safe_external_url_rejects_non_web_or_ambiguous_values(raw: str) -> None:
    assert wa._safe_external_url(raw) == ""


def test_safe_external_url_rejects_non_strings_without_raising() -> None:
    assert wa._safe_external_url(None) == ""
    assert wa._safe_external_url({"url": "https://example.com"}) == ""


def test_safe_external_url_honours_a_custom_scheme_allowlist() -> None:
    assert wa._safe_external_url("http://example.com/x", schemes=("https",)) == ""
    assert (
        wa._safe_external_url("https://example.com/x", schemes=("https",))
        == "https://example.com/x"
    )


# ---------------------------------------------------------------------------
# release page: HTTPS only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "http://github.com/lingyi-ling/novel-downloader/releases",
        "javascript:alert(1)",
        "file:///C:/Windows/win.ini",
        "https://user:pass@github.com/lingyi-ling/novel-downloader/releases",
        "//github.com/lingyi-ling/novel-downloader/releases",
    ],
)
def test_safe_release_url_falls_back_for_non_https_values(bad: str) -> None:
    assert wa._safe_release_url(bad) == wa._RELEASES_PAGE


def test_safe_release_url_keeps_an_https_release_page() -> None:
    page = "https://github.com/lingyi-ling/novel-downloader/releases/tag/v1.2.3"
    assert wa._safe_release_url(page) == page


def test_update_payload_never_serves_a_non_https_release_page() -> None:
    """A cached/crafted ``html_url`` cannot downgrade the update-bar link."""
    cache = wa._UpdateCache(
        info=UpdateInfo(
            current="1.0.0",
            latest="2.0.0",
            available=True,
            notes_url="javascript:alert(1)",
            asset=None,
            assets=(),
        ),
        error=None,
        checked_at=1.0,
        current="1.0.0",
        platform="windows-x64",
    )
    payload = wa.ApiBridge()._update_payload(cache, prefs={})
    assert payload["notes_url"] == wa._RELEASES_PAGE
    assert payload["url"] == wa._RELEASES_PAGE


# ---------------------------------------------------------------------------
# bridge method
# ---------------------------------------------------------------------------


def _capture_browser(
    monkeypatch: pytest.MonkeyPatch, *, opened: bool = True
) -> list[str]:
    seen: list[str] = []

    def fake_open(url: str) -> bool:
        seen.append(url)
        return opened

    monkeypatch.setattr(wa.webbrowser, "open", fake_open)
    return seen


@pytest.mark.parametrize(
    "payload",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "file:///C:/Windows/win.ini",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "ftp://example.com/book",
        "https://user:pass@example.com/book",
        "https://good.example\\@evil.example/book",
        "//example.com/book",
        "/book/1",
        "",
    ],
)
def test_bridge_open_url_refuses_unsafe_values(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    seen = _capture_browser(monkeypatch)

    result = wa.ApiBridge().open_url(payload)

    assert seen == []
    assert result["ok"] is False
    assert result["url"] == ""
    assert result["error"]


def test_bridge_open_url_opens_a_normalised_https_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _capture_browser(monkeypatch)

    result = wa.ApiBridge().open_url(" HTTPS://Example.COM/book?x=1 ")

    assert seen == ["https://Example.COM/book?x=1"]
    assert result == {
        "ok": True,
        "url": "https://Example.COM/book?x=1",
        "error": None,
    }


def test_bridge_open_url_reports_a_failed_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``webbrowser.open`` returning False must not look like success."""
    _capture_browser(monkeypatch, opened=False)

    result = wa.ApiBridge().open_url("https://example.com/book")

    assert result["ok"] is False
    assert result["url"] == "https://example.com/book"
    assert "browser" in str(result["error"]).lower()


# ---------------------------------------------------------------------------
# static DOM contract
# ---------------------------------------------------------------------------


def _url_safety_block() -> str:
    start = APP_HTML.index("/* url-safety-start */")
    end = APP_HTML.index("/* url-safety-end */") + len("/* url-safety-end */")
    return APP_HTML[start:end]


def test_frontend_has_one_external_url_filter() -> None:
    block = _url_safety_block()
    assert "function safeExternalUrl" in block
    assert "EXTERNAL_URL_MAX_LENGTH = 2048" in block
    # The browser's own parser decides the protocol; only http(s) survives.
    assert 'u.protocol !== "http:"' in block
    assert 'u.protocol !== "https:"' in block
    # Ambiguous authorities are rejected instead of being sent to the OS.
    assert "u.username" in block
    assert "u.password" in block
    assert 'includes("\\\\")' in block
    assert "/[\\u0000-\\u001f\\u007f]/" in block


def test_frontend_routes_every_link_through_the_bridge() -> None:
    block = _url_safety_block()
    assert "function openExternal" in block
    assert "bridge.open_url(url)" in block
    # When the bridge is unavailable, the platform opener is the fallback.
    assert "nativeWindowOpen" in block
    assert 'nativeWindowOpen(url, "_blank", "noopener,noreferrer")' in block
    # No call site may bypass the helper.
    assert 'api("open_url"' not in APP_HTML
    assert APP_HTML.count("openExternal(") >= 4
    assert "openExternal(url.dataset.openUrl)" in APP_HTML
    assert "openExternal(url)" in APP_HTML


def test_frontend_blocks_in_app_navigation_and_popups() -> None:
    block = _url_safety_block()
    assert "window.open = function" in block
    assert 'closest("a[href]")' in block
    assert 'document.addEventListener("click"' in block
    assert 'document.addEventListener("auxclick"' in block
    assert '["dragover", "drop"]' in block
    assert "window.navigation" in block
    assert 'window.navigation.addEventListener("navigate"' in block
    # Local fragment links (the skip link) stay in the document.
    assert 'href.startsWith("#")' in block


def test_frontend_cover_images_use_the_same_filter() -> None:
    covers = APP_HTML[
        APP_HTML.index("function coverBox(") : APP_HTML.index("function sourceCaps(")
    ]
    assert "safeExternalUrl(r.cover_url" in covers
    detail = APP_HTML[
        APP_HTML.index("function renderDetail(") : APP_HTML.index(
            "function endSearchUi("
        )
    ]
    assert "safeExternalUrl(r.cover_url" in detail
    assert "safeExternalUrl(r.book_url" in detail


def test_window_is_created_from_local_html_and_pins_popup_routing() -> None:
    source = Path(wa.__file__).read_text(encoding="utf-8")
    call = source.split("webview.create_window(", 1)[1].split("**kwargs", 1)[0]
    assert "html=APP_HTML" in call
    assert "url=" not in call
    assert "js_api=bridge" in call
    assert 'shell_settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True' in source


# ---------------------------------------------------------------------------
# behavioural check of the extracted JS filter
# ---------------------------------------------------------------------------


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return node


_JS_HARNESS = r"""
const assert = require("assert");
const toasts = [];
function t(key) { return key; }
function toast(msg) { toasts.push(msg); }
const document = { addEventListener() {}, querySelector() { return null; } };
const window = { open() {}, addEventListener() {} };
__LOGIC__
const rejects = [
  "", "   ", "book/1", "/book/1",
  "javascript:alert(1)", "JavaScript:alert(1)", "file:///C:/Windows/win.ini",
  "data:text/html,<script>alert(1)</script>", "vbscript:msgbox(1)",
  "ftp://example.com/book", "mailto:user@example.com",
  "https://user:pass@example.com/book", "https://user@example.com/book",
  "https://good.example\\@evil.example/book", "https://example.com/\\book",
  "https://example.com/a\u0000b", "https://example.com/a\nb",
  "https://example.com/a b", "https://example.com/a\tb",
  "https://example.com/" + "a".repeat(3000),
];
for (const raw of rejects) assert.strictEqual(safeExternalUrl(raw), "", raw);
assert.strictEqual(safeExternalUrl("https://example.com/book/1"),
  "https://example.com/book/1");
assert.strictEqual(safeExternalUrl("http://example.com/a?x=1#f"),
  "http://example.com/a?x=1#f");
assert.strictEqual(safeExternalUrl("//example.com/book"),
  "https://example.com/book");

const opened = [];
window.pywebview = { api: { open_url(url) {
  opened.push(url);
  return { ok: true };
} } };
assert.strictEqual(openExternal("https://example.com/book"), true);
assert.deepStrictEqual(opened, ["https://example.com/book"]);
assert.strictEqual(openExternal("javascript:alert(1)"), false);
assert.ok(toasts.length >= 1);
console.log("ok");
"""


def test_extracted_js_url_filter_behaves(tmp_path: Path) -> None:
    harness = _JS_HARNESS.replace("__LOGIC__", _url_safety_block())
    path = tmp_path / "link_safety.js"
    path.write_text(harness, encoding="utf-8")
    result = subprocess.run(
        [_node(), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
