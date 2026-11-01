#!/usr/bin/env python3
"""
Real-network smoke tests for a small sample of site parsers.

Each test fetches a live book-detail page, feeds it to the parser, and
asserts the parse neither crashes nor yields an empty title. All network
failures (and site-layout changes that break link discovery) skip the
test instead of failing, so these can run in offline environments.
"""

import re
from typing import Any

import pytest

from novel_downloader.plugins.sites.biquge345.parser import Biquge345Parser
from novel_downloader.plugins.sites.shuhaige.parser import ShuhaigeParser
from novel_downloader.plugins.sites.trxs.parser import TrxsParser
from novel_downloader.schemas import ParserConfig

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}


def _get(url: str) -> str:
    import requests

    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15)
    except requests.RequestException as e:  # network down / blocked / timeout
        pytest.skip(f"network unavailable: {type(e).__name__}")
    if resp.status_code != 200:
        pytest.skip(f"non-200 response: {resp.status_code}")
    # Prefer the charset declared in the page over requests' guess.
    m = re.search(rb"charset=[\"']?([\w-]+)", resp.content[:4000])
    if m:
        try:
            return resp.content.decode(m.group(1).decode("ascii"), errors="replace")
        except (LookupError, UnicodeDecodeError):
            pass
    return resp.text


def _first_link(html: str, pattern: str) -> str:
    m = re.search(pattern, html)
    if not m:
        pytest.skip("site layout changed: no book link found")
    return m.group(1)


@pytest.fixture()
def cfg(tmp_path: Any) -> ParserConfig:
    return ParserConfig(cache_dir=str(tmp_path))


def test_trxs_real_book_info(cfg: ParserConfig) -> None:
    home = _get("https://www.trxs.cc")
    href = _first_link(home, r'href="(/tongren/\d+\.html)"')
    page = _get("https://www.trxs.cc" + href)
    info = TrxsParser(cfg).parse_book_info([page])
    assert info is not None, "trxs parser returned None for a live book page"
    assert info["book_name"], "trxs parser produced an empty title"
    assert info["volumes"][0]["chapters"]


def test_shuhaige_real_book_info(cfg: ParserConfig) -> None:
    listing = _get("https://www.shuhaige.net/all/2.html")
    href = _first_link(listing, r'href="(/\d+/)"')
    page = _get("https://www.shuhaige.net" + href)
    info = ShuhaigeParser(cfg).parse_book_info([page])
    assert info is not None, "shuhaige parser returned None for a live book page"
    assert info["book_name"], "shuhaige parser produced an empty title"
    assert info["volumes"][0]["chapters"]


def test_biquge345_real_book_info(cfg: ParserConfig) -> None:
    home = _get("https://www.biquge345.com")
    href = _first_link(home, r'href="(/book/\d+/)"')
    page = _get("https://www.biquge345.com" + href)
    info = Biquge345Parser(cfg).parse_book_info([page])
    assert info is not None, "biquge345 parser returned None for a live book page"
    assert info["book_name"], "biquge345 parser produced an empty title"
    assert info["volumes"][0]["chapters"]
