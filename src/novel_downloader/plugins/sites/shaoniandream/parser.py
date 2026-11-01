#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.shaoniandream.parser
---------------------------------------------------
"""

import base64
import json
import logging
import re
from typing import Any

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from lxml import html

from novel_downloader.plugins.base.parser import BaseParser
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import (
    BookInfoDict,
    ChapterDict,
    ChapterInfoDict,
    MediaResource,
    VolumeInfoDict,
)

logger = logging.getLogger(__name__)


@registrar.register_parser()
class ShaoniandreamParser(BaseParser):
    """
    Parser for 少年梦 book-info pages.
    """

    site_name: str = "shaoniandream"

    _RE_TAG_I = re.compile(r"<i[^>]*>.*?</i>", re.DOTALL)

    def parse_book_info(
        self,
        raw_pages: list[str],
        **kwargs: Any,
    ) -> BookInfoDict | None:
        if len(raw_pages) < 2:
            return None

        if not raw_pages[0].strip():
            return None

        info_tree = html.fromstring(raw_pages[0])
        try:
            catalog_data = json.loads(raw_pages[1])
        except (json.JSONDecodeError, TypeError):
            logger.warning("shaoniandream book_info: catalog page is not valid JSON")
            return None
        if not isinstance(catalog_data, dict):
            logger.warning("shaoniandream book_info: catalog JSON is not an object")
            return None

        data = catalog_data.get("data") or {}
        if not isinstance(data, dict):
            logger.warning("shaoniandream book_info: catalog data is not an object")
            return None
        readdir = data.get("readdir") or []
        if not isinstance(readdir, list):
            logger.warning("shaoniandream book_info: readdir is not a list")
            readdir = []

        # --- parse main info ---
        book_name = self._first_str(
            info_tree.xpath(
                '//div[@class="bookdetail-name"]/span[@class="title"]/text()'
            )
        )
        author = self._first_str(
            info_tree.xpath('//span[contains(@class,"penName")]//a/text()')
        )
        cover_url = self._first_str(
            info_tree.xpath('//div[@class="cover"]/img/@data-original')
        )
        update_time = self._first_str(
            info_tree.xpath('//div[@class="bookdetial-newchapter"]//span/text()'),
            replaces=[("● ", "")],
        )
        word_count = self._first_str(
            info_tree.xpath('//div[@class="font-list"]/span[1]/text()')
        )
        serial_status = self._first_str(
            info_tree.xpath('//div[@class="bookdetail-name"]/i/text()')
        )
        tags = info_tree.xpath('//div[@class="label-list"]/span/text()')
        summary = self._join_strs(
            info_tree.xpath('//div[@class="bookdetial-jianjie"]//text()')
        )

        # --- parse volumes & chapters ---
        volumes: list[VolumeInfoDict] = []
        for v in readdir:
            if not isinstance(v, dict):
                continue
            volume_name = v.get("title") or ""
            vol_list = v.get("list") or []
            if not isinstance(vol_list, list):
                vol_list = []
            chapters: list[ChapterInfoDict] = []
            for c in vol_list:
                if not isinstance(c, dict):
                    continue
                chapters.append(
                    {
                        "title": c.get("title") or "",
                        "url": c.get("url") or "",
                        "chapterId": str(c.get("id") or ""),
                        "accessible": "lock_fill" not in (c.get("class") or ""),
                    }
                )
            volumes.append(
                {
                    "volume_name": volume_name,
                    "volume_intro": v.get("miaoshu") or "",
                    "chapters": chapters,
                }
            )

        if not volumes:
            return None

        return {
            "book_name": book_name,
            "author": author,
            "cover_url": cover_url,
            "update_time": update_time,
            "word_count": word_count,
            "serial_status": serial_status,
            "tags": tags,
            "summary": summary,
            "volumes": volumes,
            "extra": {},
        }

    def parse_chapter_content(
        self,
        raw_pages: list[str],
        chapter_id: str,
        **kwargs: Any,
    ) -> ChapterDict | None:
        if not raw_pages:
            return None

        try:
            raw_json = json.loads(raw_pages[0])
        except (json.JSONDecodeError, TypeError):
            logger.warning("shaoniandream chapter: response is not valid JSON")
            return None
        if not isinstance(raw_json, dict):
            logger.warning("shaoniandream chapter: response is not a JSON object")
            return None
        if raw_json.get("status") != 1:
            raise ValueError("Invalid chapter response")

        data = raw_json.get("data")
        if not isinstance(data, dict):
            logger.warning("shaoniandream chapter: data missing or not an object")
            return None
        title = data.get("title") or ""
        img_prefix = data.get("imgPrefix") or ""
        encryt_keys = data.get("encryt_keys") or []
        show_content = data.get("show_content") or []
        chapter_pics = data.get("chapterpic") or []

        # --- decode AES key/iv ---
        if not isinstance(encryt_keys, list) or len(encryt_keys) < 2:
            logger.warning("shaoniandream chapter: encryt_keys missing or incomplete")
            return None
        try:
            key = base64.b64decode(encryt_keys[0])
            iv = base64.b64decode(encryt_keys[1])
            AES.new(key, AES.MODE_CBC, iv)
        except (ValueError, TypeError):
            logger.warning("shaoniandream chapter: failed to decode AES key/iv")
            return None

        def decrypt(cipher_b64: str) -> str:
            cipher_bytes = base64.b64decode(cipher_b64)
            cipher = AES.new(key, AES.MODE_CBC, iv)
            decrypted = cipher.decrypt(cipher_bytes)
            try:
                res: str = unpad(decrypted, AES.block_size).decode("utf-8")
            except ValueError:
                res = decrypted.decode("utf-8", errors="ignore")
            return res

        paragraphs: list[str] = []
        for p in show_content if isinstance(show_content, list) else []:
            if not isinstance(p, dict):
                continue
            para_enc = p.get("content")
            if not para_enc:
                continue
            try:
                text = self._RE_TAG_I.sub("", decrypt(para_enc)).strip()
            except (ValueError, TypeError):
                logger.warning("shaoniandream chapter: failed to decrypt paragraph")
                continue
            if text:
                paragraphs.append(text)

        postscript = ""
        miaoshu_enc = data.get("miaoshu")
        if miaoshu_enc:
            try:
                postscript = self._RE_TAG_I.sub("", decrypt(miaoshu_enc)).strip()
                if postscript:
                    paragraphs.append(postscript)
            except Exception:
                pass

        resources: list[MediaResource] = []
        curr_paragraph_idx = len(paragraphs)

        if isinstance(chapter_pics, list):
            for pic in chapter_pics:
                if not isinstance(pic, dict):
                    continue
                url = pic.get("url")
                if not url:
                    continue
                full_url = img_prefix + url
                resources.append(
                    {
                        "type": "image",
                        "paragraph_index": curr_paragraph_idx,
                        "url": full_url,
                    }
                )

        if not (paragraphs or resources):
            return None

        content = "\n".join(paragraphs)

        return {
            "id": chapter_id,
            "title": title,
            "content": content,
            "extra": {
                "site": self.site_name,
                "resources": resources,
            },
        }
