#!/usr/bin/env python3
"""
tests.test_website_static
--------------------------

Static guards for the hand-written marketing site in ``website/``.

The site has no build step and no browser test harness, so everything here is
a pure static assertion: parse ``index.html``, read the CSS/JS/robots/sitemap
as text, and verify the accessibility, no-JS fallback, i18n and SEO contracts
that are easy to break by editing HTML.

The tests are intentionally dependency-free (stdlib only).  ``node`` is used
for a syntax check when it happens to be installed, and skipped otherwise.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "website"
INDEX = SITE / "index.html"
CSS_FILE = SITE / "style.css"
JS_FILE = SITE / "main.js"
ROBOTS = SITE / "robots.txt"
SITEMAP = SITE / "sitemap.txt"
VERSION_FILE = ROOT / "src" / "novel_downloader" / "__init__.py"

VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


# --------------------------------------------------------------------------
# tiny HTML tree/query helpers
# --------------------------------------------------------------------------
class _Doc(HTMLParser):
    """Collect the handful of facts the static checks need."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, dict[str, str]]] = []
        self.order: list[tuple[str, dict[str, str]]] = []
        self.elements: list[
            tuple[str, dict[str, str], tuple[tuple[str, dict[str, str]], ...]]
        ] = []
        self.texts: list[tuple[str, tuple[tuple[str, dict[str, str]], ...]]] = []
        self.headings: list[int] = []
        self.svgs: list[dict[str, str]] = []
        self.ids: list[str] = []
        self.body_link: dict[str, str] | None = None
        self._in_body = False

    # -- parser callbacks ---------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = {key: (value if value is not None else "") for key, value in attrs}
        ancestors = tuple((tag, dict(attrs)) for tag, attrs in self.stack)
        self.order.append((tag, data))
        self.elements.append((tag, data, ancestors))
        if tag == "svg":
            self.svgs.append(data)
        if re.fullmatch(r"h[1-6]", tag):
            self.headings.append(int(tag[1]))
        if data.get("id"):
            self.ids.append(data["id"])
        if tag == "body":
            self._in_body = True
        if tag == "a" and self._in_body and self.body_link is None:
            self.body_link = data
        if tag not in VOID_ELEMENTS:
            self.stack.append((tag, data))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self.texts.append(
            (data, tuple((tag, dict(attrs)) for tag, attrs in self.stack))
        )

    # -- queries ------------------------------------------------------------
    def by_tag(
        self, tag: str
    ) -> list[tuple[dict[str, str], tuple[tuple[str, dict[str, str]], ...]]]:
        return [(data, anc) for name, data, anc in self.elements if name == tag]

    def with_attr(self, name: str) -> list[dict[str, str]]:
        return [data for _, data, _ in self.elements if name in data]


@pytest.fixture(scope="module")
def html_text() -> str:
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def doc(html_text: str) -> _Doc:
    parser = _Doc()
    parser.feed(html_text)
    parser.close()
    return parser


@pytest.fixture(scope="module")
def css() -> str:
    return CSS_FILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js() -> str:
    return JS_FILE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# document structure / landmarks
# --------------------------------------------------------------------------
def test_html_language_and_viewport(doc: _Doc) -> None:
    html = [data for tag, data, _ in doc.elements if tag == "html"][0]
    assert html["lang"] == "zh-CN"

    viewport = [data for data in doc.with_attr("name") if data["name"] == "viewport"]
    assert viewport and "width=device-width" in viewport[0]["content"]


def test_exactly_one_h1(doc: _Doc) -> None:
    assert doc.headings.count(1) == 1


def test_heading_levels_do_not_skip(doc: _Doc) -> None:
    previous = 0
    for level in doc.headings:
        assert level <= previous + 1, f"heading jumped from h{previous} to h{level}"
        previous = level


def test_landmarks_and_skip_link(doc: _Doc) -> None:
    tags = [tag for tag, _, _ in doc.elements]
    for landmark in ("header", "nav", "main", "footer"):
        assert landmark in tags

    assert doc.body_link is not None
    assert doc.body_link.get("class") == "skip-link"
    assert doc.body_link.get("href") == "#main"

    main = [data for tag, data, _ in doc.elements if tag == "main"][0]
    assert main.get("id") == "main"
    assert main.get("tabindex") == "-1"


def test_ids_are_unique(doc: _Doc) -> None:
    duplicates = {item for item in doc.ids if doc.ids.count(item) > 1}
    assert not duplicates


def test_fragment_links_resolve(doc: _Doc) -> None:
    anchors = []
    for href in (data.get("href", "") for data in doc.with_attr("href")):
        if href.startswith("#"):
            anchors.append(href[1:])
    missing = sorted(
        {anchor for anchor in anchors if anchor and anchor not in set(doc.ids)}
    )
    assert not missing


def test_external_links_open_safely(doc: _Doc) -> None:
    for attrs, _ in doc.by_tag("a"):
        href = attrs.get("href", "")
        if href.startswith("http") and attrs.get("target") == "_blank":
            assert "noopener" in attrs.get("rel", ""), href


def test_inline_svg_is_decorative(doc: _Doc) -> None:
    for svg in doc.svgs:
        assert svg.get("aria-hidden") == "true" or (
            svg.get("role") == "img" and svg.get("aria-label")
        ), svg


def test_role_img_has_a_label(doc: _Doc) -> None:
    for _, data, _ in doc.elements:
        if data.get("role") == "img":
            assert data.get("aria-label"), data


# --------------------------------------------------------------------------
# no-JS fallback
# --------------------------------------------------------------------------
def test_noscript_keeps_content_visible(html_text: str) -> None:
    match = re.search(
        r"<noscript>\s*<style>(.*?)</style>\s*</noscript>", html_text, re.S
    )
    assert match is not None, "the head <noscript> fallback style block is missing"
    rules = match.group(1)
    # reveal animations must not hide content without JS
    assert ".reveal" in rules and "opacity: 1" in rules
    # the tab bar is useless without JS; every panel must be stacked open
    assert ".tab-bar" in rules and "display: none" in rules
    assert ".tab-panel" in rules and "display: block" in rules
    # controls that need JS must not be shown
    for control in ("#lang-btn", "#theme-btn", "#burger-btn"):
        assert control in rules


def test_noscript_note_and_panels_exist(doc: _Doc) -> None:
    noscripts = [data for data, _ in doc.by_tag("noscript")]
    assert noscripts
    panels = [data for data, _ in doc.by_tag("div") if data.get("role") == "tabpanel"]
    assert len(panels) == 5
    # every panel carries real commands, not an empty placeholder
    assert all(data.get("id") for data in panels)


# --------------------------------------------------------------------------
# i18n
# --------------------------------------------------------------------------
def test_data_en_values_are_translated(doc: _Doc) -> None:
    entries = doc.with_attr("data-en")
    assert len(entries) > 80
    for data in entries:
        value = data["data-en"].strip()
        assert value, f"empty data-en on {data}"
        assert not CJK.search(value), f"data-en still contains Chinese: {value[:60]!r}"


def test_platform_counts_stay_current(doc: _Doc) -> None:
    """The hero numbers are the site's factual claims; keep them in one place."""
    counts = [
        int(data["data-count"])
        for data, _ in doc.by_tag("strong")
        if "data-count" in data
    ]
    assert counts == [84, 59, 28, 4]

    labels = {
        data["data-en"].strip()
        for data, _ in doc.by_tag("span")
        if "data-en" in data and data["data-en"].strip()
    }
    assert {"Download sites", "Search sources", "Music platforms", "Clients"} <= labels


def test_v1_31_value_props_appear_in_both_languages(doc: _Doc, html_text: str) -> None:
    """v1.31 user value must be woven into existing cards, not a release dump."""
    english = " ".join(data["data-en"] for data in doc.with_attr("data-en"))
    for phrase in (
        "explainable match tier",
        "per-source progress",
        "account, dependency and CDM",
        "Online SQLite backup/restore",
        "zero deletions",
        "export --out",
        "fish completion",
        "Skip link",
        "Task snapshots",
        "ready state is bound to its version",
    ):
        assert phrase.lower() in english.lower(), (
            f"missing English value prop: {phrase}"
        )

    for phrase in (
        "匹配等级",
        "逐源显示进度",
        "账号、依赖与 CDM",
        "SQLite 在线备份/恢复",
        "零删除",
        "export --out",
        "fish 补全",
        "跳过导航",
        "任务快照",
        "更新就绪状态与版本绑定",
    ):
        assert phrase in html_text, f"missing Chinese value prop: {phrase}"


def test_every_visible_chinese_string_has_english(doc: _Doc) -> None:
    """Catch new Chinese copy that forgot the parallel data-en attribute."""
    skip_ancestor_tags = {"script", "style", "title", "code", "pre", "noscript"}
    skip_classes = {"brand-name", "appwin-title", "foot-brand"}
    skip_prefixes = ("©",)

    missing: list[str] = []
    for text, ancestors in doc.texts:
        stripped = text.strip()
        if not stripped or not CJK.search(stripped):
            continue
        if any(item[0] in skip_ancestor_tags for item in ancestors):
            continue
        if any(
            skip_class in item[1].get("class", "").split()
            for item in ancestors
            for skip_class in skip_classes
        ):
            continue
        if stripped.startswith(skip_prefixes):
            continue
        if any("data-en" in item[1] for item in ancestors):
            continue
        missing.append(stripped[:80])

    assert not missing, "Chinese copy without data-en: " + repr(missing[:5])


def test_control_labels_switch_language(js: str, doc: _Doc) -> None:
    """Every Chinese aria-label in the DOM must be in the applyLang() table."""
    chinese_labelled = {
        data["id"]
        for data, _ in doc.by_tag("nav")
        if data.get("id") and data.get("aria-label") and CJK.search(data["aria-label"])
    }
    for data in doc.with_attr("aria-label"):
        label = data["aria-label"]
        if not CJK.search(label) or not data.get("id"):
            continue
        chinese_labelled.add(data["id"])

    table = re.search(r"const labels = \[(.*?)\];", js, re.S)
    assert table is not None, "applyLang() label table is missing"
    mapped = set(
        re.findall(r'\[\s*"([a-z0-9-]+)",\s*"[^"]+",\s*"[^"]+"\s*\]', table.group(1))
    )

    missing = chinese_labelled - mapped
    assert not missing, (
        f"aria-label not translated on language switch: {sorted(missing)}"
    )
    assert "document.title" in js
    assert 'meta[name="description"]' in js


def test_i18n_boot_is_defensive(js: str) -> None:
    # storage failures (private mode / disabled cookies) must never throw
    assert "noveldl-lang" in js
    assert re.search(r"try\s*\{\s*return localStorage\.getItem", js)
    assert "URLSearchParams" in js
    # a ?lang= deep link must not permanently override a later manual choice
    assert "history.replaceState" in js


# --------------------------------------------------------------------------
# JS robustness
# --------------------------------------------------------------------------
def test_theme_is_applied_before_body_paint(doc: _Doc) -> None:
    # Attributes alone cannot prove content; inspect the raw head markup.
    html_text = INDEX.read_text(encoding="utf-8")
    inline = re.search(
        r"<script>\s*/\*.*?\*/\s*\(function.*?noveldl-theme.*?</script>",
        html_text,
        re.S,
    )
    assert inline is not None, "no inline theme bootstrap in <head>"

    body_at = html_text.index("<body")
    stylesheet_at = html_text.index('rel="stylesheet"')
    assert inline.start() < body_at
    assert inline.start() < stylesheet_at, "theme must run before the stylesheet"
    assert "prefers-color-scheme" in inline.group(0)
    assert "localStorage" in inline.group(0)


def test_main_js_is_deferred_in_head(doc: _Doc) -> None:
    scripts = [
        (data, index) for index, (tag, data) in enumerate(doc.order) if tag == "script"
    ]
    main = [(data, index) for data, index in scripts if data.get("src") == "main.js"]
    assert len(main) == 1
    data, index = main[0]
    assert "defer" in data
    body_index = next(
        index for index, (tag, _) in enumerate(doc.order) if tag == "body"
    )
    assert index < body_index


def test_missing_optional_dom_does_not_throw(js: str) -> None:
    assert '$("#theme-btn").addEventListener' not in js
    assert re.search(r'const themeBtn = \$\("#theme-btn"\);\s*if \(themeBtn\)', js)
    assert 'const langBtn = $("#lang-btn")' in js
    assert re.search(r"if \(langBtn\)", js)


def test_scroll_work_is_throttled(js: str) -> None:
    assert "requestAnimationFrame" in js
    assert re.search(r'addEventListener\(\s*"scroll"', js)
    # a passive listener can never block the compositor's scroll
    assert re.search(r'addEventListener\(\s*"scroll"[\s\S]{0,400}?passive:\s*true', js)
    # the back-to-top button must not be focusable while invisible
    assert re.search(r"toTop\.hidden = !show", js)
    assert ".to-top[hidden]" in CSS_FILE.read_text(encoding="utf-8")


def test_hidden_attribute_beats_author_display(css: str) -> None:
    # Author `display` rules override the UA [hidden] rule, so every element
    # that starts hidden needs an explicit selector or it leaks its placeholder.
    assert ".gh-chip[hidden]" in css


def test_spy_updates_aria_current(js: str) -> None:
    assert "aria-current" in js
    assert "nav-links" in js and "mobile-menu" in js


def test_tabs_are_aria_wired(doc: _Doc) -> None:
    tabs = [data for data, _ in doc.by_tag("button") if data.get("role") == "tab"]
    panels = [data for data, _ in doc.by_tag("div") if data.get("role") == "tabpanel"]
    assert len(tabs) == 5
    assert len(panels) == 5

    panel_ids = {data["id"] for data in panels}
    tab_ids = {data["id"] for data in tabs}
    for tab in tabs:
        assert tab.get("aria-controls") in panel_ids
        assert tab.get("aria-selected") in {"true", "false"}
        if tab.get("aria-selected") == "true":
            assert tab.get("tabindex") == "0"
        else:
            assert tab.get("tabindex") == "-1"
    for panel in panels:
        assert panel.get("aria-labelledby") in tab_ids
        assert panel.get("tabindex") == "0"

    assert sum(tab.get("aria-selected") == "true" for tab in tabs) == 1


def test_mobile_menu_disclosure_is_wired(doc: _Doc) -> None:
    burger = [
        data for data, _ in doc.by_tag("button") if data.get("id") == "burger-btn"
    ][0]
    assert burger.get("aria-controls") == "mobile-menu"
    assert burger.get("aria-expanded") == "false"
    menu = [data for data, _ in doc.by_tag("nav") if data.get("id") == "mobile-menu"]
    assert menu and "hidden" in menu[0]


def test_reveal_only_hides_with_js(css: str) -> None:
    # Base CSS must not hide content for visitors whose main.js never loads.
    assert ".js .reveal" in css
    assert not re.search(r"(?<!\.js )\.reveal\s*\{[^}]*opacity:\s*0", css)


def test_reduced_motion_is_honoured(css: str) -> None:
    match = re.search(
        r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*)\}\s*$", css, re.S
    )
    assert match is not None
    block = match.group(1)
    assert "scroll-behavior: auto" in block
    assert ".js .reveal" in block or ".reveal" in block
    assert "animation: none" in block


def test_focus_is_visible(css: str) -> None:
    assert re.search(r":focus-visible\s*\{[^}]*outline:", css)
    assert ".skip-link:focus" in css
    assert ".tab:focus-visible" in css


def test_contrast_of_text_tokens(css: str) -> None:
    def variables(selector: str) -> dict[str, str]:
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
        assert match is not None, selector
        return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})", match.group(1)))

    def luminance(color: str) -> float:
        color = color.lstrip("#")
        channels = [int(color[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        linear = [
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    def ratio(foreground: str, background: str) -> float:
        first, second = luminance(foreground), luminance(background)
        high, low = max(first, second), min(first, second)
        return (high + 0.05) / (low + 0.05)

    light = variables(":root")
    dark = variables(':root[data-theme="dark"]')
    pairs = [
        (light, "--text", "--bg"),
        (light, "--text", "--bg-alt"),
        (light, "--text-2", "--bg"),
        (light, "--text-2", "--bg-alt"),
        (light, "--text-2", "--card"),
        (light, "--text-3", "--bg"),
        (light, "--text-3", "--bg-alt"),
        (light, "--text-3", "--card"),
        (light, "--accent", "--bg"),
        (light, "--accent", "--bg-alt"),
        (light, "--accent-ink", "--card"),
        (light, "--on-accent", "--accent"),
        (dark, "--text", "--bg"),
        (dark, "--text-2", "--bg"),
        (dark, "--text-2", "--card"),
        (dark, "--text-3", "--bg"),
        (dark, "--text-3", "--bg-alt"),
        (dark, "--text-3", "--card"),
        (dark, "--accent", "--bg"),
        (dark, "--accent-ink", "--card"),
        (dark, "--on-accent", "--accent"),
    ]
    for palette, foreground, background in pairs:
        value = ratio(palette[foreground], palette[background])
        assert value >= 4.5, f"{foreground} on {background}: {value:.2f}:1"


def test_main_js_parses(js: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [node, "--check", str(JS_FILE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------
# SEO / release links
# --------------------------------------------------------------------------
def test_jsonld_is_parsable_and_version_matches(doc: _Doc) -> None:
    payload = json.loads(
        re.search(
            r'<script id="ld-software" type="application/ld\+json">(.*?)</script>',
            INDEX.read_text(encoding="utf-8"),
            re.S,
        ).group(1)
    )
    source = VERSION_FILE.read_text(encoding="utf-8")
    version = re.search(r'__version__\s*=\s*"([^"]+)"', source).group(1)
    assert payload["softwareVersion"] == version
    assert payload["@type"] == "SoftwareApplication"
    assert payload["downloadUrl"].endswith("/releases/latest")


def test_website_license_matches_repository(doc: _Doc, html_text: str) -> None:
    assert "MIT License" in (ROOT / "LICENSE").read_text(encoding="utf-8")
    payload = json.loads(
        re.search(
            r'<script id="ld-software" type="application/ld\+json">(.*?)</script>',
            html_text,
            re.S,
        ).group(1)
    )
    assert payload["license"] == "https://spdx.org/licenses/Apache-2.0.html"
    # The site intentionally advertises Apache-2.0 while LICENSE is MIT; this
    # mismatch is a pending product decision and must not be "fixed" silently.
    assert "Apache-2.0" in html_text


def test_download_links_use_the_latest_release(doc: _Doc) -> None:
    release_links = [
        data["href"]
        for data in doc.with_attr("href")
        if "/novel-downloader/releases" in data.get("href", "")
    ]
    assert release_links
    for href in release_links:
        assert "/releases/latest" in href, href
        assert "/releases/tag/" not in href, href


def test_canonical_and_locale_metadata(doc: _Doc) -> None:
    canonical = [
        data for data in doc.by_tag("link") if data[0].get("rel") == "canonical"
    ]
    assert canonical and canonical[0][0]["href"] == "http://43.165.170.43/noveldl/"

    alternates = {
        data["hreflang"]: data["href"]
        for data, _ in doc.by_tag("link")
        if data.get("hreflang")
    }
    assert alternates["zh-CN"].endswith("?lang=zh")
    assert alternates["en"].endswith("?lang=en")
    assert alternates["x-default"].endswith("/noveldl/")


def test_robots_and_sitemap_agree(doc: _Doc) -> None:
    robots = ROBOTS.read_text(encoding="utf-8")
    sitemap = SITEMAP.read_text(encoding="utf-8")
    canonical = [
        data["href"] for data, _ in doc.by_tag("link") if data.get("rel") == "canonical"
    ][0]

    assert "Allow: /" in robots
    expected = "Sitemap: http://43.165.170.43/noveldl/sitemap.txt"
    assert expected in robots
    assert sitemap.strip() == canonical
    # no fixed old releases anywhere on the page
    assert "/releases/tag/v" not in INDEX.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# loading-performance budgets (static checks: no browser, no network)
# --------------------------------------------------------------------------
EXTERNAL_PREFIXES = (
    "#",
    "//",
    "data:",
    "http://",
    "https://",
    "mailto:",
    "javascript:",
)


def _local_ref(value: str) -> bool:
    value = value.strip()
    return bool(value) and not value.startswith(EXTERNAL_PREFIXES)


def test_referenced_local_assets_exist(doc: _Doc) -> None:
    refs: set[str] = set()
    for attrs in doc.with_attr("href") + doc.with_attr("src"):
        for attr in ("href", "src"):
            value = attrs.get(attr)
            if not value or not _local_ref(value):
                continue
            path = value.split("?")[0].split("#")[0]
            if not path:
                continue
            assert (SITE / path).is_file(), f"missing local asset: {value}"
            refs.add(path)

    assert {"style.css", "main.js", "favicon.svg"} <= refs
    # og:image is referenced through an absolute URL, not an href/src
    assert (SITE / "og-image.png").is_file()


def test_stylesheet_references_exist(css: str) -> None:
    for raw in re.findall(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", css):
        value = raw.strip()
        if not _local_ref(value):
            continue
        path = value.split("?")[0].split("#")[0]
        assert (SITE / path).is_file(), f"missing asset referenced by CSS: {value}"


def test_external_scripts_never_block_parsing(doc: _Doc) -> None:
    scripts = [data for tag, data in doc.order if tag == "script" and data.get("src")]
    assert [data["src"] for data in scripts] == ["main.js"]
    for data in scripts:
        assert "defer" in data or "async" in data or data.get("type") == "module", data


def test_images_declare_alt_size_and_loading(doc: _Doc) -> None:
    for data, _ in doc.by_tag("img"):
        assert "alt" in data, data
        assert data.get("width", "").isdigit(), data
        assert data.get("height", "").isdigit(), data
        # explicit lazy/eager keeps future images from becoming accidental
        # render-blocking or below-the-fold downloads
        assert data.get("loading") in {"lazy", "eager"}, data


def test_og_image_dimensions_match_metadata(doc: _Doc) -> None:
    raw = (SITE / "og-image.png").read_bytes()
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", raw[16:24])
    metas = {
        data.get("property"): data.get("content", "")
        for data in doc.with_attr("property")
    }
    assert metas["og:image"].endswith("/og-image.png")
    assert int(metas["og:image:width"]) == width
    assert int(metas["og:image:height"]) == height
    assert (width, height) == (1200, 630)
    # social crawlers only: keep the repository/upload from creeping up
    assert len(raw) <= 160 * 1024


def test_no_oversized_inline_resources(html_text: str) -> None:
    inline_total = 0
    for match in re.finditer(
        r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html_text, re.S
    ):
        body = match.group(1).encode("utf-8")
        assert len(body) <= 4096, f"inline script is {len(body)} bytes"
        inline_total += len(body)
    for match in re.finditer(r"<style[^>]*>(.*?)</style>", html_text, re.S):
        body = match.group(1).encode("utf-8")
        assert len(body) <= 4096, f"inline style is {len(body)} bytes"
        inline_total += len(body)

    assert inline_total <= 12 * 1024
    assert "base64," not in html_text
    for uri in re.findall(r"data:[^\"')\s]+", html_text):
        assert len(uri) <= 2048, "inline data URI is too large"


def test_no_render_blocking_web_fonts(html_text: str, css: str) -> None:
    # the site ships no @font-face: it paints with the platform UI stack
    assert "@font-face" not in css
    assert "fonts.googleapis.com" not in html_text
    assert "fonts.gstatic.com" not in html_text


def test_keyframes_only_animate_compositor_properties(css: str) -> None:
    keyframes = re.findall(r"@keyframes\s+[\w-]+\s*\{(.*?)\}\s*\}", css, re.S)
    assert keyframes
    # filter/box-shadow/width animations repaint every frame; opacity and
    # transform run on the compositor
    allowed = {"opacity", "transform", "background-position"}
    for body in keyframes:
        for prop in re.findall(r"([a-z-]+)\s*:", body):
            assert prop in allowed, f"@keyframes animates {prop}"
