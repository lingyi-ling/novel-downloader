#!/usr/bin/env python3
"""
novel_downloader.infra.book_url_resolver
----------------------------------------

Utility for resolving a novel site URL into a standardized configuration.
"""

from __future__ import annotations

__all__ = ["resolve_book_url"]

import logging
import re
from collections.abc import Callable
from typing import TypedDict, TypeVar
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger(__name__)

BookURLFunc = Callable[[str, str], "BookURLInfo | None"]
E = TypeVar("E", bound=BookURLFunc)
_REGISTRY: dict[str, BookURLFunc] = {}

# Book URLs are short; anything longer is paste garbage and is rejected before
# it reaches the regexes.  Generous enough for any real page (query strings
# included) while bounding the work done on hostile input.
_MAX_URL_LENGTH = 8192

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
# ``host:port/path`` pasted without a scheme (the "scheme" is not a scheme).
_HOST_PORT_RE = re.compile(r"^[^/?#:]+:\d+(?:[/?#]|$)")
_PERCENT_RE = re.compile(r"%([0-9A-Fa-f]{2})")
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
# U+3002 / U+FF0E / U+FF61 are treated as dots by browsers.
_DOT_VARIANTS = str.maketrans({"\u3002": ".", "\uff0e": ".", "\uff61": "."})


class BookURLInfo(TypedDict):
    site_key: str
    book_id: str | None
    chapter_id: str | None


def register_extractor(hosts: list[str]) -> Callable[[E], E]:
    def decorator(func: E) -> E:
        for host in hosts:
            _REGISTRY[host] = func
        return func

    return decorator


def _decode_unreserved(text: str) -> str:
    """
    Decode ``%XX`` escapes that stand for unreserved characters only.

    Reserved escapes (``%2F``, ``%3F``, ``%40`` …) are left untouched so a
    pasted URL can never smuggle a path separator or an authority delimiter
    into the host/path we match on.
    """
    if "%" not in text:
        return text

    def _replace(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else match.group(0)

    return _PERCENT_RE.sub(_replace, text)


def _prepare_url(text: str) -> str | None:
    """
    Turn pasted/scraped input into an absolute ``http(s)`` URL.

    :return: the URL to parse, or None when the input uses a scheme that can
        never be a book page (``javascript:``, ``data:``, ``file:`` …).
    """
    match = _SCHEME_RE.match(text)
    if match is not None:
        scheme = match.group(1).lower()
        if scheme in ("http", "https"):
            # WHATWG parsing: after a special scheme, zero or more slashes
            # still introduce the authority (``https:/host`` == ``https://host``).
            rest = text[match.end() :].lstrip("/")
            return f"{scheme}://{rest}"
        # ``host:port/path`` without a scheme is not an unsupported scheme.
        if _HOST_PORT_RE.match(text):
            return "http://" + text
        return None
    if text.startswith("//"):
        return "http://" + text.lstrip("/")
    return "http://" + text


def _canonical_host(netloc: str, scheme: str) -> str:
    """Lower-case, de-credential, de-port and IDNA-normalize a netloc."""
    host = netloc.rpartition("@")[2].lower()
    if scheme == "http" and host.endswith(":80"):
        host = host[:-3]
    elif scheme == "https" and host.endswith(":443"):
        host = host[:-4]
    # IP literals and the empty host can never match a registered site host;
    # leave them as-is instead of mangling the brackets.
    if not host or host.startswith("["):
        return host
    host = _decode_unreserved(host).translate(_DOT_VARIANTS)
    while host.endswith(".") and len(host) > 1:
        host = host[:-1]
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            # Keep the raw spelling: it will not match any registry entry.
            return host
    return host


def _canonical_path(raw_path: str) -> str:
    """Normalize a path the way a browser does before sending a request."""
    path = _decode_unreserved(raw_path or "/")
    if not path.startswith("/"):
        path = "/" + path
    # Trailing ``;params`` (``;jsessionid=…``) are session bookkeeping, not
    # part of the resource the site serves.
    head, sep, last = path.rpartition("/")
    if ";" in last:
        path = head + sep + last.split(";", 1)[0]
    path = re.sub(r"/{2,}", "/", path)
    segments: list[str] = []
    for segment in path.split("/"):
        if segment == ".":
            continue
        if segment == "..":
            if len(segments) > 1:
                segments.pop()
            continue
        segments.append(segment)
    return "/".join(segments) or "/"


def _backslash_to_slash(text: str) -> str:
    """Read ``\\`` as ``/`` in the authority/path, as browsers do."""
    cut = len(text)
    for marker in ("?", "#"):
        index = text.find(marker)
        if index != -1:
            cut = min(cut, index)
    return text[:cut].replace("\\", "/") + text[cut:]


def _normalize_host_and_path(url: str) -> tuple[str, str, str]:
    """
    Normalize a URL into ``(canonical_host, path, query)``.

      * Trim surrounding whitespace and a possible BOM (pasted input); reject
        absurdly long input outright.
      * Read ``\\`` as ``/`` (browser behaviour for http(s) URLs).
      * Reject non-HTTP(S) schemes (``javascript:``, ``data:``, ``file:`` …)
        and malformed authorities instead of raising ``ValueError``.
      * Treat the scheme case-insensitively; a protocol-relative
        ``//host/path`` link is read as ``http``.
      * Lowercase the host, drop credentials (``user:pass@``), strip a
        redundant default port (``:80`` for http, ``:443`` for https) and a
        trailing root dot, and IDNA-normalize non-ASCII hosts. Non-default
        ports are kept: they are meaningful.
      * Normalize the path (unreserved percent-escapes, duplicate slashes,
        dot segments) but never decode reserved escapes, so ``%2F`` stays a
        path *segment* character rather than becoming a separator.
    """
    text = url.strip().lstrip("\ufeff").strip()
    if len(text) > _MAX_URL_LENGTH:
        return "", "", ""
    prepared = _prepare_url(_backslash_to_slash(text))
    if prepared is None:
        return "", "", ""
    try:
        parsed = urlsplit(prepared)
    except ValueError:
        return "", "", ""
    host = _canonical_host(parsed.netloc, parsed.scheme.lower())
    return host, _canonical_path(parsed.path), parsed.query or ""


def _query_param(query: str, name: str, pattern: str | None = None) -> str | None:
    """
    Return the first value of an *exact* query parameter ``name``.

    Substring matching would let ``?chapterid=9`` masquerade as ``?id=9``.
    """
    values = parse_qs(query, keep_blank_values=True).get(name)
    if not values:
        return None
    value = values[0]
    if pattern is not None and re.fullmatch(pattern, value) is None:
        return None
    return value


def _make_info(
    site_key: str, book_id: str | None, chap_id: str | None = None
) -> BookURLInfo | None:
    return {
        "site_key": site_key,
        "book_id": book_id,
        "chapter_id": chap_id,
    }


def resolve_book_url(url: str) -> BookURLInfo | None:
    """
    Resolve a novel site URL into a standardized BookURLInfo.

      * If an extractor matches, return a BookURLInfo dict.
      * Otherwise, fall back to the rule-driven ("book source") registry.

    :param url: URL string.
    :return: BookURLInfo dict or None if unresolved.
    """
    host, path, query = _normalize_host_and_path(url)
    if extractor := _REGISTRY.get(host):
        return extractor(path, query)

    # Rule-based sources are resolved by host + regex declared in their JSON.
    try:
        from novel_downloader.plugins.rule import resolve_url as _resolve_rule_url
    except Exception:  # pragma: no cover - rule package always present
        return None

    info = _resolve_rule_url(host, path, query)
    return info  # type: ignore[return-value]


@register_extractor(["www.aaatxt.com"])
def extract_aaatxt(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/shu/(\\d+)\\.html$", path):
        return _make_info("aaatxt", m.group(1), None)
    if m := re.search("^/yuedu/(\\d+_\\d+)\\.html$", path):
        return _make_info("aaatxt", m.group(1).split("_")[0], m.group(1))
    return None


@register_extractor(["www.akatsuki-novels.com"])
def extract_akatsuki_novels(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("/stories/view/(\\d+)/novel_id~(\\d+)", path):
        return _make_info("akatsuki_novels", m.group(2), m.group(1))
    if m := re.search("novel_id~(\\d+)", path):
        return _make_info("akatsuki_novels", m.group(1), None)
    return None


@register_extractor(["www.alicesw.com"])
def extract_alicesw(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/book/(\\d+)/([^.]+)\\.html$", path):
        return _make_info("alicesw", None, f"{m.group(1)}-{m.group(2)}")
    if m := re.search("^/novel/(\\d+)\\.html$", path):
        return _make_info("alicesw", m.group(1), None)
    if m := re.search("^/other/chapters/id/(\\d+)\\.html$", path):
        return _make_info("alicesw", m.group(1), None)
    return None


@register_extractor(["www.alphapolis.co.jp"])
def extract_alphapolis(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/novel/(\\d+)/(\\d+)/episode/(\\d+)$", path):
        return _make_info("alphapolis", f"{m.group(1)}-{m.group(2)}", m.group(3))
    if m := re.search("^/novel/(\\d+)/(\\d+)$", path):
        return _make_info("alphapolis", f"{m.group(1)}-{m.group(2)}", None)
    return None


@register_extractor(
    [
        # ``www.b520.cc`` stopped serving search; ``b5200.org`` now redirects
        # to ``www.bqg6.xyz``, which serves the same catalogue under the same
        # book ids.  Users copy links from any of these hosts.
        "www.b520.cc",
        "www.bqg6.xyz",
        "bqg6.xyz",
        "www.b5200.org",
        "b5200.org",
    ]
)
def extract_b520(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/(\\d+_\\d+)/(\\d+)\\.html$", path):
        return _make_info("b520", m.group(1), m.group(2))
    if m := re.search("^/(\\d+_\\d+)/?$", path):
        return _make_info("b520", m.group(1), None)
    return None


@register_extractor(["www.biquge345.com"])
def extract_biquge345(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/chapter/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("biquge345", m.group(1), m.group(2))
    if m := re.search("^/book/(\\d+)/?$", path):
        return _make_info("biquge345", m.group(1), None)
    return None


@register_extractor(["www.biquge5.com"])
def extract_biquge5(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/(\\d+_\\d+)/(\\d+)\\.html$", path):
        return _make_info("biquge5", m.group(1), m.group(2))
    if m := re.search("^/(\\d+_\\d+)/?$", path):
        return _make_info("biquge5", m.group(1), None)
    return None


@register_extractor(["www.biquguo.com"])
def extract_biquguo(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/(\\d+)/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("biquguo", f"{m.group(1)}-{m.group(2)}", m.group(3))
    if m := re.search("^/(\\d+)/(\\d+)/?$", path):
        return _make_info("biquguo", f"{m.group(1)}-{m.group(2)}", None)
    return None


@register_extractor(["biquyuedu.com"])
def extract_biquyuedu(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/novel/([^/]+)/(\\d+)\\.html$", path):
        return _make_info("biquyuedu", m.group(1), m.group(2))
    if m := re.search("^/novel/([^.]+)\\.html$", path):
        return _make_info("biquyuedu", m.group(1), None)
    return None


@register_extractor(["m.bixiange.me", "m.bixiange.top"])
def extract_bixiange(path: str, query: str) -> BookURLInfo | None:
    if m := re.match(r"^/([^/]+)/(\d+)/index/(\d+)\.html$", path):
        cat, bid, chap = m.groups()
        return _make_info("bixiange", f"{cat}-{bid}", chap)
    if m := re.match(r"^/([^/]+)/(\d+)/(\d+)\.html$", path):
        cat, bid, chap = m.groups()
        return _make_info("bixiange", f"{cat}-{bid}", chap)
    if m := re.match(r"^/([^/]+)/(\d+)", path):
        cat, bid = m.groups()
        return _make_info("bixiange", f"{cat}-{bid}", None)
    return None


@register_extractor(["www.blqudu.cc", "www.biqudv.cc"])
def extract_blqudu(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/(\\d+_\\d+)/(\\d+)\\.html$", path):
        return _make_info("blqudu", m.group(1), m.group(2))
    if m := re.search("^/(\\d+_\\d+)/?$", path):
        return _make_info("blqudu", m.group(1), None)
    return None


@register_extractor(["www.bxwx9.org"])
def extract_bxwx9(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/b/(\\d+)/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("bxwx9", f"{m.group(1)}-{m.group(2)}", m.group(3))
    if m := re.search("^/b/(\\d+)/(\\d+)/?$", path):
        return _make_info("bxwx9", f"{m.group(1)}-{m.group(2)}", None)
    return None


@register_extractor(["www.ciluke.com"])
def extract_ciluke(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/(\\d+)/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("ciluke", f"{m.group(1)}-{m.group(2)}", m.group(3))
    if m := re.search("^/(\\d+)/(\\d+)/?$", path):
        return _make_info("ciluke", f"{m.group(1)}-{m.group(2)}", None)
    return None


@register_extractor(["www.ciweimao.com"])
def extract_ciweimao(path: str, query: str) -> BookURLInfo | None:
    if m := re.search(r"^/chapter/(\d+)$", path):
        return _make_info("ciweimao", None, m.group(1))
    if m := re.search(r"^/book/(\d+)$", path):
        return _make_info("ciweimao", m.group(1), None)
    if m := re.search(r"^/chapter-list/(\d+)(?:/|$)", path):
        return _make_info("ciweimao", m.group(1), None)
    return None


@register_extractor(["www.ciyuanji.com"])
def extract_ciyuanji(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/chapter/(\\d+)_(\\d+)\\.html$", path):
        return _make_info("ciyuanji", m.group(1), m.group(2))
    if m := re.search("^/b_d_(\\d+)\\.html$", path):
        return _make_info("ciyuanji", m.group(1), None)
    return None


@register_extractor(["czbooks.net"])
def extract_czbooks(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/n/([a-zA-Z0-9]+)/([a-zA-Z0-9]+)", path):
        return _make_info("czbooks", m.group(1), m.group(2))
    if m := re.search("^/n/([a-zA-Z0-9]+)", path):
        return _make_info("czbooks", m.group(1), None)
    return None


@register_extractor(["www.deqixs.com"])
def extract_deqixs(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/xiaoshuo/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("deqixs", m.group(1), m.group(2))
    if m := re.search("^/xiaoshuo/(\\d+)/?$", path):
        return _make_info("deqixs", m.group(1), None)
    return None


@register_extractor(["www.dushu.com"])
def extract_dushu(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/showbook/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("dushu", m.group(1), m.group(2))
    if m := re.search("^/showbook/(\\d+)/?$", path):
        return _make_info("dushu", m.group(1), None)
    return None


@register_extractor(["www.dxmwx.org", "tw.dxmwx.org"])
def extract_dxmwx(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/read/(\\d+)_(\\d+)\\.html$", path):
        return _make_info("dxmwx", m.group(1), m.group(2))
    if m := re.search("^/(?:book|chapter)/(\\d+)\\.html$", path):
        return _make_info("dxmwx", m.group(1), None)
    return None


@register_extractor(["www.esjzone.cc"])
def extract_esjzone(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/forum/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("esjzone", m.group(1), m.group(2))
    if m := re.search("^/detail/(\\d+)\\.html$", path):
        return _make_info("esjzone", m.group(1), None)
    return None


@register_extractor(["b.faloo.com"])
def extract_faloo(path: str, query: str) -> BookURLInfo | None:
    if m := re.search(r"^/(\d+)_(\d+)\.html$", path):
        return _make_info("faloo", m.group(1), m.group(2))
    if m := re.search(r"^/(\d+)\.html$", path):
        return _make_info("faloo", m.group(1), None)
    return None


@register_extractor(["fanqienovel.com"])
def extract_fanqienovel(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/reader/(\\d+)", path):
        return _make_info("fanqienovel", None, m.group(1))
    if m := re.search("^/page/(\\d+)", path):
        return _make_info("fanqienovel", m.group(1), None)
    return None


@register_extractor(["www.fsshu.com"])
def extract_fsshu(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/biquge/(\\d+_\\d+)/([a-zA-Z0-9]+)\\.html$", path):
        return _make_info("fsshu", m.group(1), m.group(2))
    if m := re.search("^/biquge/(\\d+_\\d+)/?$", path):
        return _make_info("fsshu", m.group(1), None)
    return None


@register_extractor(["b.guidaye.com"])
def extract_b(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/([^/]+)/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("guidaye", f"{m.group(1)}-{m.group(2)}", m.group(3))
    if m := re.search("^/([^/]+)/(\\d+)/?$", path):
        return _make_info("guidaye", f"{m.group(1)}-{m.group(2)}", None)
    return None


@register_extractor(["www.haiwaishubao.com"])
def extract_haiwaishubao(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/(?:book|index)/(\\d+)/(\\d+)(?:_\\d+)?\\.html$", path):
        return _make_info("haiwaishubao", m.group(1), m.group(2))
    if m := re.search("^/(?:book|index)/(\\d+)/", path):
        return _make_info("haiwaishubao", m.group(1), None)
    return None


@register_extractor(["www.hetushu.com", "www.hetubook.com"])
def extract_hetushu(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/book/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("hetushu", m.group(1), m.group(2))
    if m := re.search("^/book/(\\d+)/", path):
        return _make_info("hetushu", m.group(1), None)
    return None


@register_extractor(["hongxiuzhao.net"])
def extract_hongxiuzhao(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/([A-Za-z0-9]+)\\.html$", path):
        return _make_info("hongxiuzhao", m.group(1), None)
    return None


@register_extractor(["www.i25zw.com"])
def extract_i25zw(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("i25zw", m.group(1), m.group(2))
    if m := re.search("^/(?:book/)?(\\d+)\\.html$", path):
        return _make_info("i25zw", m.group(1), None)
    if m := re.search("^/(\\d+)/?$", path):
        return _make_info("i25zw", m.group(1), None)
    return None


@register_extractor(["ixdzs8.com"])
def extract_ixdzs8(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/read/(\\d+)/([a-zA-Z0-9]+)\\.html$", path):
        return _make_info("ixdzs8", m.group(1), m.group(2))
    if m := re.search("^/read/(\\d+)/?$", path):
        return _make_info("ixdzs8", m.group(1), None)
    return None


@register_extractor(["www.jpxs123.com"])
def extract_jpxs123(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/([^/]+)/([^/]+)/(\\d+)\\.html$", path):
        return _make_info("jpxs123", f"{m.group(1)}-{m.group(2)}", m.group(3))
    if m := re.search("^/([^/]+)/([^.]+)\\.html$", path):
        return _make_info("jpxs123", f"{m.group(1)}-{m.group(2)}", None)
    return None


@register_extractor(["www.kadokado.com.tw"])
def extract_kadokado(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/chapter/(\\d+)", path):
        return _make_info("kadokado", None, m.group(1))
    if m := re.search("^/book/(\\d+)", path):
        return _make_info("kadokado", m.group(1), None)
    return None


@register_extractor(["www.ktshu.cc"])
def extract_ktshu(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/book/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("ktshu", m.group(1), m.group(2))
    if m := re.search("^/book/(\\d+)/?$", path):
        return _make_info("ktshu", m.group(1), None)
    return None


@register_extractor(["www.kunnu.com"])
def extract_kunnu(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/([^/]+)/(\\d+)\\.htm$", path):
        return _make_info("kunnu", m.group(1), m.group(2))
    if m := re.search("^/([^/]+)/?$", path):
        return _make_info("kunnu", m.group(1), None)
    return None


@register_extractor(["www.laoyaoxs.org"])
def extract_laoyaoxs(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/list/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("laoyaoxs", m.group(1), m.group(2))
    if m := re.search("^/(?:info|list)/(\\d+)(?:\\.html|/)?$", path):
        return _make_info("laoyaoxs", m.group(1), None)
    return None


@register_extractor(["www.lewenn.net"])
def extract_lewenn(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/([^/]+)/(\\d+)\\.html$", path):
        return _make_info("lewenn", m.group(1), m.group(2))
    if m := re.search("^/([^/]+)/?$", path):
        return _make_info("lewenn", m.group(1), None)
    return None


@register_extractor(["www.linovel.net"])
def extract_linovel(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/book/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("linovel", m.group(1), m.group(2))
    if m := re.search("^/book/(\\d+)(?:\\.html|/)?", path):
        return _make_info("linovel", m.group(1), None)
    return None


@register_extractor(["www.linovelib.com"])
def extract_linovelib(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/novel/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("linovelib", m.group(1), m.group(2))
    if m := re.search("^/novel/(\\d+)/vol_\\d+\\.html$", path):
        return _make_info("linovelib", m.group(1), None)
    if m := re.search("^/novel/(\\d+)\\.html$", path):
        return _make_info("linovelib", m.group(1), None)
    return None


@register_extractor(["lnovel.org", "lnovel.tw"])
def extract_lnovel(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/chapters-(\\d+)$", path):
        return _make_info("lnovel", None, m.group(1))
    if m := re.search("^/books-(\\d+)$", path):
        return _make_info("lnovel", m.group(1), None)
    return None


@register_extractor(["www.lvscwx.cc", "www.lvsewx.cc"])
def extract_lvsewx(path: str, query: str) -> BookURLInfo | None:
    if m := re.search(r"^/ebook/(\d+)\.html$", path):
        return _make_info("lvsewx", m.group(1), None)
    if m := re.search(r"^/books/\d+/(\d+)/(\d+)\.html$", path):
        return _make_info("lvsewx", m.group(1), m.group(2))
    return None


@register_extractor(["www.mangg.com"])
def extract_mangg_com(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/(id\\d+)/(\\d+)\\.html$", path):
        return _make_info("mangg_com", m.group(1), m.group(2))
    if m := re.search("^/(id\\d+)/?$", path):
        return _make_info("mangg_com", m.group(1), None)
    return None


@register_extractor(["www.mangg.net"])
def extract_mangg_net(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/(id\\d+)/(\\d+)\\.html$", path):
        return _make_info("mangg_net", m.group(1), m.group(2))
    if m := re.search("^/(id\\d+)/", path):
        return _make_info("mangg_net", m.group(1), None)
    return None


@register_extractor(["m.mjyhb.com"])
def extract_mjyhb(path: str, query: str) -> BookURLInfo | None:
    if m := re.search(r"^/read_([a-z0-9]+)/([a-z0-9]+)(?:_\d+)?\.html$", path, re.I):
        return _make_info("mjyhb", m.group(1), m.group(2))
    if m := re.search(r"^/info_([a-z0-9]+)/?$", path, re.I):
        return _make_info("mjyhb", m.group(1), None)
    return None


@register_extractor(["101kanshu.com"])
def extract_n101kanshu(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/txt/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("n101kanshu", m.group(1), m.group(2))
    if m := re.search("^/book/(\\d+)(?:/index)?\\.html?$", path):
        return _make_info("n101kanshu", m.group(1), None)
    return None


@register_extractor(["www.17k.com"])
def extract_n17k(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/chapter/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("n17k", m.group(1), m.group(2))
    if m := re.search("^/(?:book|list)/(\\d+)\\.html$", path):
        return _make_info("n17k", m.group(1), None)
    return None


@register_extractor(["www.23ddw.net"])
def extract_n23ddw(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/du/(\\d+)/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("n23ddw", f"{m.group(1)}-{m.group(2)}", m.group(3))
    if m := re.search("^/du/(\\d+)/(\\d+)/?$", path):
        return _make_info("n23ddw", f"{m.group(1)}-{m.group(2)}", None)
    return None


@register_extractor(["www.23qb.com"])
def extract_n23qb(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/book/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("n23qb", m.group(1), m.group(2))
    if m := re.search("^/book/(\\d+)/", path):
        return _make_info("n23qb", m.group(1), None)
    return None


@register_extractor(["www.37yq.com"])
def extract_n37yq(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/lightnovel/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("n37yq", m.group(1), m.group(2))
    if m := re.search("^/lightnovel/(\\d+)(?:\\.html|/catalog)?$", path):
        return _make_info("n37yq", m.group(1), None)
    return None


@register_extractor(["www.37yue.com"])
def extract_n37yue(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/(\\d+)/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("n37yue", f"{m.group(1)}-{m.group(2)}", m.group(3))
    if m := re.search("^/(\\d+)/(\\d+)/?$", path):
        return _make_info("n37yue", f"{m.group(1)}-{m.group(2)}", None)
    return None


@register_extractor(["www.69hao.com", "www.69hsw.com"])
def extract_n69hao(path: str, query: str) -> BookURLInfo | None:
    if m := re.search(r"^/(\d+)/(\d+)\.html$", path):
        return _make_info("n69hao", m.group(1), m.group(2))
    if m := re.search(r"^/(\d+)/?$", path):
        return _make_info("n69hao", m.group(1), None)
    return None


@register_extractor(["www.69shuba.com"])
def extract_n69shuba(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/txt/(\\d+)/(\\d+)", path):
        return _make_info("n69shuba", m.group(1), m.group(2))
    if m := re.search("^/book/(\\d+)(?:\\.htm|/)?$", path):
        return _make_info("n69shuba", m.group(1), None)
    return None


@register_extractor(["www.69yue.top"])
def extract_n69yue(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/article/(\\d+)\\.html$", path):
        return _make_info("n69yue", None, m.group(1))
    if m := re.search("^/articlecategroy/([^.]+)\\.html$", path):
        return _make_info("n69yue", m.group(1), None)
    if path == "/mulu.html" and (pid := _query_param(query, "pid", "[A-Za-z0-9]+")):
        return _make_info("n69yue", pid, None)
    return None


@register_extractor(["www.71ge.com"])
def extract_n71ge(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/(\\d+_\\d+)/(\\d+)\\.html$", path):
        return _make_info("n71ge", m.group(1), m.group(2))
    if m := re.search("^/(\\d+_\\d+)/?$", path):
        return _make_info("n71ge", m.group(1), None)
    return None


@register_extractor(["www.8novel.com", "article.8novel.com"])
def extract_n8novel(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/read/(\\d+)/?$", path):
        # Chapter links carry the id as a bare query token (``?106235``).
        if mq := re.match(r"(\d+)", query):
            return _make_info("n8novel", m.group(1), mq.group(1))
        return _make_info("n8novel", m.group(1), None)
    if m := re.search("^/novelbooks/(\\d+)/?$", path):
        return _make_info("n8novel", m.group(1), None)
    return None


@register_extractor(["www.8tsw.com"])
def extract_n8tsw(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/(\\d+_\\d+)/(\\d+)\\.html$", path):
        return _make_info("n8tsw", m.group(1), m.group(2))
    if m := re.search("^/(\\d+_\\d+)/?$", path):
        return _make_info("n8tsw", m.group(1), None)
    return None


@register_extractor(["novelpia.jp"])
def extract_novelpia(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/viewer/(\\d+)", path):
        return _make_info("novelpia", None, m.group(1))
    if m := re.search("^/novel/(\\d+)", path):
        return _make_info("novelpia", m.group(1), None)
    return None


@register_extractor(["www.piaotia.com"])
def extract_piaotia(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/html/(\\d+)/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("piaotia", f"{m.group(1)}-{m.group(2)}", m.group(3))
    if m := re.search("^/(?:bookinfo|html)/(\\d+)/(\\d+)(?:/|\\.html$)", path):
        return _make_info("piaotia", f"{m.group(1)}-{m.group(2)}", None)
    return None


@register_extractor(["www.pilibook.net", "www.mozishuwu.com"])
def extract_pilibook(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/(\\d+)/(\\d+)/read/(\\d+)\\.html$", path):
        return _make_info("pilibook", f"{m.group(1)}-{m.group(2)}", m.group(3))
    if m := re.search("^/(\\d+)/(\\d+)/(?:info|menu)(?:/[\\w.-]*)?\\.?html?$", path):
        return _make_info("pilibook", f"{m.group(1)}-{m.group(2)}", None)
    return None


@register_extractor(["www.qbtr.cc"])
def extract_qbtr(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/([^/]+)/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("qbtr", f"{m.group(1)}-{m.group(2)}", m.group(3))
    if m := re.search("^/([^/]+)/(\\d+)\\.html$", path):
        return _make_info("qbtr", f"{m.group(1)}-{m.group(2)}", None)
    return None


@register_extractor(["www.qidian.com"])
def extract_qidian(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/chapter/(\\d+)/(\\d+)/", path):
        return _make_info("qidian", m.group(1), m.group(2))
    if m := re.search("^/book/(\\d+)/", path):
        return _make_info("qidian", m.group(1), None)
    return None


@register_extractor(["www.qimao.com"])
def extract_qimao(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/shuku/(\\d+)-(\\d+)", path):
        return _make_info("qimao", m.group(1), m.group(2))
    if m := re.search("^/shuku/(\\d+)", path):
        return _make_info("qimao", m.group(1), None)
    return None


@register_extractor(["book.qq.com"])
def extract_qq(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/book-read/(\\d+)/(\\d+)", path):
        return _make_info("qqbook", m.group(1), m.group(2))
    if m := re.search("^/book-detail/(\\d+)", path):
        return _make_info("qqbook", m.group(1), None)
    return None


@register_extractor(["quanben5.com", "big5.quanben5.com"])
def extract_quanben5(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/n/([^/]+)/(\\d+)\\.html$", path):
        return _make_info("quanben5", m.group(1), m.group(2))
    if m := re.search("^/n/([^/]+)/", path):
        return _make_info("quanben5", m.group(1), None)
    return None


@register_extractor(["www.ruochu.com"])
def extract_ruochu(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/book/(\\d+)/(\\d+)$", path):
        return _make_info("ruochu", m.group(1), m.group(2))
    if m := re.search("^/(?:book|chapter)/(\\d+)", path):
        return _make_info("ruochu", m.group(1), None)
    return None


@register_extractor(["m.sfacg.com"])
def extract_sfacg(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/c/(\\d+)/", path):
        return _make_info("sfacg", None, m.group(1))
    if m := re.search("^/(?:b|i)/(\\d+)/", path):
        return _make_info("sfacg", m.group(1), None)
    return None


@register_extractor(["www.shaoniandream.com"])
def extract_shaoniandream(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/readchapter/(\\d+)", path):
        return _make_info("shaoniandream", None, m.group(1))
    if m := re.search("^/book_detail/(\\d+)$", path):
        return _make_info("shaoniandream", m.group(1), None)
    return None


@register_extractor(["m.shauthor.com"])
def extract_shauthor(path: str, query: str) -> BookURLInfo | None:
    if m := re.search(r"^/read_([a-z0-9]+)/([a-z0-9]+)(?:_\d+)?\.html$", path, re.I):
        return _make_info("shauthor", m.group(1), m.group(2))
    if m := re.search(r"^/info_([a-z0-9]+)/?$", path, re.I):
        return _make_info("shauthor", m.group(1), None)
    return None


@register_extractor(["www.shencou.com"])
def extract_shencou(path: str, query: str) -> BookURLInfo | None:
    if m := re.match(r"^/books/read_(\d+)\.html$", path):
        return _make_info("shencou", m.group(1), None)
    if m := re.match(r"^/read/\d+/(\d+)/(\d+)\.html$", path):
        return _make_info("shencou", m.group(1), m.group(2))
    if m := re.match(r"^/read/\d+/(\d+)/", path):
        return _make_info("shencou", m.group(1), None)
    return None


@register_extractor(["www.shu111.com"])
def extract_shu111(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/book/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("shu111", m.group(1), m.group(2))
    if m := re.search("^/book/(\\d+)\\.html$", path):
        return _make_info("shu111", m.group(1), None)
    return None


@register_extractor(["www.shuhaige.net"])
def extract_shuhaige(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("shuhaige", m.group(1), m.group(2))
    if m := re.search("^/(\\d+)/?$", path):
        return _make_info("shuhaige", m.group(1), None)
    return None


@register_extractor(["ncode.syosetu.com"])
def extract_ncode(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/([nN]\\w+)/(\\d+)/?$", path):
        return _make_info("syosetu", m.group(1).lower(), m.group(2))
    if m := re.search("^/([nN]\\w+)/?$", path):
        return _make_info("syosetu", m.group(1).lower(), None)
    return None


@register_extractor(["novel18.syosetu.com"])
def extract_novel18(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/([nN]\\w+)/(\\d+)/?$", path):
        return _make_info("syosetu18", m.group(1).lower(), m.group(2))
    if m := re.search("^/([nN]\\w+)/?$", path):
        return _make_info("syosetu18", m.group(1).lower(), None)
    return None


@register_extractor(["syosetu.org"])
def extract_syosetu(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/novel/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("syosetu_org", m.group(1), m.group(2))
    if m := re.search("^/novel/(\\d+)/?$", path):
        return _make_info("syosetu_org", m.group(1), None)
    return None


@register_extractor(["www.tianyabooks.com"])
def extract_tianyabooks(path: str, query: str) -> BookURLInfo | None:
    if m := re.match(r"^/([^/]+)/([^/]+)/(\d+)\.html$", path):
        category, book, chap_id = m.groups()
        return _make_info("tianyabooks", f"{category}-{book}", chap_id)
    if m := re.match(r"^/([^/]+)/([^/]+)/?$", path):
        category, book = m.groups()
        return _make_info("tianyabooks", f"{category}-{book}", None)
    return None


@register_extractor(["www.tongrenquan.org"])
def extract_tongrenquan(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/tongren/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("tongrenquan", m.group(1), m.group(2))
    if m := re.search("^/tongren/(\\d+)\\.html$", path):
        return _make_info("tongrenquan", m.group(1), None)
    return None


@register_extractor(["tongrenshe.cc"])
def extract_tongrenshe(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/tongren/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("tongrenshe", m.group(1), m.group(2))
    if m := re.search("^/tongren/(\\d+)\\.html$", path):
        return _make_info("tongrenshe", m.group(1), None)
    return None


@register_extractor(["www.trxs.cc"])
def extract_trxs(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/tongren/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("trxs", m.group(1), m.group(2))
    if m := re.search("^/tongren/(\\d+)\\.html$", path):
        return _make_info("trxs", m.group(1), None)
    return None


@register_extractor(
    [
        "www.ttkan.co",
        "cn.ttkan.co",
        "tw.ttkan.co",
        "www.wa01.com",
        "cn.wa01.com",
        "tw.wa01.com",
    ]
)
def extract_ttkan(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/novel/pagea/([^_]+)_(\\d+)\\.html$", path):
        return _make_info("ttkan", m.group(1), m.group(2))
    if m := re.search("^/novel/chapters/([^/]+)$", path):
        return _make_info("ttkan", m.group(1), None)
    return None


@register_extractor(["twkan.com"])
def extract_twkan(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/txt/(\\d+)/(\\d+)$", path):
        return _make_info("twkan", m.group(1), m.group(2))
    if m := re.search("^/book/(\\d+)(?:/index\\.html|\\.html)?$", path):
        return _make_info("twkan", m.group(1), None)
    return None


@register_extractor(["www.uaa.com"])
def extract_uaa(path: str, query: str) -> BookURLInfo | None:
    if path == "/novel/chapter" and (chapter_id := _query_param(query, "id", r"\d+")):
        return _make_info("uaa", None, chapter_id)
    if path == "/novel/intro" and (book_id := _query_param(query, "id", r"\d+")):
        return _make_info("uaa", book_id, None)
    return None


@register_extractor(["www.wanbengo.com"])
def extract_wanbengo(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("wanbengo", m.group(1), m.group(2))
    if m := re.search("^/(\\d+)/?$", path):
        return _make_info("wanbengo", m.group(1), None)
    return None


@register_extractor(["www.wenku8.net"])
def extract_wenku8(path: str, query: str) -> BookURLInfo | None:
    if m := re.match(r"^/book/(\d+)\.htm$", path):
        return _make_info("wenku8", m.group(1), None)
    if m := re.match(r"^/novel/\d+/(\d+)/(\d+)\.htm$", path):
        return _make_info("wenku8", m.group(1), m.group(2))
    if m := re.match(r"^/novel/\d+/(\d+)/", path):
        return _make_info("wenku8", m.group(1), None)
    return None


@register_extractor(["www.westnovel.com"])
def extract_westnovel(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/([a-z]+)/showinfo-(\\d+-\\d+-\\d+)\\.html$", path):
        return _make_info("westnovel_sub", None, m.group(2))
    if m := re.search("^/([a-z]+)/list/(\\d+)\\.html$", path):
        return _make_info("westnovel_sub", f"{m.group(1)}-list-{m.group(2)}", None)
    if m := re.search("^/([a-z]+)/([a-z0-9_]+)/(\\d+)\\.html$", path):
        return _make_info("westnovel", f"{m.group(1)}-{m.group(2)}", m.group(3))
    if m := re.search("^/([a-z]+)/([a-z0-9_]+)/?$", path):
        return _make_info("westnovel", f"{m.group(1)}-{m.group(2)}", None)
    return None


@register_extractor(["www.wxscs.com", "wxscs.com", "wxsck.com"])
def extract_wxscs(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/book/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("wxsck", m.group(1), m.group(2))
    if m := re.search("^/book/(\\d+)/?$", path):
        return _make_info("wxsck", m.group(1), None)
    return None


@register_extractor(["www.xiaoshuoge.info"])
def extract_xiaoshuoge(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/html/(\\d+)/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("xiaoshuoge", f"{m.group(1)}-{m.group(2)}", m.group(3))
    if m := re.search("^/html/(\\d+)/(\\d+)/?$", path):
        return _make_info("xiaoshuoge", f"{m.group(1)}-{m.group(2)}", None)
    return None


@register_extractor(["www.xiguashuwu.com"])
def extract_xiguashuwu(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/book/(\\d+)/(\\d+)(?:_\\d+)?\\.html$", path):
        return _make_info("xiguashuwu", m.group(1), m.group(2))
    if m := re.search("^/book/(\\d+)/", path):
        return _make_info("xiguashuwu", m.group(1), None)
    return None


@register_extractor(["m.xs63b.com"])
def extract_xs63b(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/([^/]+)/([^/]+)/(\\d+)\\.html$", path):
        return _make_info("xs63b", f"{m.group(1)}-{m.group(2)}", m.group(3))
    if m := re.search("^/([^/]+)/([^/]+)/?$", path):
        return _make_info("xs63b", f"{m.group(1)}-{m.group(2)}", None)
    return None


@register_extractor(["www.xshbook.com"])
def extract_xshbook(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/(\\d+)/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("xshbook", f"{m.group(1)}-{m.group(2)}", m.group(3))
    if m := re.search("^/(\\d+)/(\\d+)/?$", path):
        return _make_info("xshbook", f"{m.group(1)}-{m.group(2)}", None)
    return None


@register_extractor(["www.yamibo.com"])
def extract_yamibo(path: str, query: str) -> BookURLInfo | None:
    if path == "/novel/view-chapter" and (
        chapter_id := _query_param(query, "id", r"\d+")
    ):
        return _make_info("yamibo", None, chapter_id)
    if m := re.search("^/novel/(\\d+)", path):
        return _make_info("yamibo", m.group(1), None)
    return None


@register_extractor(
    ["www.yibige.org", "sg.yibige.org", "tw.yibige.org", "hk.yibige.org"]
)
def extract_yibige(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("yibige", m.group(1), m.group(2))
    if m := re.search("^/(\\d+)/", path):
        return _make_info("yibige", m.group(1), None)
    return None


@register_extractor(["www.yodu.org"])
def extract_yodu(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/book/(\\d+)/(\\d+)\\.html$", path):
        return _make_info("yodu", m.group(1), m.group(2))
    if m := re.search("^/book/(\\d+)/?$", path):
        return _make_info("yodu", m.group(1), None)
    return None


@register_extractor(["www.zhenhunxiaoshuo.com"])
def extract_zhenhunxiaoshuo(path: str, query: str) -> BookURLInfo | None:
    if m := re.search("^/(\\d+)\\.html$", path):
        return _make_info("zhenhunxiaoshuo", None, m.group(1))
    if m := re.search("^/([^/]+)/?$", path):
        return _make_info("zhenhunxiaoshuo", m.group(1), None)
    return None
