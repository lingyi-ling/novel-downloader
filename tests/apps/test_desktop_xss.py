#!/usr/bin/env python3
"""
tests.apps.test_desktop_xss
---------------------------

HTML-injection contracts for the single-file desktop front end.

Book titles, authors, site names, search keywords, task errors, music track
names and cover URLs all originate on the network. They are rendered into
``web_assets.APP_HTML`` through template literals, so one unescaped ``${...}``
in an HTML context is an XSS. These tests freeze the sink inventory, require
every interpolation in an HTML template to be escaped or on the reviewed
allowlist, and execute ``esc``/``safeExternalUrl``/``highlight``/``coverBox`` under
Node with hostile inputs.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from novel_downloader.apps.desktop.web_assets import APP_HTML

#: Sinks that write markup. Counts are frozen: a new sink needs review.
SINK_COUNTS = {
    "innerHTML": 34,
    "insertAdjacentHTML": 1,
    "outerHTML": 1,
}

#: Markup-producing expressions that are neither ``esc(...)`` nor ``safeExternalUrl``
#: but are safe by construction. Kept exact so any change needs review.
SAFE_EXPRESSIONS = {
    # highlight() wraps an already-escaped match; ``m`` came out of esc(raw).
    "m": "highlight(): mark body is an escaped regex match",
    # Internal enums and CSS class literals.
    'mode === "dark" ? "sun" : "moon"': "theme mode is normalized to two values",
    'grouped ? "group" : "row"': "boolean scope maps to a static class",
    'i === 0 ? " cap-best" : ""': "index comparison emits static classes",
    'catGender === "male" ? "1" : "2"': "internal gender enum",
    'key === "live" ? "live" : ""': "internal task group key",
    'indet ? "indeterminate" : ""': "boolean maps to a static class",
    'r.done ? (r.ok ? "ok" : "bad") : "wait"': "booleans map to static classes",
    'bad ? "bad" : ""': "boolean maps to a static class",
    'music.selected.has(i) ? "checked" : ""': "Set membership, static token",
    'tr.vip ? `<span class="vip-tag">VIP</span>` : ""': "static badge",
    # Numeric values (indexes, counts, percentages) and their ternaries.
    "i": "numeric result index",
    "i + 1": "numeric display index",
    "j": "numeric lyric index",
    "shown": "numeric result count",
    "total": "numeric result count",
    "rest": "numeric remaining-source count",
    "tasks.length": "array length",
    "pct": "Math.round() percentage",
    "sources.length - 3": "array length arithmetic",
    "music.failedItems.length - MUSIC_FAIL_ROWS": "array length arithmetic",
    'total > 0 ? ` aria-valuenow="${pct}"` : ""': "numeric progress attributes",
    # Locals assembled earlier from escaped/numeric pieces.
    "img": "coverBox()/renderDetail() escaped <img>",
    "meta": "escaped author/word-count/date spans",
    "openBtn": "safeExternalUrl + esc before insertion",
    "letter": "esc() of the title's first character",
    "sub": "escaped author/word-count/date spans",
    "chapterText": "numeric chapter counters",
    "stage": "esc() applied at assignment",
    "speed": "esc() applied at assignment",
    "retry": "escaped retry count inside a static span",
    "err": "esc() applied at assignment",
    "created": "escaped formatted timestamp",
    "exports": "buttons built with esc()",
    "copyErr": "button built with esc()",
    "openDir": "button built with static copy",
    "dot": "static health class",
    "action": "button built with esc()",
    "more": "static overflow count",
    "buttons": "lyric buttons built with esc()",
    'lines.join("")': "followLine() output, which escapes both fields",
    # Trusted renderers checked by their own templates in this same test.
    "coverBox(r)": "trusted renderer",
    "matchBadgeHtml(r)": "trusted renderer",
    "sourceCaps(r)": "trusted renderer",
    "sourceChips(tr)": "trusted renderer",
    "lyricButtons(tr, i)": "trusted renderer",
    "pill(k.status)": "trusted renderer (escapes class and label)",
    'meta.join(" ")': "joining pre-escaped spans",
    'created ? `<span class="hint mono">${created}</span>` : ""': "nested template",
    'lines.length ? `<ul class="fs-list">${lines.join("")}</ul>` : ""': (
        "nested template"
    ),
}

#: Bare identifiers allowed as the value of an ``innerHTML`` assignment.
BARE_RHS = {"html", "more"}

_URL_SCHEMES = re.compile(r"(?:src|href)=\"\s*(?:javascript|data|vbscript):", re.I)


def _script() -> str:
    match = re.search(r"<script>(.*?)</script>", APP_HTML, re.S)
    assert match, "the front end must keep its inline boot script"
    return match.group(1)


def _extract(marker: str) -> str:
    """Return the text between ``/* marker-start */`` and ``/* marker-end */``."""
    script = _script()
    start = script.index(f"/* {marker}-start */")
    end = script.index(f"/* {marker}-end */", start)
    return script[start:end]


def _esc_line() -> str:
    match = re.search(r"^const esc = .*$", _script(), re.M)
    assert match, "esc() must stay a single, greppable definition"
    return match.group(0)


class _TemplateScanner:
    """Collect ``${...}`` expressions from every HTML-looking template.

    A JavaScript lexer is needed because regex literals (``esc``'s character
    class, ``highlight``'s metacharacter escape) contain quotes that would
    otherwise desynchronise a naive string parser.
    """

    def __init__(self, source: str, line_offset: int) -> None:
        self.source = source
        self.line_offset = line_offset
        self.length = len(source)
        self.found: list[tuple[int, str]] = []

    def line_at(self, pos: int) -> int:
        return self.line_offset + self.source.count("\n", 0, pos)

    def skip_string(self, pos: int, quote: str) -> int:
        pos += 1
        while pos < self.length:
            char = self.source[pos]
            if char == "\\":
                pos += 2
                continue
            if char == quote:
                return pos + 1
            pos += 1
        return pos

    @staticmethod
    def _regex_allowed(previous: str) -> bool:
        return previous == "" or previous in "=(:,;!&|?{}[]<>+-*%~^"

    def skip_regex(self, pos: int) -> int:
        pos += 1
        in_class = False
        while pos < self.length:
            char = self.source[pos]
            if char == "\\":
                pos += 2
                continue
            if char == "[":
                in_class = True
            elif char == "]":
                in_class = False
            elif char == "/" and not in_class:
                pos += 1
                return pos
            elif char == "\n":
                return pos
            pos += 1
        return pos

    def template(self, pos: int) -> int:
        """Parse one backtick template; record expressions if it is markup."""
        pos += 1
        text_start = pos
        literals: list[str] = []
        pieces: list[tuple[str, int, str]] = []
        while pos < self.length:
            char = self.source[pos]
            if char == "\\":
                pos += 2
                continue
            if char == "`":
                literals.append(self.source[text_start:pos])
                has_markup = any(
                    "<" in text and (">" in text or "=" in text) for text in literals
                )
                if has_markup:
                    self.found.extend((line, expr) for _, line, expr in pieces)
                return pos + 1
            if char == "$" and pos + 1 < self.length and self.source[pos + 1] == "{":
                literals.append(self.source[text_start:pos])
                expr_start = pos + 2
                depth = 1
                cursor = expr_start
                previous = ""
                while cursor < self.length and depth:
                    current = self.source[cursor]
                    if current in "\"'":
                        cursor = self.skip_string(cursor, current)
                        previous = "x"
                    elif current == "`":
                        cursor = self.template(cursor)
                        previous = "x"
                    elif current == "/" and cursor + 1 < self.length:
                        nxt = self.source[cursor + 1]
                        if nxt == "/":
                            newline = self.source.find("\n", cursor)
                            cursor = self.length if newline < 0 else newline
                        elif nxt == "*":
                            close = self.source.find("*/", cursor + 2)
                            cursor = self.length if close < 0 else close + 2
                        elif self._regex_allowed(previous):
                            cursor = self.skip_regex(cursor)
                            previous = "x"
                        else:
                            cursor += 1
                    elif current == "{":
                        depth += 1
                        cursor += 1
                        previous = "{"
                    elif current == "}":
                        depth -= 1
                        cursor += 1
                        previous = "}"
                    else:
                        if not current.isspace():
                            previous = current
                        cursor += 1
                expression = self.source[expr_start : cursor - 1]
                pieces.append(("expr", self.line_at(pos), expression))
                pos = cursor
                text_start = pos
                continue
            pos += 1
        return pos

    def scan(self) -> list[tuple[int, str]]:
        pos = 0
        previous = ""
        while pos < self.length:
            char = self.source[pos]
            if char in "\"'":
                pos = self.skip_string(pos, char)
                previous = "x"
            elif char == "`":
                pos = self.template(pos)
                previous = "x"
            elif char == "/" and pos + 1 < self.length:
                nxt = self.source[pos + 1]
                if nxt == "/":
                    newline = self.source.find("\n", pos)
                    pos = self.length if newline < 0 else newline
                elif nxt == "*":
                    close = self.source.find("*/", pos + 2)
                    pos = self.length if close < 0 else close + 2
                elif self._regex_allowed(previous):
                    pos = self.skip_regex(pos)
                    previous = "x"
                else:
                    pos += 1
            else:
                if not char.isspace():
                    previous = char
                pos += 1
        return self.found


def _html_interpolations() -> list[tuple[int, str]]:
    script = _script()
    start = APP_HTML.index(script)
    offset = APP_HTML.count("\n", 0, start) + 1
    return _TemplateScanner(script, offset).scan()


def _is_sanitized(expr: str) -> bool:
    return any(
        token in expr
        for token in ("esc(", "safeExternalUrl(", "highlight(", "resultCardLabel(")
    )


# ---------------------------------------------------------------------------
# Sink inventory and dangerous constructs
# ---------------------------------------------------------------------------


def test_html_sink_inventory_is_frozen() -> None:
    """New innerHTML-style writes fail until this inventory is reviewed."""
    script = _script()
    actual = {name: len(re.findall(rf"\b{name}\b", script)) for name in SINK_COUNTS}
    assert actual == SINK_COUNTS


@pytest.mark.parametrize("sink", ["document.write", "srcdoc", "eval(", "new Function"])
def test_alternative_html_injection_sinks_are_absent(sink: str) -> None:
    assert sink not in _script()


def test_markup_sink_assignments_are_clears_or_renderers() -> None:
    """Every non-template RHS is either a clear or a reviewed renderer."""
    script = _script()
    pattern = re.compile(r"(?:innerHTML|outerHTML)\s*=\s*([A-Za-z_$][\w$]*)([^\n;]*)")
    for match in pattern.finditer(script):
        name, tail = match.groups()
        if name in BARE_RHS:
            continue
        # ``rows.map(...)`` / ``results.slice(...).map(...)`` spread a
        # template across lines; the template itself is checked below.
        assert ".map(" in tail or ".slice(" in tail, match.group(0)


def test_insert_adjacent_html_uses_a_static_position_and_renderer() -> None:
    script = _script()
    assert script.count("insertAdjacentHTML(") == 1
    block = script[script.index("insertAdjacentHTML(") :]
    assert block.lstrip().startswith("insertAdjacentHTML(")
    assert '"beforebegin"' in block[:120]
    assert "resultCardHtml(" in block[:400]


def test_url_attributes_never_contain_active_content_schemes() -> None:
    assert not _URL_SCHEMES.search(_script())


def test_follow_status_only_receives_escaped_markup() -> None:
    """followStatus() assigns its argument to innerHTML; callers must escape."""
    calls = re.findall(r"followStatus\((.*?),\s*(?:true|false)\)", _script(), re.S)
    assert calls, "followStatus() must keep its call sites analysable"
    for argument in calls:
        assert argument.strip() == '""' or "esc(" in argument, argument


# ---------------------------------------------------------------------------
# Interpolation contract
# ---------------------------------------------------------------------------


def test_every_html_interpolation_is_escaped_or_reviewed() -> None:
    """All untrusted values in markup go through esc()/safeExternalUrl()/renderers."""
    untrusted = {
        expr.strip() for _, expr in _html_interpolations() if not _is_sanitized(expr)
    }
    unknown = untrusted - set(SAFE_EXPRESSIONS)
    assert not unknown, "unescaped interpolation in HTML: " + repr(sorted(unknown))
    stale = set(SAFE_EXPRESSIONS) - untrusted
    assert not stale, "reviewed-safe expressions disappeared: " + repr(sorted(stale))


def test_escape_html_covers_every_markup_and_quote_character() -> None:
    line = _esc_line()
    assert "replace(/[&<>\"']/g" in line
    for entity in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
        assert entity in line


def test_result_card_accessible_name_starts_from_escaped_title() -> None:
    assert "const title = esc(r.title);" in _script()


# ---------------------------------------------------------------------------
# URL attributes: cover art and "Open in browser"
# ---------------------------------------------------------------------------


def test_cover_images_and_book_urls_use_safe_external_url() -> None:
    script = _script()
    assert 'src="${esc(r.cover_url' not in script
    assert 'data-open-url="${esc(r.book_url' not in script
    detail = script[script.index("function renderDetail(") :]
    assert 'const cover = safeExternalUrl(r.cover_url || "");' in detail
    assert 'src="${esc(cover)}"' in detail
    assert 'const bookUrl = safeExternalUrl(r.book_url || "");' in detail
    assert 'data-open-url="${esc(bookUrl)}"' in detail
    box = _extract("cover-box")
    assert 'const src = safeExternalUrl(r.cover_url || "");' in box
    assert 'src="${esc(src)}"' in box


def test_external_navigation_never_gets_a_raw_url() -> None:
    """Anchors, window.open and dropped links all go through openExternal()."""
    script = _script()
    assert 'api("open_url"' not in script
    assert "openExternal(url); return null;" in script
    assert "const url = safeExternalUrl(raw);" in script
    assert "window.open = function (url)" in script
    assert "linkHref(e.target)" in script
    assert "openExternal(href);" in script
    assert 'nativeWindowOpen(url, "_blank", "noopener,noreferrer")' in script
    assert '["dragover", "drop"].forEach' in script


# ---------------------------------------------------------------------------
# Executed logic under Node
# ---------------------------------------------------------------------------


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return node


_HARNESS = r"""
const assert = require("assert");
__ESC__
__SAFE_URL__
__HIGHLIGHT__
__COVER_BOX__

// esc(): all five characters that can break out of text or an attribute.
assert.strictEqual(esc("&<>"), "&amp;&lt;&gt;");
assert.strictEqual(esc('"'), "&quot;");
assert.strictEqual(esc("'"), "&#39;");
assert.strictEqual(esc(null), "");
assert.strictEqual(esc(undefined), "");

// safeExternalUrl(): only http(s); protocol-relative covers become https.
assert.strictEqual(safeExternalUrl(""), "");
assert.strictEqual(safeExternalUrl("   "), "");
assert.strictEqual(safeExternalUrl("https://example.com/a?b=1&c=2"), "https://example.com/a?b=1&c=2");
assert.strictEqual(safeExternalUrl("http://example.com/a"), "http://example.com/a");
assert.strictEqual(safeExternalUrl("//cdn.example/x.jpg"), "https://cdn.example/x.jpg");
assert.strictEqual(safeExternalUrl("HTTPS://Example.COM/x"), "https://example.com/x");
for (const bad of [
  "javascript:alert(1)",
  " javascript:alert(1) ",
  "JaVaScRiPt:alert(1)",
  "java\nscript:alert(1)",
  "data:text/html,<script>alert(1)</script>",
  "data:image/svg+xml,<svg onload=alert(1)>",
  "vbscript:msgbox(1)",
  "file:///C:/Windows/win.ini",
  "ftp://example.com/x",
  "covers/a.jpg",
  // Whitespace, backslashes and userinfo are not valid in a fetched URL.
  "https://example.com/a b",
  "https://example.com/a\tb",
  "https://example.com/a\\b",
  "https://user:pass@example.com/a",
  "https://example.com/" + "a".repeat(2060),
]) {
  assert.strictEqual(safeExternalUrl(bad), "", "accepted " + JSON.stringify(bad));
}
// A quote in the URL is percent-encoded by the parser, never emitted raw.
assert.ok(!safeExternalUrl('https://ok.example/" onmouseover="alert(1)').includes('"'));

// highlight(): escaping happens before <mark> is inserted.
assert.strictEqual(highlight("<b>", ""), "&lt;b&gt;");
const marked = highlight("<img src=x onerror=alert(1)>", "<img");
assert.ok(marked.startsWith("<mark>&lt;img</mark>"));
assert.ok(!marked.includes("<img"));

// coverBox(): a hostile title or cover URL can never create an element.
const evilCover = coverBox({
  title: "<img src=x onerror=alert(1)>",
  cover_url: "javascript:alert(1)",
});
assert.ok(!evilCover.includes("<img"));
assert.ok(!evilCover.includes("javascript:"));
assert.ok(evilCover.includes("&lt;"));
for (const bad of [
  "data:image/svg+xml,<svg onload=alert(1)>",
  "file:///C:/Windows/win.ini",
  "vbscript:msgbox(1)",
]) {
  assert.ok(!coverBox({ title: "B", cover_url: bad }).includes("<img"), bad);
}
const proto = coverBox({ title: "B", cover_url: "//cdn.example/a.jpg" });
assert.ok(proto.includes('<img src="https://cdn.example/a.jpg"'));
const query = coverBox({ title: "B", cover_url: "https://cdn.example/a.jpg?x=1&y=2" });
assert.ok(query.includes("x=1&amp;y=2"), "URL text must still be HTML-escaped");
console.log("ok");
"""


def test_esc_safe_url_highlight_and_cover_box_under_node(tmp_path: Path) -> None:
    harness = (
        _HARNESS.replace("__ESC__", _esc_line())
        .replace("__SAFE_URL__", _extract("safe-external-url"))
        .replace("__HIGHLIGHT__", _extract("highlight"))
        .replace("__COVER_BOX__", _extract("cover-box"))
    )
    path = tmp_path / "desktop_xss.js"
    path.write_text(harness, encoding="utf-8")
    result = subprocess.run(
        [_node(), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
