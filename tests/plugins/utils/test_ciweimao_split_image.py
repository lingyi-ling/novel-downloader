#!/usr/bin/env python3
"""Offline unit tests for :mod:`novel_downloader.plugins.utils.ciweimao.image`.

``split_image`` is pure NumPy array slicing driven by Tsukkomi metadata, so
the tests feed small synthetic RGB arrays and inspect the produced slices and
block ordering.  No real chapter images are involved.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from novel_downloader.libs import image_utils
from novel_downloader.plugins.utils.ciweimao.image import split_image


def _blank(h: int, w: int = 8, value: int = 10) -> np.ndarray:
    return np.full((h, w, 3), value, dtype=np.uint8)


def _meta(
    tsukkomi: dict[str, Any],
    image_maps: dict[str, Any] | None = None,
    height: int = 10,
) -> dict[str, Any]:
    return {
        "tsukkomi_list": tsukkomi,
        "imageInfoMaps": image_maps or {},
        "height": height,
    }


# --------------------------------------------------------------------------- #
# text lines
# --------------------------------------------------------------------------- #


def test_two_text_lines_produce_two_padded_slices() -> None:
    img = _blank(40)
    result = split_image(img, _meta({"1": [0, 1]}))

    # font_height=10 -> slice 15 rows + 2 padding rows above and below.
    assert len(result.images) == 2
    for slc in result.images:
        assert slc.shape == (19, 8, 3)

    assert result.blocks == [
        {"type": "paragraph", "image_idxs": [0, 1]},
    ]


def test_padding_rows_use_background_colour() -> None:
    img = _blank(20)
    result = split_image(img, _meta({"1": [0, 0]}), background=(1, 2, 3))
    slc = result.images[0]
    assert (slc[0] == np.array([1, 2, 3], dtype=np.uint8)).all()
    assert (slc[-1] == np.array([1, 2, 3], dtype=np.uint8)).all()
    # Inner rows come from the source image.
    assert (slc[2:-2] == 10).all()


def test_paragraphs_are_processed_in_numeric_key_order() -> None:
    img = _blank(200)
    # Dict order deliberately reversed vs numeric order.
    result = split_image(
        img,
        _meta({"10": [4, 4], "2": [0, 0]}),
    )
    assert [b["type"] for b in result.blocks] == ["paragraph", "paragraph"]
    paras = [b for b in result.blocks if b["type"] == "paragraph"]
    assert [b["image_idxs"] for b in paras] == [[0], [1]]

    # The first slice must come from the top of the image (paragraph "2").
    src = img[:15]
    assert np.array_equal(result.images[0][2:-2], src)


# --------------------------------------------------------------------------- #
# image lines
# --------------------------------------------------------------------------- #


def test_image_line_emits_image_block_and_advances_cursor() -> None:
    img = _blank(200)
    meta = _meta(
        {"1": [0, 1]},
        image_maps={"0": {"path": "https://img.example/a.png", "height": 100}},
    )
    result = split_image(img, meta)

    assert result.blocks[0] == {"type": "image", "url": "https://img.example/a.png"}
    assert result.blocks[1] == {"type": "paragraph", "image_idxs": [0]}

    # now_y = 0 + 100 + 1.2*10 = 112, so the text slice starts at row 112.
    text_slice = result.images[0]
    assert np.array_equal(text_slice[2:-2], img[112:127])


def test_image_only_chapter_has_no_paragraph_blocks() -> None:
    img = _blank(40)
    meta = _meta(
        {"1": [0, 0]},
        image_maps={"0": {"path": "u://x.jpg", "height": 20}},
    )
    result = split_image(img, meta)
    assert result.images == []
    assert result.blocks == [{"type": "image", "url": "u://x.jpg"}]


# --------------------------------------------------------------------------- #
# clamping / degenerate geometry
# --------------------------------------------------------------------------- #


def test_text_lines_clamp_to_image_height_and_break() -> None:
    img = _blank(6)
    result = split_image(img, _meta({"1": [0, 3]}))

    # Line 0 slices rows 0..6 (clamped), then line 1 starts beyond the image
    # and start_y == end_y, so the loop breaks.
    assert len(result.images) == 1
    assert result.images[0].shape == (6 + 4, 8, 3)
    assert result.blocks == [{"type": "paragraph", "image_idxs": [0]}]


def test_zero_height_image_yields_nothing() -> None:
    img = _blank(0)
    result = split_image(img, _meta({"1": [0, 0]}))
    assert result.images == []
    assert result.blocks == []


# --------------------------------------------------------------------------- #
# watermark filtering
# --------------------------------------------------------------------------- #


def test_remove_watermark_runs_filter_on_each_slice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    real_filter = image_utils.filter_gray_watermark

    def _spy(
        img: np.ndarray,
        threshold: int = 200,
        background: tuple[int, int, int] = (255, 255, 255),
    ) -> np.ndarray:
        calls.append(1)
        return real_filter(img, threshold=threshold, background=background)

    monkeypatch.setattr(image_utils, "filter_gray_watermark", _spy)

    img = _blank(40)
    result = split_image(img, _meta({"1": [0, 1]}), remove_watermark=True)

    assert sum(calls) == 2
    assert len(result.images) == 2


def test_watermark_filter_not_called_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: object, **kwargs: object) -> np.ndarray:
        raise AssertionError("filter must not run when remove_watermark=False")

    monkeypatch.setattr(image_utils, "filter_gray_watermark", _boom)
    result = split_image(_blank(40), _meta({"1": [0, 0]}))
    assert len(result.images) == 1
