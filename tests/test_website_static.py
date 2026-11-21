#!/usr/bin/env python3
"""
tests.test_website_static
--------------------------

Static guards for the hand-written marketing site in ``website/``.

The site has no build step and no browser test harness, so everything here is
a pure static assertion: parse ``index.html``, read the CSS/JS/robots/sitemap
as text, and verify the accessibility, no-JS fallback, i18n and release-link
contracts that are easy to break by editing HTML.  Crawler-facing SEO
(title/description budgets, Open Graph, JSON-LD, hreflang, sitemap) lives in
``tests/test_website_seo.py``.

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
from xml.etree import ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "website"
INDEX = SITE / "index.html"
ERROR_PAGE = SITE / "404.html"
CSS_FILE = SITE / "style.css"
JS_FILE = SITE / "main.js"
ROBOTS = SITE / "robots.txt"
SITEMAP = SITE / "sitemap.txt"
SITEMAP_XML = SITE / "sitemap.xml"
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
# colour helpers (WCAG 2.x relative luminance / contrast)
# --------------------------------------------------------------------------
def _luminance(color: str) -> float:
    color = color.lstrip("#")
    channels = [int(color[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(foreground: str, background: str) -> float:
    first, second = _luminance(foreground), _luminance(background)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


def _blend(foreground: str, background: str, alpha: float) -> str:
    """Composite a hex colour over another at one alpha, as a browser does."""
    foreground = foreground.lstrip("#")
    background = background.lstrip("#")
    channels = [
        round(
            int(foreground[i : i + 2], 16) * alpha
            + int(background[i : i + 2], 16) * (1 - alpha)
        )
        for i in (0, 2, 4)
    ]
    return "#{:02x}{:02x}{:02x}".format(*channels)


def _tokens(css: str, selector: str) -> dict[str, str]:
    """Hex design tokens declared by one selector block."""
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match is not None, selector
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})", match.group(1)))


def _rule(css: str, selector: str) -> str:
    """Body of the first rule with this exact selector."""
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match is not None, selector
    return match.group(1)


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
def error_text() -> str:
    return ERROR_PAGE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def error_doc(error_text: str) -> _Doc:
    parser = _Doc()
    parser.feed(error_text)
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


def test_landmarks_are_unique_and_navs_are_labelled(doc: _Doc, error_doc: _Doc) -> None:
    for parser in (doc, error_doc):
        tags = [tag for tag, _, _ in parser.elements]
        for landmark in ("header", "main", "footer"):
            assert tags.count(landmark) == 1, f"{landmark} must appear once"

        order = [tag for tag, _ in parser.order]
        assert order.index("header") < order.index("main") < order.index("footer")

        for data, _ in parser.by_tag("nav"):
            assert data.get("aria-label"), "every nav landmark needs a name"

        for tag, _, ancestors in parser.elements:
            if tag in {"header", "main", "footer"}:
                assert not any(
                    parent in {"header", "main", "footer"} for parent, _ in ancestors
                ), f"{tag} must be a top-level landmark"


def test_aria_references_and_states_are_valid(doc: _Doc, error_doc: _Doc) -> None:
    focusable = {"a", "button", "input", "select", "textarea", "summary"}
    for parser in (doc, error_doc):
        ids = set(parser.ids)
        for attribute in ("aria-controls", "aria-labelledby"):
            for data in parser.with_attr(attribute):
                targets = [
                    value for value in re.split(r"\s+", data[attribute]) if value
                ]
                assert targets, f"empty {attribute}"
                for target in targets:
                    assert target in ids, f"{attribute}={target} has no such id"

        for data in parser.with_attr("aria-expanded"):
            assert data["aria-expanded"] in {"true", "false"}
        for data in parser.with_attr("aria-pressed"):
            assert data["aria-pressed"] in {"true", "false"}
        for data in parser.with_attr("aria-selected"):
            assert data["aria-selected"] in {"true", "false"}
        for data in parser.with_attr("aria-hidden"):
            assert data["aria-hidden"] in {"true", "false"}

        for tag, data, _ in parser.elements:
            if data.get("aria-hidden") == "true":
                assert tag not in focusable, f"focusable <{tag}> is aria-hidden"
            if "tabindex" in data:
                # positive tabindex reorders the whole document and is a bug
                assert int(data["tabindex"]) in {0, -1}, data


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


def test_inline_svg_is_decorative(doc: _Doc, error_doc: _Doc) -> None:
    for parser in (doc, error_doc):
        for svg in parser.svgs:
            assert svg.get("aria-hidden") == "true" or (
                svg.get("role") == "img" and svg.get("aria-label")
            ), svg


def test_role_img_has_a_label(doc: _Doc, error_doc: _Doc) -> None:
    for parser in (doc, error_doc):
        for _, data, _ in parser.elements:
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


def test_features_section_has_nine_well_formed_cards(doc: _Doc) -> None:
    """The 3x3 value grid is the site's main feature surface: keep all nine,
    each with an icon, a heading and a bilingual paragraph."""

    def in_features(anc: tuple[tuple[str, dict[str, str]], ...]) -> bool:
        return any(
            tag == "section" and attrs.get("id") == "features" for tag, attrs in anc
        )

    def inside_card(anc: tuple[tuple[str, dict[str, str]], ...]) -> bool:
        return any(
            tag == "article" and "card" in attrs.get("class", "").split()
            for tag, attrs in anc
        )

    cards = [
        data
        for data, anc in doc.by_tag("article")
        if "card" in data.get("class", "").split() and in_features(anc)
    ]
    assert len(cards) == 9

    headings = [data for data, anc in doc.by_tag("h3") if in_features(anc)]
    assert len(headings) == 9

    paragraphs = [
        data for data, anc in doc.by_tag("p") if in_features(anc) and inside_card(anc)
    ]
    assert len(paragraphs) == 9
    assert all("data-en" in data for data in paragraphs)

    icons = [
        data
        for data, anc in doc.by_tag("div")
        if "card-ico" in data.get("class", "").split() and in_features(anc)
    ]
    assert len(icons) == 9

    svgs = [data for data, anc in doc.by_tag("svg") if in_features(anc)]
    assert len(svgs) == 9


def _search_demo(html_text: str) -> str:
    """Return the worked example that follows the 3x3 feature grid."""
    match = re.search(
        r"<!-- ={10,} SEARCH DEMO.*?-->(.*?)<!-- /SEARCH DEMO -->",
        html_text,
        re.S,
    )
    assert match is not None, "the aggregate-search worked example is missing"
    return match.group(1)


def _demo_group(demo: str, group_id: str) -> str:
    """Return one labelled column of the worked example (intent/tiers/sort)."""
    match = re.search(
        rf'<div class="sd-group [^"]*" id="{group_id}">(.*?)</div>', demo, re.S
    )
    assert match is not None, f"missing {group_id} group in the worked example"
    return match.group(1)


def test_search_demo_stays_out_of_the_three_by_three_grid(
    doc: _Doc, html_text: str
) -> None:
    """The example must not stretch a grid row again.

    Inside the aggregate-search card it made row 1 roughly twice as tall as
    rows 2-3, leaving a third of a card of empty space beside it.
    """
    demos = [
        (data, ancestors)
        for data, ancestors in doc.by_tag("div")
        if "search-demo" in data.get("class", "").split()
    ]
    assert len(demos) == 1
    data, ancestors = demos[0]
    assert data.get("id") == "search-demo"
    assert any(
        tag == "section" and attrs.get("id") == "features" for tag, attrs in ancestors
    ), "the worked example must stay in the features section"
    assert not any(tag == "article" for tag, _ in ancestors), (
        "keep the worked example out of a feature card"
    )

    features = re.search(
        r'<section class="section" id="features">(.*?)</section>', html_text, re.S
    )
    assert features is not None
    body = features.group(1)
    assert body.index('class="grid grid-3"') < body.index('id="search-demo"'), (
        "the example follows the grid"
    )


def test_aggregate_search_badges_explain_each_result(html_text: str) -> None:
    """Every ranked row says why it landed: a match tier and a relevance score."""
    demo = _search_demo(html_text)

    assert re.search(
        r'data-en="A Record of a Mortal[^"]*"[^>]*>\s*凡人修仙传\s*<', demo
    ), "the example must name a book in both languages"
    assert re.search(r'data-en="Relevance 0\.94"[^>]*>\s*相关度 0\.94\s*<', demo), (
        "the example row must show its relevance score in both languages"
    )

    tiers = _demo_group(demo, "sd-tiers")
    for english, chinese in (
        ("Exact", "精确"),
        ("Prefix", "前缀"),
        ("Contains", "包含"),
        ("Partial", "部分"),
    ):
        assert re.search(rf'data-en="{english}"[^>]*>\s*{chinese}\s*<', tiers), (
            f"missing {english} match-tier badge"
        )


def test_aggregate_search_query_intent_is_search_level(html_text: str) -> None:
    """The example explains the list-level query intent without blurring it
    into the per-row match tier: both labels are static, bilingual copy."""
    demo = _search_demo(html_text)
    intent = _demo_group(demo, "sd-intent")

    assert re.search(r'data-en="Query intent"[^>]*>\s*查询意图\s*<', intent), (
        "the query intent must be labelled in both languages"
    )
    assert "whole search" in intent, (
        "the label must say the intent describes the whole search"
    )
    assert "not a row-level badge" in intent, (
        "the label must distinguish it from the per-row match-tier badge"
    )
    assert "整次查询" in intent, "the Chinese label must carry the same meaning"
    assert 'data-en="Title match"' in intent
    assert 'data-en="Author match"' in intent
    assert "hidden" not in intent, "the intent labels must show without JS"

    # search-level scope and row-level tier must not share one visual class
    assert 'class="sd-scope"' in intent
    assert "sd-tier" not in intent, "query intent must not look like a result badge"

    tiers = _demo_group(demo, "sd-tiers")
    assert "Title match" not in tiers and "Author match" not in tiers, (
        "query intent must not be mixed into the match-tier group"
    )
    assert 'class="sd-tier"' in tiers
    assert "sd-scope" not in tiers


def test_aggregate_search_sort_options_are_static_markup(html_text: str) -> None:
    """Sort choices must be readable with JS off and never look like dead controls."""
    demo = _search_demo(html_text)
    sort = _demo_group(demo, "sd-sort")

    assert re.search(
        r'data-en="Sort · desktop and Android"[^>]*>\s*排序 · 桌面 / Android\s*<',
        sort,
    ), "the sort group must name the clients and both languages"

    for english, chinese in (
        ("Relevance", "相关度"),
        ("Quality", "质量"),
        ("Word count", "字数"),
        ("Updated", "更新时间"),
    ):
        assert re.search(rf'data-en="{english}"[^>]*>\s*{chinese}\s*<', sort), (
            f"missing {english} sort option"
        )

    # plain text, not controls: nothing here needs JS to render, and no widget
    # pretends to be interactive while being static marketing copy
    assert "sd-sort-opt" in sort
    assert "sd-tier" not in sort and "sd-scope" not in sort
    for tag in ("button", "select", "input", "textarea", "script", "noscript"):
        assert not re.search(rf"<{tag}\b", demo), f"sort demo must not use <{tag}>"


def test_search_demo_separates_intent_from_tier_visually(css: str) -> None:
    """Intent, tier and sort choices must read as three different things."""
    scope = re.search(r"\.sd-scope\s*\{([^}]*)\}", css)
    assert scope is not None
    assert "dashed" in scope.group(1), "scope tags are dashed, not badges"
    assert "border-radius: 6px" in scope.group(1)

    tier = re.search(r"\.sd-tier\s*\{([^}]*)\}", css)
    assert tier is not None
    assert "var(--accent-soft)" in tier.group(1), "tier badges carry the accent tint"
    assert "border-radius: 99px" in tier.group(1)

    sort = re.search(r"\.sd-sort-opt\s*\{([^}]*)\}", css)
    assert sort is not None
    assert "border" not in sort.group(1) and "background" not in sort.group(1), (
        "sort options are plain text, not buttons or pills"
    )

    # the example must never be a second row inside the card grid
    assert re.search(r"\.search-demo\s*\{[^}]*margin-top:\s*18px", css)


def test_verify_manifest_links_are_spaced(css: str) -> None:
    """The two manifest links used to touch, separated by one source space."""
    block = re.search(r"\.verify-dl \.verify-links\s*\{([^}]*)\}", css)
    assert block is not None
    assert "display: flex" in block.group(1)
    assert "gap:" in block.group(1)


def test_feature_grid_stretches_cards_and_collapses_to_one_column(css: str) -> None:
    grid = re.search(r"\.grid\s*\{([^}]*)\}", css)
    assert grid is not None
    assert "display: grid" in grid.group(1)
    # rows take the height of their tallest card instead of each card hugging
    # its own copy, which is what keeps a 3-up row visually aligned
    assert "align-items: stretch" in grid.group(1)
    assert re.search(
        r"\.grid-3\s*\{\s*grid-template-columns:\s*repeat\(3,\s*1fr\)", css
    )
    assert re.search(
        r"@media \(max-width: 560px\)[\s\S]*?\.grid-3,\s*\.grid-4\s*\{"
        r"\s*grid-template-columns:\s*1fr",
        css,
    )


def test_verify_code_blocks_scroll_inside_their_card(css: str) -> None:
    block = re.search(r"\.verify-dl pre\s*\{([^}]*)\}", css)
    assert block is not None
    assert "overflow-x: auto" in block.group(1)

    shared = re.search(r"\.tab-panel pre,\s*\.verify-dl pre\s*\{([^}]*)\}", css)
    assert shared is not None
    assert "scrollbar-width: thin" in shared.group(1)
    assert "overscroll-behavior-x: contain" in shared.group(1)
    # the default overlay scrollbar is invisible until touched on phones; the
    # slim custom bar shows that a clipped command continues to the right
    assert re.search(r"\.verify-dl pre::-webkit-scrollbar\s*\{[^}]*height:\s*8px", css)


def test_english_text_expansion_guards(css: str) -> None:
    eyebrow = re.search(r"\.hero-eyebrow\s*\{([^}]*)\}", css)
    assert eyebrow is not None
    # English is longer than the Chinese source and used to leave "2.0" alone
    # after a hyphen break inside the pill
    assert "text-wrap: balance" in eyebrow.group(1)

    mobile = re.search(r"@media \(max-width: 560px\)\s*\{(.*?)\n\}", css, re.S)
    assert mobile is not None
    assert re.search(
        r'html\[lang="en"\]\s+\.hero-stats\s*\{[^}]*display:\s*grid'
        r"[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)",
        mobile.group(1),
    )


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

    table = re.search(r"const (?:labels|LABEL_SPECS) = \[(.*?)\];", js, re.S)
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


def test_language_button_names_the_target_language(doc: _Doc, js: str) -> None:
    """``aria-label="切换语言"`` told screen-reader users nothing about what
    the button would switch to; the label must name the destination."""
    button = [data for data, _ in doc.by_tag("button") if data.get("id") == "lang-btn"][
        0
    ]
    assert button.get("aria-label") == "切换到英文"
    assert re.search(r'\["lang-btn",\s*"切换到英文",\s*"Switch to Chinese"\]', js), (
        "applyLang() must announce the destination language"
    )


def test_mobile_menu_moves_focus_to_the_target(js: str) -> None:
    """Hiding the open menu hides the link that was just activated; without a
    move the keyboard user's focus falls back to the document start."""
    assert "focusMenuTarget" in js
    assert re.search(r"focus\(\{ preventScroll: true \}\)", js)
    assert re.search(r'setAttribute\("tabindex", "-1"\)', js)
    assert re.search(r"if \(!focusMenuTarget\(link\)\) burger\.focus\(\)", js)


def test_code_blocks_are_keyboard_scrollable(doc: _Doc, css: str) -> None:
    """A horizontally scrollable <pre> is not reachable by keyboard in every
    browser unless it is focusable."""
    pres = [data for data, _ in doc.by_tag("pre")]
    assert len(pres) >= 7
    for data in pres:
        assert data.get("tabindex") == "0", data
    for selector in (".tab-panel pre", ".verify-dl pre"):
        assert "overflow-x: auto" in _rule(css, selector)


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
    light = _tokens(css, ":root")
    dark = _tokens(css, ':root[data-theme="dark"]')
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
        value = _contrast(palette[foreground], palette[background])
        assert value >= 4.5, f"{foreground} on {background}: {value:.2f}:1"


def test_contrast_extends_to_every_text_surface(css: str) -> None:
    """Accent text, button labels and code panels must pass AA too.

    The base test covers the main text tokens; these are the pairings the
    components actually use (accent on the alternate section background,
    accent-ink headings on the page background, buttons on hover, code text).
    """
    light = _tokens(css, ":root")
    dark = _tokens(css, ':root[data-theme="dark"]')
    pairs = [
        (light, "--accent", "--bg-alt"),
        (light, "--accent", "--card"),
        (light, "--accent-ink", "--bg"),
        (light, "--accent-ink", "--bg-alt"),
        (light, "--on-accent", "--accent-hover"),
        (light, "--code-text", "--code-bg"),
        (dark, "--accent", "--bg-alt"),
        (dark, "--accent", "--card"),
        (dark, "--accent-ink", "--bg"),
        (dark, "--accent-ink", "--bg-alt"),
        (dark, "--on-accent", "--accent-hover"),
        (dark, "--code-text", "--code-bg"),
    ]
    for palette, foreground, background in pairs:
        value = _contrast(palette[foreground], palette[background])
        assert value >= 4.5, f"{foreground} on {background}: {value:.2f}:1"


def test_code_panel_literal_colours_meet_aa(css: str) -> None:
    """The code panels use literal greys instead of tokens; keep them AA."""
    light = _tokens(css, ":root")
    dark = _tokens(css, ':root[data-theme="dark"]')
    for selector in (
        ".tab",
        ".tab:hover",
        ".tab-panel .c",
        ".verify-dl .c",
        ".noscript-note",
    ):
        match = re.search(r"color:\s*(#[0-9a-fA-F]{6})", _rule(css, selector))
        assert match is not None, selector
        colour = match.group(1)
        for name, palette in (("light", light), ("dark", dark)):
            value = _contrast(colour, palette["--code-bg"])
            assert value >= 4.5, f"{selector} {colour} on {name} code-bg: {value:.2f}:1"


def test_accent_tinted_badges_keep_aa_contrast(css: str) -> None:
    """Eyebrow, client tags, version pills and tier badges are accent-soft
    tints, so contrast must be computed against the composited colour."""
    for selector in (
        ":root",
        ':root[data-theme="dark"]',
    ):
        palette = _tokens(css, selector)
        block = _rule(css, selector)
        rgba = re.search(
            r"--accent-soft:\s*rgba\(\s*(\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\s*\)",
            block,
        )
        assert rgba is not None, selector
        tint = "#{:02x}{:02x}{:02x}".format(
            *(int(rgba.group(index)) for index in (1, 2, 3))
        )
        alpha = float(rgba.group(4))
        for surface in ("--bg", "--bg-alt", "--card"):
            background = _blend(tint, palette[surface], alpha)
            value = _contrast(palette["--accent-ink"], background)
            assert value >= 4.5, (
                f"accent-ink on accent-soft/{surface} ({selector}): {value:.2f}:1"
            )


def test_404_action_labels_are_not_dimmed(error_text: str, css: str) -> None:
    """Dimming the English half of the 404 buttons to .76 dropped the primary
    label to 3.61:1. The labels must keep the button's full foreground."""
    block = _rule(error_text, ".error-actions .btn span")
    for value in re.findall(r"opacity:\s*([0-9.]+)", block):
        assert float(value) >= 1.0, "dimmed 404 button labels fail AA"
    light = _tokens(css, ":root")
    assert _contrast(light["--on-accent"], light["--accent"]) >= 4.5
    assert _contrast(light["--text"], light["--card"]) >= 4.5


def test_focus_visible_ring_covers_interactive_surfaces(css: str) -> None:
    """Every keyboard-reachable control must keep a visible ring.

    The universal :focus-visible rule covers all of them; the explicit list is
    the audited contract, so deleting one selector from a refactor fails here.
    """
    base = re.search(r"(?m)^:focus-visible\s*\{([^}]*)\}", css)
    assert base is not None, "no universal :focus-visible rule"
    assert "outline: 2px solid var(--accent)" in base.group(1)
    assert "outline-offset: 2px" in base.group(1)

    for selector in (
        ".nav-links a:focus-visible",
        ".mobile-menu a:focus-visible",
        ".brand:focus-visible",
        ".lang-btn:focus-visible",
        ".icon-btn:focus-visible",
        ".btn:focus-visible",
        ".download-card:focus-visible",
        ".dl-link:focus-visible",
        ".faq summary:focus-visible",
        ".foot-links a:focus-visible",
        ".to-top:focus-visible",
        ".error-nav a:focus-visible",
        ".tab:focus-visible",
        ".btn-primary:focus-visible",
        ".skip-link:focus",
    ):
        assert selector in css, f"missing focus style: {selector}"

    # The only element allowed to suppress its ring is the skip-link target,
    # which is a scroll target rather than a control.
    no_outline = re.findall(r"([^{}]+)\{[^{}]*outline:\s*none[^{}]*\}", css)
    assert no_outline, "expected the documented #main exception"
    assert all("#main" in selector for selector in no_outline), no_outline

    # Non-text contrast: the ring itself must be visible against every surface
    # it can be drawn on (WCAG 1.4.11 needs 3:1).
    for selector in (":root", ':root[data-theme="dark"]'):
        palette = _tokens(css, selector)
        for foreground, backgrounds in (
            ("--accent", ("--bg", "--bg-alt", "--card")),
            ("--text", ("--bg", "--card")),
            ("--code-text", ("--code-bg",)),
        ):
            for background in backgrounds:
                value = _contrast(palette[foreground], palette[background])
                assert value >= 3.0, (
                    f"focus ring {foreground} on {background}: {value:.2f}:1"
                )


def test_skip_link_is_height_independent_until_focused(css: str) -> None:
    """A fixed negative `top` stops hiding the link once text zoom makes it
    taller; the hidden state must be a percentage transform."""
    block = _rule(css, ".skip-link")
    assert "position: fixed" in block
    assert "translateY(calc(-100% - 8px))" in block
    assert "display: none" not in block
    assert not re.search(r"top:\s*-\d", block), "fixed negative top can peek at zoom"
    focus = re.findall(r"\.skip-link:focus[^{,]*\{([^}]*)\}", css)
    assert any("transform: none" in body for body in focus), (
        "focusing the skip link must reveal it"
    )


def test_skip_link_is_the_first_body_element(doc: _Doc, error_doc: _Doc) -> None:
    for parser in (doc, error_doc):
        body_at = next(
            index for index, (tag, _) in enumerate(parser.order) if tag == "body"
        )
        tag, data = parser.order[body_at + 1]
        assert tag == "a", "the skip link must be the first element in <body>"
        assert "skip-link" in data.get("class", "").split()
        assert data.get("href") == "#main"


def test_reduced_motion_covers_leftover_transforms(css: str, js: str) -> None:
    match = re.search(
        r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*)\}\s*$", css, re.S
    )
    assert match is not None
    block = match.group(1)

    transition_rules = re.findall(r"([^{}]+)\{[^}]*transition:\s*none[^}]*\}", block)
    assert any(
        ".icon-btn" in rule and ".dl-link" in rule and ".skip-link" in rule
        for rule in transition_rules
    ), "icon, download-link and skip-link transitions must stop"
    transform_rules = re.findall(r"([^{}]+)\{[^}]*transform:\s*none[^}]*\}", block)
    assert any(".download-card:hover .dl-link" in rule for rule in transform_rules), (
        "the download-link nudge must be disabled"
    )
    assert any(
        ".btn:hover" in rule and ".card:hover" in rule for rule in transform_rules
    ), "hover lifts must be disabled"

    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in js
    assert re.search(r"if \(reduceMotion\.matches\)", js)
    assert re.search(
        r"behavior:\s*reduceMotion\.matches\s*\?\s*\"auto\"\s*:\s*\"smooth\"", js
    )


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


def test_footer_release_notes_link_is_static_and_safe(doc: _Doc) -> None:
    """The changelog entry must be a plain footer link: usable with JS off,
    opened safely, bilingual, and pointed at a file that actually exists."""
    url = "https://github.com/lingyi-ling/novel-downloader/blob/main/CHANGELOG.md"
    matches = [
        (data, ancestors)
        for data, ancestors in doc.by_tag("a")
        if data.get("href") == url
    ]
    assert len(matches) == 1, "exactly one footer link must point at CHANGELOG.md"
    data, ancestors = matches[0]

    # it belongs to the footer navigation, next to GitHub / Docs / Issues
    assert any(
        tag == "nav" and attrs.get("id") == "foot-links" for tag, attrs in ancestors
    ), "the release-notes link must live in the footer navigation"
    # a plain anchor in static markup: no reveal gating, so it shows with JS off
    assert not any(
        "reveal" in attrs.get("class", "").split() for _, attrs in ancestors
    ), "the release-notes link must not depend on scroll-reveal JS"
    assert "hidden" not in data

    assert data.get("target") == "_blank"
    assert "noopener" in data.get("rel", "").split(), data.get("rel")
    assert data.get("data-en", "").strip() == "Release notes"

    labels = [
        text.strip()
        for text, text_ancestors in doc.texts
        if text.strip()
        and any(
            tag == "a" and attrs.get("href") == url for tag, attrs in text_ancestors
        )
    ]
    assert "更新日志" in labels

    # a changelog link that 404s is worse than no link at all
    assert (ROOT / "CHANGELOG.md").is_file()


def _verify_dl_block(html_text: str) -> str:
    match = re.search(
        r'<details[^>]*\bid="verify-dl"[^>]*>(.*?)</details>', html_text, re.S
    )
    assert match is not None, "the download verification disclosure is missing"
    return match.group(1)


def test_download_verification_is_native_and_no_js(doc: _Doc) -> None:
    entries = [
        (data, ancestors)
        for data, ancestors in doc.by_tag("details")
        if data.get("id") == "verify-dl"
    ]
    assert len(entries) == 1
    data, ancestors = entries[0]
    # <details open> keeps the guidance readable with JavaScript disabled
    assert "open" in data
    assert "hidden" not in data
    assert any(
        tag == "section" and attrs.get("id") == "download" for tag, attrs in ancestors
    ), "the verification card must live in the download section"

    summaries = [
        attrs
        for attrs, anc in doc.by_tag("summary")
        if anc and anc[-1][0] == "details" and anc[-1][1].get("id") == "verify-dl"
    ]
    assert len(summaries) == 1


def test_download_verification_commands_and_signing_note(html_text: str) -> None:
    block = _verify_dl_block(html_text)
    # both platform recipes
    assert "Get-FileHash" in block
    assert "-Algorithm SHA256" in block
    assert "shasum -a 256" in block
    assert "sha256sum" in block
    # the published checksum list and the machine-readable manifest
    base = "https://github.com/lingyi-ling/novel-downloader/releases/latest/download/"
    assert base + "SHA256SUMS.txt" in block
    assert base + "latest.json" in block

    # manifest signing is not enabled; say so in both languages and never
    # imply the checksum proves who published a file
    lowered = block.lower()
    assert "签名" in block and "尚未启用" in block
    assert "not a signature" in lowered
    assert "not enabled" in lowered
    assert "is signed" not in lowered
    assert "已签名" not in block


def test_download_verification_is_fully_bilingual(html_text: str) -> None:
    parsed = _Doc()
    parsed.feed(_verify_dl_block(html_text))
    parsed.close()

    entries = parsed.with_attr("data-en")
    assert len(entries) >= 6
    for data in entries:
        value = data["data-en"].strip()
        assert value
        assert not CJK.search(value), f"data-en still contains Chinese: {value[:60]!r}"

    english = " ".join(data["data-en"] for data in entries)
    assert "Verify your download" in english
    assert "Windows (PowerShell)" in english
    assert "not a signature" in english


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
    # The page is served in zh-CN by default, so its language alternate is the
    # canonical URL itself; only the en variant needs a query deep link.
    assert alternates["zh-CN"] == "http://43.165.170.43/noveldl/"
    assert alternates["en"].endswith("?lang=en")
    assert alternates["x-default"].endswith("/noveldl/")


def test_robots_and_sitemap_agree(doc: _Doc) -> None:
    robots = ROBOTS.read_text(encoding="utf-8")
    sitemap = SITEMAP.read_text(encoding="utf-8")
    canonical = [
        data["href"] for data, _ in doc.by_tag("link") if data.get("rel") == "canonical"
    ][0]

    assert "Allow: /" in robots
    assert "Sitemap: http://43.165.170.43/noveldl/sitemap.xml" in robots
    assert "Sitemap: http://43.165.170.43/noveldl/sitemap.txt" in robots
    assert sitemap.strip() == canonical

    root = ET.parse(SITEMAP_XML).getroot()
    locs = [
        (loc.text or "").strip()
        for loc in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
    ]
    assert locs == [canonical]
    # no fixed old releases anywhere on the page
    assert "/releases/tag/v" not in INDEX.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# static 404 page
# --------------------------------------------------------------------------
def test_404_is_a_noindex_landmark_document(error_text: str, error_doc: _Doc) -> None:
    assert ERROR_PAGE.is_file()

    html = [data for tag, data, _ in error_doc.elements if tag == "html"][0]
    assert html["lang"] == "zh-CN"
    assert error_doc.headings.count(1) == 1

    tags = [tag for tag, _, _ in error_doc.elements]
    for landmark in ("header", "nav", "main", "footer"):
        assert landmark in tags

    assert error_doc.body_link is not None
    assert error_doc.body_link.get("class") == "skip-link"
    assert error_doc.body_link.get("href") == "#main"

    main = [data for tag, data, _ in error_doc.elements if tag == "main"][0]
    assert main.get("id") == "main"
    assert main.get("tabindex") == "-1"

    robots = [data for data in error_doc.with_attr("name") if data["name"] == "robots"]
    assert robots and "noindex" in robots[0]["content"].lower()

    title = re.search(r"<title>(.*?)</title>", error_text, re.S)
    assert title is not None and "404" in title.group(1)


def test_404_copy_is_bilingual_without_javascript(
    error_text: str, error_doc: _Doc
) -> None:
    english = " ".join(
        text.strip()
        for text, ancestors in error_doc.texts
        if text.strip() and any(attrs.get("lang") == "en" for _, attrs in ancestors)
    )
    assert "Page not found" in english
    assert "This address does not exist" in english
    for label in ("Home", "Download", "Docs", "Features", "Quick start", "FAQ"):
        assert label in english, f"missing English label: {label}"

    # The Chinese half is literal markup too, not something JS injects.
    for phrase in ("页面走丢了", "这个地址不存在", "返回首页", "更新日志"):
        assert phrase in error_text, f"missing Chinese copy: {phrase}"


def test_404_returns_to_site_navigation(error_doc: _Doc, doc: _Doc) -> None:
    hrefs = [data.get("href", "") for data in error_doc.with_attr("href")]

    assert "/noveldl/" in hrefs
    assert "/noveldl/#download" in hrefs
    docs = "https://github.com/lingyi-ling/novel-downloader/tree/main/docs"
    assert docs in hrefs

    # The in-page shortcuts must land on sections that actually exist.
    index_ids = set(doc.ids)
    fragments = {
        href.split("#", 1)[1]
        for href in hrefs
        if href.startswith("/noveldl/#") and "#" in href
    }
    assert {"features", "clients", "start", "download", "faq"} <= fragments
    assert fragments <= index_ids, f"dangling 404 targets: {fragments - index_ids}"

    for data, _ in error_doc.by_tag("a"):
        href = data.get("href", "")
        if href.startswith("http") and data.get("target") == "_blank":
            assert "noopener" in data.get("rel", "").split(), href


def test_404_header_links_stay_visible_without_js(
    error_text: str, error_doc: _Doc
) -> None:
    """style.css hides the header links below 760px for the burger menu; this
    page has no JavaScript, so its inline layout must keep them visible."""
    assert re.search(
        r"@media \(max-width: 760px\)[\s\S]*?\.nav-links\s*\{[^}]*display:\s*flex",
        error_text,
    ), "the 404 header nav must not vanish on phones"
    assert re.search(
        r"\.nav-links a:not\(:last-child\)\s*\{\s*display:\s*inline", error_text
    ), "the 560px rule that keeps only the last link must be overridden"
    assert re.search(
        r"@media \(max-width: 560px\)\s*\{[\s\S]*?"
        r'\.nav-links a span\[lang="en"\]\s*\{\s*display:\s*none',
        error_text,
    ), "phones drop the English halves so the links fit"
    # The override only wins because the inline block follows the stylesheet.
    assert error_text.index('rel="stylesheet"') < error_text.index(
        "@media (max-width: 760px)"
    )

    header_links = [
        data
        for data, ancestors in error_doc.by_tag("a")
        if any(tag == "header" for tag, _ in ancestors)
    ]
    hrefs = {data.get("href", "") for data in header_links}
    assert "/noveldl/" in hrefs, "the brand/home link must stay in the header"
    assert "/noveldl/#download" in hrefs


def test_404_skip_link_marks_the_english_run(error_text: str) -> None:
    assert re.search(
        r'<a class="skip-link" href="#main">[^<]*'
        r'<span lang="en">Skip to main content</span>',
        error_text,
    ), "the English half of the skip link needs a lang attribute"


def test_404_needs_no_javascript(error_text: str, error_doc: _Doc) -> None:
    assert not re.search(r"<script\b[^>]*\bsrc=", error_text)
    # Nothing is hidden until a script runs, and no control requires JS.
    assert ".reveal" not in error_text
    assert not [data for _, data, _ in error_doc.elements if "hidden" in data]
    for phrase in ("页面走丢了", "Page not found", "返回首页", "Download", "Docs"):
        assert phrase in error_text

    # The only inline script is the pre-paint theme choice; it cannot hide
    # content (and the page is complete when it never runs).
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", error_text, re.S)
    assert len(scripts) == 1
    assert "noveldl-theme" in scripts[0]
    assert "display" not in scripts[0] and "hidden" not in scripts[0]


def test_404_local_assets_resolve_under_the_site_prefix(error_doc: _Doc) -> None:
    """The page is served at arbitrary depths, so its URLs are absolute."""
    prefix = "/noveldl/"
    refs: set[str] = set()
    for attrs in error_doc.with_attr("href") + error_doc.with_attr("src"):
        for attr in ("href", "src"):
            value = attrs.get(attr, "")
            if not value.startswith(prefix):
                continue
            refs.add(value)
            relative = value[len(prefix) :].split("?")[0].split("#")[0]
            if relative:
                assert (SITE / relative).is_file(), f"missing local asset: {value}"

    assert prefix + "style.css" in refs
    assert prefix + "favicon.svg" in refs
    # The 404 page reuses the main site's shell instead of a second design.
    assert '<header class="nav">' in ERROR_PAGE.read_text(encoding="utf-8")
    assert '<footer class="footer">' in ERROR_PAGE.read_text(encoding="utf-8")


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


def test_images_declare_alt_size_and_loading(doc: _Doc, error_doc: _Doc) -> None:
    for parser in (doc, error_doc):
        for data, _ in parser.by_tag("img"):
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


# --------------------------------------------------------------------------
# resource-loading policy
#
# The site has no build step and no CDN: every subresource below is a file in
# website/.  These tests pin the loading decisions that keep first paint cheap
# so a later edit cannot quietly add a font, a third-party script or a
# render-blocking request.
# --------------------------------------------------------------------------
SPECULATIVE_RELS = {"preconnect", "dns-prefetch", "prefetch", "prerender", "preload"}


def _rel_set(data: dict[str, str]) -> set[str]:
    return set(data.get("rel", "").lower().split())


def _site_path(value: str) -> Path:
    path = value.split("?")[0].split("#")[0]
    return SITE / path.removeprefix("/noveldl/").lstrip("/")


def test_one_local_stylesheet_per_page(doc: _Doc, error_doc: _Doc) -> None:
    # One same-origin stylesheet is the only render-blocking subresource.
    for parser in (doc, error_doc):
        sheets = [
            data for data, _ in parser.by_tag("link") if "stylesheet" in _rel_set(data)
        ]
        assert len(sheets) == 1, sheets
        assert _local_ref(sheets[0].get("href", "")), sheets[0]
        assert _site_path(sheets[0]["href"]).is_file()


def test_no_third_party_scripts_fonts_or_imports(
    doc: _Doc, error_doc: _Doc, css: str, js: str
) -> None:
    for parser in (doc, error_doc):
        for data in parser.with_attr("src"):
            assert _local_ref(data["src"]), f"external subresource: {data['src']}"
    # the 404 page is deliberately JavaScript-free
    assert not [data for data, _ in error_doc.by_tag("script") if data.get("src")]
    # no web fonts and no injected runtime downloads
    assert "@font-face" not in css
    assert "@import" not in css
    assert not re.search(r"url\(", css), "CSS must not fetch fonts or images"
    assert not re.search(r"document\.createElement\(\s*[\"']script", js)
    assert "new Worker(" not in js
    assert not re.search(r"(?<![\w.])import\s*\(", js)


def test_speculative_hints_are_absent_or_local(doc: _Doc, error_doc: _Doc) -> None:
    # No preconnect/dns-prefetch/prefetch/prerender: nothing on the critical
    # path is third-party, and the GitHub calls are idle-scheduled instead.
    # If a real local preload is ever added, it must point at a shipped file.
    for parser in (doc, error_doc):
        for data, _ in parser.by_tag("link"):
            rels = _rel_set(data)
            assert not rels & SPECULATIVE_RELS - {"preload"}, data
            if "preload" in rels:
                href = data.get("href", "")
                assert _local_ref(href), data
                assert _site_path(href).is_file(), data
                assert "og-image.png" not in href, data


def test_og_image_never_competes_with_first_paint(
    doc: _Doc, error_doc: _Doc, css: str
) -> None:
    # The social card is crawler-only: it must never be an <img>, a
    # stylesheet url() or a preload target of the rendered page.
    for parser in (doc, error_doc):
        for tag, data in parser.order:
            for attr in ("src", "srcset", "href"):
                assert "og-image.png" not in data.get(attr, ""), (tag, data)
    assert "og-image.png" not in css
    owners = {
        data.get("property") or data.get("name")
        for tag, data in doc.order
        if tag == "meta" and "og-image.png" in data.get("content", "")
    }
    assert owners == {"og:image", "twitter:image"}


def test_no_media_elements_compete_with_first_paint(doc: _Doc, error_doc: _Doc) -> None:
    # The page is text + CSS only, so there is nothing to size or lazy-load.
    # Any future <img>/<video>/<iframe> must carry width/height (or an
    # aspect-ratio) before it is allowed in; test_images_declare_alt_size_and_loading
    # already enforces that contract for <img>.
    for parser in (doc, error_doc):
        for tag in ("img", "picture", "video", "iframe", "embed", "object", "canvas"):
            assert not parser.by_tag(tag), f"add intrinsic dimensions for <{tag}>"


def test_render_blocking_requests_stay_deferred(doc: _Doc, error_doc: _Doc) -> None:
    scripts = [data for data, _ in doc.by_tag("script") if data.get("src")]
    assert [data["src"] for data in scripts] == ["main.js"]
    assert "defer" in scripts[0]
    # defer + async would execute before the document is parsed again
    assert "async" not in scripts[0]
    # one blocking stylesheet, nothing else in the head pulls bytes
    for parser in (doc, error_doc):
        assert (
            sum("stylesheet" in _rel_set(data) for data, _ in parser.by_tag("link"))
            == 1
        )


# --------------------------------------------------------------------------
# JavaScript execution budget
# --------------------------------------------------------------------------
def test_decorative_work_waits_for_idle(js: str) -> None:
    assert re.search(r"function whenIdle\(fn\)", js)
    assert re.search(r"requestIdleCallback\(fn, \{ timeout: 3000 \}\)", js)
    assert re.search(r"setTimeout\(fn, 1200\)", js)
    # scrollspy + stars + release version
    assert js.count("whenIdle(() => {") >= 3
    # the scrollspy lookups and observers are inside an idle callback
    scrollspy = re.search(r"scrollspy \*/", js)
    menu = re.search(r"/\* -+ mobile menu", js)
    assert scrollspy is not None and menu is not None
    assert "whenIdle(() => {" in js[scrollspy.end() : menu.start()]
    # no request starts at parse time: everything runs from an idle callback
    assert not re.search(r"^(?:fetch|ghJson|latestVersion)\(", js, re.M)


def test_element_queries_are_resolved_once(js: str) -> None:
    ids = re.findall(r'\$\("#([\w-]+)"\)', js)
    assert len(ids) == len(set(ids)), "the same #id is queried twice"
    # language switches reuse their label elements instead of re-querying
    assert re.search(r"if \(!labelEls\)", js)
    assert "LABEL_SPECS.map" in js


def test_js_does_not_force_synchronous_layout(js: str) -> None:
    # Reads that force layout inside the scroll/reveal path would stall frames;
    # the one scroll read (scrollY) happens inside requestAnimationFrame.
    forced = (
        "getBoundingClientRect",
        "getComputedStyle",
        "offsetWidth",
        "offsetHeight",
        "offsetTop",
        "offsetLeft",
        "scrollHeight",
        "scrollWidth",
        "clientWidth",
        "clientHeight",
        "document.write",
    )
    for api in forced:
        assert api not in js, f"{api} forces a synchronous layout"
    assert re.search(r"requestAnimationFrame\(syncToTop\)", js)


# --------------------------------------------------------------------------
# layout-shift and animation guards
# --------------------------------------------------------------------------
def test_demo_window_size_is_font_independent(css: str) -> None:
    # The showcase mock is built from fixed-size bars, not text: a fallback
    # font swap cannot change its height.  min-height keeps the first paint
    # at 300px while the cards are still streaming in.
    assert "max-width: 860px" in _rule(css, ".appwin")
    assert "overflow: hidden" in _rule(css, ".appwin")
    assert "min-height: 300px" in _rule(css, ".appwin-body")
    assert "grid-template-columns: 150px 1fr" in _rule(css, ".appwin-body")
    for selector, height in (
        (".aw-title, .aw-sub", "height: 10px"),
        (".aw-meta", "height: 8px"),
        (".aw-h1", "height: 16px"),
        (".aw-btn", "height: 24px"),
    ):
        assert height in _rule(css, selector), selector
    # count-up digits keep the same advance width while animating
    assert "font-variant-numeric: tabular-nums" in _rule(css, ".stat strong")


def test_animations_touch_only_compositor_properties(css: str) -> None:
    reveal = _rule(css, ".js .reveal")
    assert "opacity" in reveal and "transform" in reveal
    assert not re.search(
        r"(width|height|top|left|right|bottom|margin|padding)", reveal
    ), "the reveal must not animate layout properties"
    # the only keyframe animation is the opacity pulse
    assert len(re.findall(r"@keyframes", css)) == 1
    assert set(re.findall(r"animation:\s*([\w-]+)", css)) <= {"none", "awPulse"}


def test_reduced_motion_still_wins_the_cascade(css: str) -> None:
    # The reduced-motion block is last so it overrides every animation above.
    assert css.rfind("@media (prefers-reduced-motion: reduce)") > css.rfind(
        "@keyframes"
    )
    block = css[css.rfind("@media (prefers-reduced-motion: reduce)") :]
    assert "animation: none" in block
    assert "transition: none" in block
    assert "scroll-behavior: auto" in block


def test_css_has_no_unused_class_rules(
    css: str, html_text: str, error_text: str
) -> None:
    # Comments are stripped first: they contain prose such as "WCAG 2.x".
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    classes = set(re.findall(r"\.([A-Za-z_][\w-]*)", stripped))
    # added by main.js after parse, so they never appear in the markup
    dynamic = {"js", "in", "open", "show"}
    pages = html_text + error_text
    unused = sorted(
        name
        for name in classes - dynamic
        if not re.search(rf'class="[^"]*\b{re.escape(name)}\b', pages)
    )
    assert not unused, f"CSS rules match no element: {unused}"
