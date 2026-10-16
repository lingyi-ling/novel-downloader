#!/usr/bin/env python3
import base64
import hashlib
import json

from Crypto.Cipher import DES

from novel_downloader.plugins.sites.qimao.parser import QimaoParser
from novel_downloader.plugins.sites.qimao.searcher import QimaoSearcher
from novel_downloader.schemas import SearchResult

_KEY_STR = "0123456789abcdefghijklmnopqrstuv"


def _encrypt(body: str) -> str:
    """Build a VIP-style ``chapterData`` payload from plain HTML."""
    key = hashlib.md5(_KEY_STR.encode()).hexdigest()[:8].encode()
    data = body.encode()
    pad = 8 - len(data) % 8
    data += bytes([pad]) * pad
    ciphertext = DES.new(key, DES.MODE_ECB).encrypt(data)
    inner = base64.b64encode(ciphertext).decode()
    return base64.b64encode(inner.encode()).decode() + _KEY_STR


def _detail_pages() -> list[str]:
    detail = {
        "data": {
            "book_detail": {
                "book_id": 1690157,
                "title": "武破苍穹",
                "author": "破苍天",
                "words_num": "54.96",
                "is_over": "1",
                "image_link": "https://example.com/cover.jpg",
                "update_time": "2023-04-20 18:22:08",
                "category_1_name": "玄幻奇幻",
                "category_2_name": "东方玄幻",
                "latest_chapter_title": "第四十五章　神奇小兽（大结局）",
            }
        }
    }
    intro = {"data": {"intro": "天道沦丧，魔道永昌。"}}
    catalog = {
        "data": {
            "chapters": [
                {"id": "16812033970001", "title": "第一章　稚子之心1"},
                {"id": "16812033990014", "title": "第十四章　蒙混过关！"},
            ]
        }
    }
    return [
        json.dumps(detail, ensure_ascii=False),
        json.dumps(intro, ensure_ascii=False),
        json.dumps(catalog, ensure_ascii=False),
    ]


def test_decrypt_vip_payload() -> None:
    body = "<p>自家事自家知。</p><p>之后有人过关了。</p>"
    assert QimaoParser._decrypt(_encrypt(body)) == body


def test_decrypt_invalid_payload_returns_empty() -> None:
    assert QimaoParser._decrypt("!!!not-a-valid-payload!!!") == ""


def test_parse_book_info() -> None:
    parser = QimaoParser.__new__(QimaoParser)
    info = parser.parse_book_info(_detail_pages())
    assert info is not None
    assert info["book_name"] == "武破苍穹"
    assert info["author"] == "破苍天"
    assert info["word_count"] == "54.96万字"
    assert info["serial_status"] == "完结"
    assert info["tags"] == ["玄幻奇幻", "东方玄幻"]
    chapters = info["volumes"][0]["chapters"]
    assert [c["chapterId"] for c in chapters] == ["16812033970001", "16812033990014"]
    assert parser._chapter_titles["16812033990014"] == "第十四章　蒙混过关！"


def test_parse_book_info_rejects_short_pages() -> None:
    parser = QimaoParser.__new__(QimaoParser)
    assert parser.parse_book_info(["{}"]) is None


def test_parse_free_chapter_content() -> None:
    parser = QimaoParser.__new__(QimaoParser)
    page = (
        '<script>window.__NUXT__={chapterData:"'
        "\\u003Cp\\u003E黄昏渐落。\\u003C/p\\u003E\\u003Cp\\u003E夜幕拉下。\\u003C/p\\u003E"
        '"}</script>'
    )
    chapter = parser.parse_chapter_content([page], "16812033970001")
    assert chapter is not None
    assert chapter["content"] == "黄昏渐落。\n夜幕拉下。"


def test_parse_vip_chapter_content() -> None:
    parser = QimaoParser.__new__(QimaoParser)
    parser.parse_book_info(_detail_pages())
    payload = _encrypt("<p>蒙混过关。</p>")
    page = f'<script>window.__NUXT__={{chapterData:"{payload}"}}</script>'
    chapter = parser.parse_chapter_content([page], "16812033990014")
    assert chapter is not None
    assert chapter["content"] == "蒙混过关。"
    assert chapter["title"] == "第十四章　蒙混过关！"


def test_parse_chapter_content_without_payload() -> None:
    parser = QimaoParser.__new__(QimaoParser)
    assert parser.parse_chapter_content(["<html></html>"], "x") is None


def test_searcher_parse_html() -> None:
    searcher = QimaoSearcher.__new__(QimaoSearcher)
    payload = {
        "data": {
            "search_list": [
                {
                    "book_id": 1784900,
                    "title": "斗破苍穹",
                    "author": "天蚕土豆",
                    "image_link": "https://example.com/1.jpg",
                    "words_num": "535.34万字",
                    "latest_chapter_title": "第1623章　大结局",
                    "update_time": "2024-01-24 09:33:57",
                    "read_url": "https://www.qimao.com/shuku/1784900/",
                }
            ]
        }
    }
    results = searcher._parse_html(json.dumps(payload, ensure_ascii=False))
    assert results == [
        SearchResult(
            site="qimao",
            book_id="1784900",
            book_url="https://www.qimao.com/shuku/1784900/",
            cover_url="https://example.com/1.jpg",
            title="斗破苍穹",
            author="天蚕土豆",
            latest_chapter="第1623章　大结局",
            update_date="2024-01-24 09:33:57",
            word_count="535.34万字",
            priority=5,
        )
    ]


def test_searcher_parse_html_handles_garbage() -> None:
    searcher = QimaoSearcher.__new__(QimaoSearcher)
    assert searcher._parse_html("<<<not json>>>") == []
    assert searcher._parse_html("") == []


def test_searcher_respects_limit() -> None:
    searcher = QimaoSearcher.__new__(QimaoSearcher)
    items = [{"book_id": str(i), "title": f"书{i}"} for i in range(5)]
    payload = json.dumps({"data": {"search_list": items}})
    assert len(searcher._parse_html(payload, limit=2)) == 2
