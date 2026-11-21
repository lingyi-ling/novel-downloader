#!/usr/bin/env python3
"""
tests.test_website_seo
----------------------

Crawler-facing guards for the hand-written site in ``website/``.

``tests/test_website_static.py`` owns accessibility, the no-JS fallback, i18n
coverage and the loading budget.  This module owns the SEO surface instead:
title/description budgets in every language the page can render, Open Graph
and Twitter cards, JSON-LD, hreflang, robots.txt, sitemap.xml, sitemap.txt,
and the local link graph that makes both sitemaps truthful.

Everything is static: no network, no browser, stdlib only.
"""

from __future__ import annotations

import json
import re
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "website"
INDEX = SITE / "index.html"
ERROR_PAGE = SITE / "404.html"
ROBOTS = SITE / "robots.txt"
SITEMAP = SITE / "sitemap.txt"
SITEMAP_XML = SITE / "sitemap.xml"
JS = SITE / "main.js"

SITE_ORIGIN = "http://43.165.170.43"
SITE_PREFIX = "/noveldl/"
SITE_BASE = f"{SITE_ORIGIN}{SITE_PREFIX}"
INDEX_URL = SITE_BASE
ERROR_URL = f"{SITE_BASE}404.html"

#: Namespaces used by the XML sitemap (sitemaps.org core plus hreflang).
SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
XHTML_NS = "http://www.w3.org/1999/xhtml"

#: Served for mistyped URLs, never advertised to crawlers.
NON_INDEXABLE = {"404.html"}

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

#: Non-HTTP schemes that are legitimately navigable and not checked locally.
SKIP_SCHEMES = {"data", "javascript", "mailto", "tel"}

LOCALE_TAG = re.compile(r"^[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-[A-Z]{2}|-[0-9]{3})?$")

#: title/description budgets.  Search engines truncate by pixel width, but a
#: character budget catches the common "a whole paragraph as the description"
#: mistake and keeps both language variants usable.
TITLE_MAX = 60
DESC_MIN = 80
DESC_MAX = 160


def _parse(text: str) -> _Page:
    page = _Page()
    page.feed(text)
    page.close()
    return page


class _Page(HTMLParser):
    """Collect the facts the SEO checks need, without a DOM library."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str]]] = []
        self.ids: set[str] = set()
        self.metas: list[dict[str, str]] = []
        self.links: list[dict[str, str]] = []
        self.anchor_hrefs: list[str] = []
        self.ld_json: list[str] = []
        self._stack: list[str] = []
        self._title: list[str] = []
        self._in_title = False
        self._script: dict[str, str] | None = None
        self._script_body: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = {name.lower(): value or "" for name, value in attrs}
        self.elements.append((tag, data))
        if "id" in data:
            self.ids.add(data["id"])
        if tag == "meta":
            self.metas.append(data)
        elif tag == "link":
            self.links.append(data)
        elif tag == "a" and "href" in data:
            self.anchor_hrefs.append(data["href"])
        elif tag == "title":
            self._in_title = True
        elif tag == "script":
            self._script = data
            self._script_body = []
        if tag not in VOID_ELEMENTS:
            self._stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = {name.lower(): value or "" for name, value in attrs}
        self.elements.append((tag, data))
        if "id" in data:
            self.ids.add(data["id"])
        if tag == "meta":
            self.metas.append(data)
        elif tag == "link":
            self.links.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._script is not None:
            if "ld+json" in self._script.get("type", ""):
                self.ld_json.append("".join(self._script_body))
            self._script = None
        while self._stack:
            if self._stack.pop() == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title.append(data)
        if self._script is not None:
            self._script_body.append(data)


@pytest.fixture(scope="module")
def index_text() -> str:
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index_page(index_text: str) -> _Page:
    return _parse(index_text)


@pytest.fixture(scope="module")
def error_text() -> str:
    return ERROR_PAGE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def error_page(error_text: str) -> _Page:
    return _parse(error_text)


@pytest.fixture(scope="module")
def js() -> str:
    return JS.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# extraction helpers
# --------------------------------------------------------------------------
def _title(text: str) -> str:
    match = re.search(r"<title>(.*?)</title>", text, re.S)
    assert match is not None, "the document has no <title>"
    return match.group(1).strip()


def _content(page: _Page, key: str, attr: str = "property") -> list[str]:
    return [meta["content"] for meta in page.metas if meta.get(attr) == key]


def _one(page: _Page, key: str, attr: str = "property") -> str:
    values = _content(page, key, attr)
    assert len(values) == 1, f"{key}: expected one tag, found {len(values)}"
    assert values[0].strip(), f"{key}: empty content"
    return values[0]


def _canonical(page: _Page) -> str:
    values = [
        link["href"]
        for link in page.links
        if "canonical" in link.get("rel", "").split()
    ]
    assert len(values) == 1, "the page must declare exactly one canonical URL"
    return values[0]


def _alternates(page: _Page) -> dict[str, str]:
    result: dict[str, str] = {}
    for link in page.links:
        tag = link.get("hreflang", "")
        if not tag:
            continue
        assert tag not in result, f"duplicate hreflang: {tag}"
        result[tag] = link["href"]
    return result


def _js_map(js: str, name: str) -> dict[str, str]:
    """Pull a two-language ``{ zh, en }`` string map out of main.js."""
    match = re.search(rf"const {name} = \{{(.*?)\}};", js, re.S)
    assert match is not None, f"{name} is missing from main.js"
    entries = dict(re.findall(r'(zh|en):\s*"([^"]*)"', match.group(1)))
    assert set(entries) == {"zh", "en"}, f"{name} must define zh and en"
    return entries


def _sitemap_urls() -> list[str]:
    text = SITEMAP.read_text(encoding="utf-8")
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _xml_sitemap_root() -> ET.Element:
    """Parse sitemap.xml; a malformed file fails here with a clear error."""
    return ET.parse(SITEMAP_XML).getroot()


def _xml_sitemap_urls(root: ET.Element) -> list[str]:
    return [(loc.text or "").strip() for loc in root.iter(f"{{{SITEMAP_NS}}}loc")]


def _local_page(url: str) -> Path:
    """Map a deployed URL onto the file that serves it."""
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}" == SITE_ORIGIN, url
    assert parsed.path.startswith(SITE_PREFIX), url
    relative = parsed.path[len(SITE_PREFIX) :]
    assert ".." not in Path(relative).parts, url
    if relative == "" or relative.endswith("/"):
        relative += "index.html"
    return SITE / relative


def _parse_robots() -> tuple[list[str], list[str]]:
    lines = [
        line.strip()
        for line in ROBOTS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    sitemaps = [
        line.split(":", 1)[1].strip()
        for line in lines
        if line.lower().startswith("sitemap:")
    ]
    return lines, sitemaps


# --------------------------------------------------------------------------
# titles and descriptions
# --------------------------------------------------------------------------
def test_title_and_description_fit_search_snippets(
    index_text: str, index_page: _Page
) -> None:
    title = _title(index_text)
    assert 0 < len(title) <= TITLE_MAX, f"title is {len(title)} characters"
    assert "\n" not in title

    description = _one(index_page, "description", attr="name")
    assert DESC_MIN <= len(description) <= DESC_MAX, (
        f"description is {len(description)} characters"
    )
    assert description == description.strip()
    assert "\n" not in description
    assert not description.startswith(("http://", "https://"))


def test_language_variants_keep_the_same_budget(
    js: str, index_text: str, index_page: _Page
) -> None:
    titles = _js_map(js, "PAGE_TITLE")
    descriptions = _js_map(js, "META_DESC")

    # The static document is the zh source of truth; JS must not drift from it.
    assert titles["zh"] == _title(index_text)
    assert descriptions["zh"] == _one(index_page, "description", attr="name")

    for lang in ("zh", "en"):
        assert 0 < len(titles[lang]) <= TITLE_MAX, f"{lang} title is too long"
        assert DESC_MIN <= len(descriptions[lang]) <= DESC_MAX, (
            f"{lang} description is {len(descriptions[lang])} characters"
        )

    # ...and switching language must actually apply both, or the en budget
    # above would describe text no crawler ever sees.
    assert re.search(r"document\.title\s*=\s*isEn\s*\?\s*PAGE_TITLE\.en", js)
    assert re.search(r"metaDesc\.setAttribute\(\s*[\"']content[\"'],\s*META_DESC\[", js)


# --------------------------------------------------------------------------
# Open Graph / Twitter cards
# --------------------------------------------------------------------------
OG_REQUIRED = (
    "og:title",
    "og:description",
    "og:type",
    "og:site_name",
    "og:url",
    "og:image",
    "og:image:width",
    "og:image:height",
    "og:image:alt",
    "og:locale",
)


def test_open_graph_and_twitter_cards_are_complete(index_page: _Page) -> None:
    canonical = _canonical(index_page)
    assert canonical == INDEX_URL

    for key in OG_REQUIRED:
        _one(index_page, key)
    for key in (
        "twitter:card",
        "twitter:title",
        "twitter:description",
        "twitter:image",
        "twitter:image:alt",
    ):
        _one(index_page, key, attr="name")

    assert _one(index_page, "og:type") == "website"
    assert _one(index_page, "og:url") == canonical
    assert _one(index_page, "og:locale") == "zh_CN"
    assert "en_US" in _content(index_page, "og:locale:alternate")

    image = _one(index_page, "og:image")
    assert image == f"{canonical}og-image.png"
    assert urlparse(image).query == "" and urlparse(image).fragment == ""
    assert (SITE / "og-image.png").is_file()
    assert _one(index_page, "og:image:width") == "1200"
    assert _one(index_page, "og:image:height") == "630"

    assert _one(index_page, "twitter:card", attr="name") == "summary_large_image"
    assert _one(index_page, "twitter:title", attr="name") == _one(
        index_page, "og:title"
    )
    assert _one(index_page, "twitter:description", attr="name") == _one(
        index_page, "og:description"
    )
    assert _one(index_page, "twitter:image", attr="name") == image
    assert _one(index_page, "twitter:image:alt", attr="name") == _one(
        index_page, "og:image:alt"
    )


# --------------------------------------------------------------------------
# JSON-LD
# --------------------------------------------------------------------------
def test_jsonld_describes_the_canonical_page(index_page: _Page) -> None:
    canonical = _canonical(index_page)
    assert index_page.ld_json, "expected at least one JSON-LD block"

    payloads = [json.loads(raw) for raw in index_page.ld_json]
    assert all(isinstance(payload, dict) for payload in payloads)

    apps = [
        payload for payload in payloads if payload.get("@type") == "SoftwareApplication"
    ]
    assert len(apps) == 1, "expected exactly one SoftwareApplication block"
    app = apps[0]

    assert app["@context"] == "https://schema.org"
    assert app["url"] == canonical
    assert app["name"] == _one(index_page, "og:site_name")
    assert app["description"] == _one(index_page, "description", attr="name")
    assert set(app["inLanguage"]) == {"zh-CN", "en"}
    assert app["applicationCategory"].strip()
    assert app["operatingSystem"].strip()
    assert app["isAccessibleForFree"] is True

    offers = app["offers"]
    assert offers["@type"] == "Offer"
    assert str(offers["price"]) == "0"
    assert re.fullmatch(r"[A-Z]{3}", offers["priceCurrency"])

    assert app["license"].startswith("https://")
    assert app["downloadUrl"].startswith(
        "https://github.com/lingyi-ling/novel-downloader/"
    )


def test_jsonld_download_url_is_the_stable_release_alias(index_page: _Page) -> None:
    """Structured data must point at the "latest" alias, not a fixed tag.

    ``downloadUrl`` is what a search result's install link resolves to. A
    ``/releases/tag/v1.36.0`` or ``/releases/download/v1.36.0/...`` URL there
    turns into a stale (or 404) link the moment the next release ships, and
    crawlers cache it far longer than the page's own markup.
    """
    payloads = [json.loads(raw) for raw in index_page.ld_json]
    app = next(
        payload for payload in payloads if payload.get("@type") == "SoftwareApplication"
    )
    url = app["downloadUrl"]
    assert url == ("https://github.com/lingyi-ling/novel-downloader/releases/latest"), (
        url
    )
    assert "/releases/tag/" not in url
    assert not re.search(r"/releases/download/v?\d", url)


def test_software_version_is_the_only_static_version(index_text: str) -> None:
    """Exactly one version literal may be in the markup, and it must be JSON-LD.

    The JSON-LD ``softwareVersion`` is the one value that has to be static for
    crawlers (and ``test_jsonld_is_parsable_and_version_matches`` pins it to
    the package version). Every *visible* version — the hero chip and the
    showcase pill — is filled in by main.js from the live manifest, so no
    second literal may drift out of date between releases.
    """
    # drop scripts and comments: neither renders a version a visitor reads
    without_scripts = re.sub(
        r"<script\b[^>]*>.*?</script>", "", index_text, flags=re.S | re.I
    )
    without_comments = re.sub(r"<!--.*?-->", "", without_scripts, flags=re.S)
    visible = re.sub(r"<[^>]+>", " ", without_comments)
    # 127.0.0.1 in the curl examples is not a version; require three numeric
    # components that are not part of a dotted quad or an asset filename
    literals = [
        match.group(1)
        for match in re.finditer(r"(?<![\d.])(\d+\.\d+\.\d+)(?![\d.])", visible)
    ]
    literals = [value for value in literals if value != "127.0.0"]
    assert not literals, f"hard-coded version(s) in visible markup: {literals}"

    # the JSON-LD block is allowed exactly one, and it is present
    ld_blocks = re.findall(
        r'<script id="ld-software" type="application/ld\+json">(.*?)</script>',
        index_text,
        re.S,
    )
    assert len(ld_blocks) == 1
    assert re.search(r'"softwareVersion"\s*:\s*"\d+\.\d+\.\d+"', ld_blocks[0])


# --------------------------------------------------------------------------
# hreflang
# --------------------------------------------------------------------------
def test_hreflang_alternates_are_self_consistent(index_page: _Page, js: str) -> None:
    canonical = _canonical(index_page)
    alternates = _alternates(index_page)

    assert set(alternates) == {"zh-CN", "en", "x-default"}
    assert alternates["x-default"] == canonical
    # The document is written in zh-CN, so its language alternate is the
    # canonical itself; pointing it at "?lang=zh" would annotate a URL that
    # canonicalises elsewhere and leave the default page without a self entry.
    assert alternates["zh-CN"] == canonical
    assert alternates["en"] != alternates["zh-CN"]

    html = [data for tag, data in index_page.elements if tag == "html"][0]
    assert html["lang"] == "zh-CN"
    assert html["lang"] in alternates

    for tag, url in alternates.items():
        if tag != "x-default":
            assert LOCALE_TAG.fullmatch(tag), tag
        parsed = urlparse(url)
        assert f"{parsed.scheme}://{parsed.netloc}" == SITE_ORIGIN, tag
        assert parsed.path == SITE_PREFIX, tag
        assert parsed.fragment == "", tag
        assert _local_page(url) == INDEX, tag

    assert parse_qs(urlparse(alternates["en"]).query) == {"lang": ["en"]}
    assert urlparse(alternates["zh-CN"]).query == ""
    assert urlparse(alternates["x-default"]).query == ""

    # The ?lang= deep links must be consumed by the page, otherwise the en
    # alternate is a link to content that never renders in English.
    assert "URLSearchParams(window.location.search)" in js
    assert re.search(r"if \(q === \"en\" \|\| q === \"zh\"\)", js)


# --------------------------------------------------------------------------
# robots.txt / sitemap.xml / sitemap.txt
# --------------------------------------------------------------------------
def test_robots_points_crawlers_at_the_deployed_sitemaps() -> None:
    lines, sitemaps = _parse_robots()

    assert "User-agent: *" in lines
    assert "Allow: /" in lines
    assert not any(line.lower() in {"disallow: /", "disallow: *"} for line in lines)

    # Both formats are advertised, XML first because it carries lastmod and
    # hreflang; the plain-text list stays for crawlers that prefer it.
    assert sitemaps == [
        f"{SITE_BASE}sitemap.xml",
        f"{SITE_BASE}sitemap.txt",
    ]
    assert _local_page(sitemaps[0]) == SITEMAP_XML
    assert _local_page(sitemaps[1]) == SITEMAP
    assert SITEMAP_XML.is_file() and SITEMAP.is_file()
    # The robots file itself must be reachable under the same prefix.
    assert _local_page(f"{SITE_BASE}robots.txt") == ROBOTS


def test_sitemap_lists_every_indexable_page_once(index_page: _Page) -> None:
    urls = _sitemap_urls()
    canonical = _canonical(index_page)

    assert urls, "sitemap.txt is empty"
    assert len(urls) == len(set(urls)), "sitemap contains duplicates"
    assert canonical in urls, "the canonical page is missing from the sitemap"

    for url in urls:
        assert " " not in url and "\t" not in url, url
        parsed = urlparse(url)
        assert parsed.scheme == "http", url
        assert parsed.query == "" and parsed.fragment == "", url
        page = _local_page(url)
        assert page.is_file(), f"sitemap URL has no local page: {url}"
        assert page.suffix == ".html", f"sitemap must list pages, not assets: {url}"

    assert ERROR_URL not in urls, "a 404 page must never be advertised"
    assert "404" not in SITEMAP.read_text(encoding="utf-8")

    # Every deployed HTML page except the 404 must be listed exactly once.
    indexable = {path.name for path in SITE.glob("*.html")} - NON_INDEXABLE
    listed = {_local_page(url).name for url in urls}
    assert listed == indexable


def test_xml_sitemap_is_wellformed_and_matches_the_canonical(
    index_page: _Page,
) -> None:
    root = _xml_sitemap_root()
    assert root.tag == f"{{{SITEMAP_NS}}}urlset"
    assert not root.findall(f"{{{SITEMAP_NS}}}sitemapindex"), (
        "sitemap.xml is a URL set, not an index of other sitemaps"
    )

    entries = root.findall(f"{{{SITEMAP_NS}}}url")
    locs = _xml_sitemap_urls(root)
    canonical = _canonical(index_page)

    assert len(entries) == len(locs) == 1
    assert locs == [canonical], "the XML sitemap must list the canonical URL"
    assert len(locs) == len(set(locs)), "sitemap.xml contains duplicates"

    # Each <loc> must serve a real page under the deployed prefix.
    page = _local_page(locs[0])
    assert page.is_file()
    assert page == INDEX

    # Every deployed HTML page except the 404 is listed exactly once.
    indexable = {path.name for path in SITE.glob("*.html")} - NON_INDEXABLE
    assert {_local_page(loc).name for loc in locs} == indexable
    assert ERROR_URL not in locs, "a 404 page must never be advertised"
    assert "404" not in SITEMAP_XML.read_text(encoding="utf-8")

    # lastmod is a stable, hand-maintained date: present, ISO, not in the
    # future.  Deriving it from mtimes would drift on every fresh checkout.
    lastmod = entries[0].findtext(f"{{{SITEMAP_NS}}}lastmod")
    assert lastmod is not None
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", lastmod), lastmod
    assert date.fromisoformat(lastmod) <= date.today(), lastmod

    # The hreflang annotations must match the page's own <link rel=alternate>
    # set, or the sitemap would advertise languages the page does not render.
    links = [
        link
        for link in entries[0].findall(f"{{{XHTML_NS}}}link")
        if link.get("rel") == "alternate"
    ]
    tags = [link.get("hreflang", "") for link in links]
    assert len(tags) == len(set(tags)), "duplicate hreflang in sitemap.xml"
    alternates = {link.get("hreflang", ""): link.get("href", "") for link in links}
    assert alternates == _alternates(index_page)
    assert parse_qs(urlparse(alternates["en"]).query) == {"lang": ["en"]}


# --------------------------------------------------------------------------
# local link graph
# --------------------------------------------------------------------------
def _check_anchor(
    href: str,
    page_path: Path,
    pages: dict[Path, _Page],
) -> None:
    assert href == href.strip() and href, f"empty or padded href: {href!r}"
    parsed = urlparse(href)

    if parsed.scheme in SKIP_SCHEMES:
        return
    if href.startswith("//"):
        # Protocol-relative external links need no local resolution.
        return
    if parsed.scheme in {"http", "https"}:
        if f"{parsed.scheme}://{parsed.netloc}" != SITE_ORIGIN:
            return
        target = _local_page(href).resolve()
    elif parsed.path == "":
        target = page_path.resolve()
    elif parsed.path.startswith("/"):
        assert parsed.path.startswith(SITE_PREFIX), (
            f"site-root link escapes the deployment prefix: {href}"
        )
        target = _local_page(f"{SITE_ORIGIN}{parsed.path}").resolve()
    else:
        target = (page_path.parent / parsed.path).resolve()
        assert target.is_relative_to(SITE.resolve()), href

    assert target.is_file(), f"link points at a missing file: {href}"
    assert target == page_path.resolve() or target != ERROR_PAGE.resolve(), (
        f"link points at the 404 document itself: {href}"
    )

    query = parse_qs(parsed.query)
    if query:
        assert target == INDEX, f"query parameters are only meaningful on /: {href}"
        assert set(query) <= {"lang"}, href
        assert set(query.get("lang", [])) <= {"zh", "en"}, href

    fragment = parsed.fragment
    if fragment:
        assert target.suffix == ".html", f"fragment on a non-page: {href}"
        page = pages.get(target)
        assert page is not None, f"no parsed document for {target}"
        assert fragment in page.ids, f"dead anchor: {href}"


def test_all_internal_links_resolve(index_page: _Page, error_page: _Page) -> None:
    pages = {INDEX.resolve(): index_page, ERROR_PAGE.resolve(): error_page}

    assert index_page.anchor_hrefs, "the home page has no anchors at all"
    for href in index_page.anchor_hrefs:
        _check_anchor(href, INDEX, pages)
    for href in error_page.anchor_hrefs:
        _check_anchor(href, ERROR_PAGE, pages)


# --------------------------------------------------------------------------
# the 404 page
# --------------------------------------------------------------------------
def test_404_is_not_advertised_to_crawlers(error_text: str, error_page: _Page) -> None:
    robots = _content(error_page, "robots", attr="name")
    assert len(robots) == 1
    assert "noindex" in robots[0].lower()

    assert ERROR_URL not in _sitemap_urls()
    assert ERROR_URL not in _xml_sitemap_urls(_xml_sitemap_root())
    # A noindex page must not claim the home page's canonical (or any other).
    assert not [link for link in error_page.links if link.get("rel") == "canonical"]
    assert not [
        meta for meta in error_page.metas if meta.get("property", "").startswith("og:")
    ]
    assert not error_page.ld_json

    title = _title(error_text)
    assert 0 < len(title) <= TITLE_MAX
    description = _one(error_page, "description", attr="name")
    assert DESC_MIN <= len(description) <= DESC_MAX
