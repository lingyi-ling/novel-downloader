#!/usr/bin/env python3
"""Offline unit tests for the Ciweimao chapter parser mixin.

``my_decrypt`` and every image helper are monkeypatched with in-memory
doubles, so the tests never touch the network and never decode real images.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray

from novel_downloader.libs import image_utils
from novel_downloader.plugins.utils.ciweimao import chapter as chapter_mod
from novel_downloader.plugins.utils.ciweimao.chapter import CiweimaoChapterMixin
from novel_downloader.plugins.utils.ciweimao.image import SplitResult
from novel_downloader.schemas import BookInfoDict, ChapterDict


class FakeParser(CiweimaoChapterMixin):
    """Minimal context satisfying the mixin's attribute protocol."""

    site_name: str = "ciweimao"
    _enable_ocr: bool = False
    _batch_size: int = 2
    _use_truncation: bool = False
    _remove_watermark: bool = False
    _cut_mode: str = "none"
    _cache_dir: Path = Path("cache")
    _CHAPTER_BACKGROUND: tuple[int, int, int] = (255, 255, 255)
    _PAGE_LINE_LIMIT: int = 2

    def __init__(self) -> None:
        self.ocr_calls: list[list[NDArray[np.uint8]]] = []

    def parse_book_info(
        self, raw_pages: list[str], **kwargs: Any
    ) -> BookInfoDict | None:
        raise AssertionError("not used in these tests")

    def parse_chapter_content(
        self, raw_pages: list[str], chapter_id: str, **kwargs: Any
    ) -> ChapterDict | None:
        raise AssertionError("not used in these tests")

    # ---- remaining _ParserContext protocol members ----

    @property
    def ocr_model(self) -> Any:
        raise AssertionError("ocr model not used offline")

    def _is_ad_line(self, line: str) -> bool:
        return False

    @classmethod
    def _norm_space(cls, s: str, c: str = " ") -> str:
        return " ".join(s.split())

    @staticmethod
    def _first_str(xs: list[str], replaces: list[tuple[str, str]] | None = None) -> str:
        return xs[0].strip() if xs else ""

    @staticmethod
    def _join_strs(xs: list[str], replaces: list[tuple[str, str]] | None = None) -> str:
        return "".join(x.strip() for x in xs)

    def _extract_text_from_image(
        self,
        images: list[NDArray[np.uint8]],
        batch_size: int = 1,
    ) -> list[tuple[str, float]]:
        self.ocr_calls.append(images)
        return [(f"text{i}", 1.0) for i in range(len(images))]


_VALID_B64 = base64.b64encode(b"fake-image").decode("ascii")


def _detail(content: Any, keys: Any) -> str:
    return json.dumps({"chapter_content": content, "encryt_keys": keys})


# --------------------------------------------------------------------------- #
# _parse_text_chapter
# --------------------------------------------------------------------------- #


def test_parse_text_chapter_extracts_paragraphs_and_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        chapter_mod,
        "my_decrypt",
        lambda content, keys, access_key: (
            "<html><body>"
            "<p><img src='//img.example/1.png' alt='pic one'></p>"
            "<p>First para</p>"
            "<p>Second<span>junk</span></p>"
            "<p><img src=''></p>"
            "</body></html>"
        ),
    )

    parser = FakeParser()
    paragraphs, resources = parser._parse_text_chapter(
        _detail("ENC", ["k1"]), json.dumps({"chapter_access_key": "AK"})
    )

    # Spans are stripped together with their content.
    assert paragraphs == ["First para", "Second"]
    # The empty-src image is skipped; the protocol image gets https: prefix.
    assert len(resources) == 1
    res = resources[0]
    assert res["type"] == "image"
    assert res["url"] == "https://img.example/1.png"
    assert res["alt"] == "pic one"
    # Image paragraph precedes any text paragraph -> paragraph_index 0.
    assert res["paragraph_index"] == 0


def test_parse_text_chapter_decrypt_called_with_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def _fake_decrypt(content: str, keys: list[str], access_key: str) -> str:
        seen["content"] = content
        seen["keys"] = keys
        seen["access_key"] = access_key
        return "<html><body><p>body</p></body></html>"

    monkeypatch.setattr(chapter_mod, "my_decrypt", _fake_decrypt)
    parser = FakeParser()
    paragraphs, resources = parser._parse_text_chapter(
        _detail("CIPHER", ["a", "b"]), json.dumps({"chapter_access_key": "KEY"})
    )

    assert seen == {"content": "CIPHER", "keys": ["a", "b"], "access_key": "KEY"}
    assert paragraphs == ["body"]
    assert resources == []


def test_parse_text_chapter_missing_fields_returns_empty(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom(content: str, keys: list[str], access_key: str) -> str:
        raise AssertionError("decrypt must not run without all fields")

    monkeypatch.setattr(chapter_mod, "my_decrypt", _boom)
    parser = FakeParser()

    with caplog.at_level(logging.WARNING):
        assert parser._parse_text_chapter(_detail(None, ["k"]), "{}") == ([], [])
        assert parser._parse_text_chapter(_detail("C", []), "{}") == ([], [])
        assert parser._parse_text_chapter(_detail("C", ["k"]), "{}") == ([], [])
    assert len(caplog.records) == 3


def test_parse_text_chapter_keeps_https_src_and_no_alt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        chapter_mod,
        "my_decrypt",
        lambda content, keys, access_key: (
            "<p><img src='https://cdn.example/2.png'></p><p>txt</p>"
        ),
    )
    parser = FakeParser()
    _, resources = parser._parse_text_chapter(
        _detail("C", ["k"]), '{"chapter_access_key":"A"}'
    )
    assert len(resources) == 1
    assert resources[0]["url"] == "https://cdn.example/2.png"
    assert resources[0]["alt"] == ""
    assert resources[0]["paragraph_index"] == 0


# --------------------------------------------------------------------------- #
# _parse_image_chapter
# --------------------------------------------------------------------------- #


def _tiny_arr(rows: int = 4, cols: int = 3, value: int = 7) -> np.ndarray:
    return np.full((rows, cols, 3), value, dtype=np.uint8)


def _split_result(images: list[Any], blocks: list[Any]) -> SplitResult:
    return SplitResult(images=images, blocks=blocks)


@pytest.fixture()
def patched_image_helpers(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Patch decoding, splitting and concat with deterministic doubles.

    Returns a list whose length tracks how often the vertical concat helper
    ran (each call corresponds to one flushed image page/paragraph).
    """
    monkeypatch.setattr(
        image_utils,
        "load_image_array_bytes",
        lambda data, white_bg=False: _tiny_arr(),
    )

    concat_calls: list[int] = []

    def _fake_concat(slices: list[Any], format: str = "JPEG") -> bytes:
        concat_calls.append(len(slices))
        return b"jpeg-bytes"

    monkeypatch.setattr(image_utils, "concat_image_slices_vertical", _fake_concat)

    def _fake_split(
        img_arr: Any,
        image_tsukkomi_list: Any,
        background: tuple[int, int, int] = (255, 255, 255),
        remove_watermark: bool = False,
    ) -> SplitResult:
        return _split_result(
            [_tiny_arr(1, value=i + 1) for i in range(4)],
            [
                {"type": "paragraph", "image_idxs": [0, 1, 2]},
                {"type": "image", "url": "https://img.example/mid.png"},
                {"type": "paragraph", "image_idxs": [3]},
            ],
        )

    monkeypatch.setattr(
        "novel_downloader.plugins.utils.ciweimao.image.split_image", _fake_split
    )
    return concat_calls


def test_parse_image_chapter_shortcut_without_cut_or_ocr() -> None:
    parser = FakeParser()  # _enable_ocr=False, _cut_mode="none"
    paragraphs, resources = parser._parse_image_chapter(_VALID_B64, "{}")

    assert paragraphs == []
    assert len(resources) == 1
    assert resources[0]["base64"] == _VALID_B64
    assert resources[0]["mime"] == "image/jpeg"
    assert resources[0]["paragraph_index"] == 0


def test_parse_image_chapter_paragraph_cut_mode(
    patched_image_helpers: list[int],
) -> None:
    parser = FakeParser()
    parser._cut_mode = "paragraph"
    paragraphs, resources = parser._parse_image_chapter(_VALID_B64, "{}")

    assert paragraphs == []
    # paragraph -> concat; image -> url resource; second paragraph -> concat.
    assert len(resources) == 3
    assert patched_image_helpers == [3, 1]  # whole paragraph per concat
    assert resources[1] == {
        "type": "image",
        "paragraph_index": 0,
        "url": "https://img.example/mid.png",
    }
    assert "base64" in resources[0]
    assert "base64" in resources[2]
    assert all(r["paragraph_index"] == 0 for r in resources)


def test_parse_image_chapter_page_cut_mode_flushes_on_limit(
    patched_image_helpers: list[int],
) -> None:
    parser = FakeParser()
    parser._cut_mode = "page"
    # _PAGE_LINE_LIMIT = 2: the first paragraph has 3 lines -> pages of 2+1.
    _, resources = parser._parse_image_chapter(_VALID_B64, "{}")

    has_url = [r.get("url") is not None for r in resources]
    # page1 (lines 0,1) flushes when line 3 arrives; page2 (line 2) flushes
    # before the inline image; then the image; then page3 (line 3).
    assert has_url == [False, False, True, False]
    assert resources[2].get("url") == "https://img.example/mid.png"
    assert len(resources) == 4
    assert patched_image_helpers == [2, 1, 1]


def test_parse_image_chapter_ocr_mode_builds_paragraphs(
    patched_image_helpers: list[int],
) -> None:
    parser = FakeParser()
    parser._enable_ocr = True
    paragraphs, resources = parser._parse_image_chapter(_VALID_B64, "{}")

    assert paragraphs == ["text0text1text2", "text3"]
    assert resources == [
        {"type": "image", "paragraph_index": 1, "url": "https://img.example/mid.png"}
    ]
    assert len(parser.ocr_calls) == 1
    assert len(parser.ocr_calls[0]) == 4
    assert patched_image_helpers == []  # OCR mode never concatenates


def test_parse_image_chapter_ocr_failure_falls_back_to_cut_mode(
    patched_image_helpers: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _explode(
        images: list[NDArray[np.uint8]], batch_size: int = 1
    ) -> list[tuple[str, float]]:
        raise RuntimeError("ocr engine missing")

    monkeypatch.setattr(FakeParser, "_extract_text_from_image", _explode)

    parser = FakeParser()
    parser._enable_ocr = True
    parser._cut_mode = "paragraph"
    paragraphs, resources = parser._parse_image_chapter(_VALID_B64, "{}")

    assert paragraphs == []
    # Falls back to paragraph slicing: 2 concats + 1 url resource.
    assert len(resources) == 3
    assert patched_image_helpers == [3, 1]  # whole paragraph per concat


def test_parse_image_chapter_decode_error_returns_original_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(data: bytes, white_bg: bool = False) -> np.ndarray:
        raise ValueError("bad image")

    monkeypatch.setattr(image_utils, "load_image_array_bytes", _boom)
    parser = FakeParser()
    parser._cut_mode = "paragraph"

    paragraphs, resources = parser._parse_image_chapter("ORIG", "{}")
    assert paragraphs == []
    assert resources[0]["base64"] == "ORIG"
    assert resources[0]["mime"] == "image/jpeg"


def test_parse_image_chapter_decodes_base64_payload(
    patched_image_helpers: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bytes] = []

    def _load(data: bytes, white_bg: bool = False) -> np.ndarray:
        seen.append(data)
        return _tiny_arr()

    monkeypatch.setattr(image_utils, "load_image_array_bytes", _load)
    payload = base64.b64encode(b"fake-jpeg").decode("ascii")
    meta = json.dumps({"tsukkomi_list": {}, "imageInfoMaps": {}, "height": 10})

    parser = FakeParser()
    parser._cut_mode = "page"
    parser._parse_image_chapter(payload, meta)

    assert seen == [b"fake-jpeg"]
