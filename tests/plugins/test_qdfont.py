"""YuewenQDFontMixin: mapping cache IO, download fallback, decode orchestration."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.plugins.utils.yuewen.qdfont import YuewenQDFontMixin

QDFONT_MODULE = "novel_downloader.plugins.utils.yuewen.qdfont"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _require_font_deps() -> None:
    pytest.importorskip("numpy")
    pytest.importorskip("fontTools")
    pytest.importorskip("PIL")


def _build_ttf(chars: list[str]) -> bytes:
    """
    Build a minimal deterministic TTF whose cmap covers exactly *chars*.

    Every glyph is the same rectangle, which is enough for PIL rendering
    (the OCR model itself is always stubbed out in these tests).
    """
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    fb = FontBuilder(unitsPerEm=1000, isTTF=True)
    glyph_names = [".notdef"] + [f"g{ord(c)}" for c in chars]
    fb.setupGlyphOrder(glyph_names)
    fb.setupCharacterMap({ord(c): f"g{ord(c)}" for c in chars})

    glyphs = {}
    for name in glyph_names:
        pen = TTGlyphPen(None)
        if name != ".notdef":
            pen.moveTo((50, 0))
            pen.lineTo((50, 700))
            pen.lineTo((450, 700))
            pen.lineTo((450, 0))
            pen.closePath()
        glyphs[name] = pen.glyph()
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics(dict.fromkeys(glyph_names, (500, 0)))
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupOS2(
        sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200
    )
    fb.setupNameTable({"familyName": "QDFontTest", "styleName": "Regular"})
    fb.setupPost()

    buf = io.BytesIO()
    fb.save(buf)
    return buf.getvalue()


class StubParser(YuewenQDFontMixin):
    """
    Minimal parser context for ``_decode_qdfont`` orchestration tests.

    ``_load_mapping_cache`` is the real implementation (backed by files in
    ``cache_dir``); the fixed-font loader and the font mapping builder are
    stubbed so no network or OCR is involved.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        fixed_font_bytes: bytes = b"stub-fixed-font",
        build_additions: dict[str, str] | None = None,
        batch_size: int = 8,
    ) -> None:
        self._cache_dir = cache_dir
        self._batch_size = batch_size
        self._fixed_font_bytes = fixed_font_bytes
        self._build_additions = build_additions or {}
        self.font_calls: list[tuple[str, Path]] = []
        self.build_calls: list[dict[str, Any]] = []

    def _load_or_download_fixed_font(self, url: str, dest_path: Path) -> bytes:
        self.font_calls.append((url, dest_path))
        return self._fixed_font_bytes

    def _build_font_mapping(
        self,
        *,
        fixed_font_bytes: bytes,
        random_font_bytes: bytes,
        direct_chars: set[str],
        reflected_chars: set[str],
        existing_map: dict[str, str],
        batch_size: int = 32,
    ) -> dict[str, str]:
        self.build_calls.append(
            {
                "fixed_font_bytes": fixed_font_bytes,
                "random_font_bytes": random_font_bytes,
                "direct_chars": set(direct_chars),
                "reflected_chars": set(reflected_chars),
                "existing_map": dict(existing_map),
                "batch_size": batch_size,
            }
        )
        # Mimic the real flow: seeded entries win, stub additions are new.
        mapping = {
            ch: existing_map[ch]
            for ch in (direct_chars | reflected_chars)
            if ch in existing_map
        }
        for ch, real in self._build_additions.items():
            if ch in (direct_chars | reflected_chars):
                mapping[ch] = real
                existing_map[ch] = real
        return mapping


class _FakeResp:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(self, content: bytes = b"", *, status_error: Exception | None = None):
        self.content = content
        self._status_error = status_error

    def raise_for_status(self) -> None:
        if self._status_error is not None:
            raise self._status_error


class OcrStubParser(YuewenQDFontMixin):
    """
    Parser context exercising the REAL ``_build_font_mapping`` with a
    stubbed OCR backend (``_extract_text_from_image``).
    """

    def __init__(
        self,
        *,
        ocr_result: list[tuple[str, float]] | None = None,
        ocr_error: Exception | None = None,
        batch_size: int = 8,
    ) -> None:
        self._batch_size = batch_size
        self._ocr_result = ocr_result
        self._ocr_error = ocr_error
        self.ocr_calls: list[dict[str, Any]] = []

    def _extract_text_from_image(
        self, images: list[Any], batch_size: int = 1
    ) -> list[tuple[str, float]]:
        self.ocr_calls.append({"images": list(images), "batch_size": batch_size})
        if self._ocr_error is not None:
            raise self._ocr_error
        return list(self._ocr_result or [])


# ---------------------------------------------------------------------------
# _load_mapping_cache
# ---------------------------------------------------------------------------


def test_load_mapping_cache_missing_file(tmp_path: Path) -> None:
    assert YuewenQDFontMixin._load_mapping_cache(tmp_path / "absent.json") == {}


def test_load_mapping_cache_valid_dict(tmp_path: Path) -> None:
    cache = tmp_path / "map.json"
    cache.write_text(json.dumps({"a": "甲", "好": "好"}), encoding="utf-8")
    assert YuewenQDFontMixin._load_mapping_cache(cache) == {"a": "甲", "好": "好"}


def test_load_mapping_cache_non_dict_json(tmp_path: Path) -> None:
    cache = tmp_path / "map.json"
    cache.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert YuewenQDFontMixin._load_mapping_cache(cache) == {}


def test_load_mapping_cache_invalid_json(tmp_path: Path) -> None:
    cache = tmp_path / "map.json"
    cache.write_text("{not json", encoding="utf-8")
    assert YuewenQDFontMixin._load_mapping_cache(cache) == {}


def test_load_mapping_cache_empty_file(tmp_path: Path) -> None:
    cache = tmp_path / "map.json"
    cache.write_text("", encoding="utf-8")
    assert YuewenQDFontMixin._load_mapping_cache(cache) == {}


# ---------------------------------------------------------------------------
# _load_or_download_fixed_font
# ---------------------------------------------------------------------------


def test_load_fixed_font_uses_cache_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "fixed_fonts" / "abc.woff2"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"cached-font-bytes")

    def fail_get(url: str, **kwargs: Any) -> Any:
        raise AssertionError("network must not be hit when the font is cached")

    monkeypatch.setattr(f"{QDFONT_MODULE}.requests.get", fail_get)

    result = YuewenQDFontMixin._load_or_download_fixed_font(
        "https://example.com/abc.woff2", dest
    )
    assert result == b"cached-font-bytes"


def test_load_fixed_font_downloads_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "fixed_fonts" / "abc.woff2"

    def fake_get(url: str, **kwargs: Any) -> Any:
        return _FakeResp(b"downloaded-bytes")

    monkeypatch.setattr(f"{QDFONT_MODULE}.requests.get", fake_get)

    result = YuewenQDFontMixin._load_or_download_fixed_font(
        "https://example.com/abc.woff2", dest
    )
    assert result == b"downloaded-bytes"
    assert dest.read_bytes() == b"downloaded-bytes"


def test_load_fixed_font_download_error_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "fixed_fonts" / "abc.woff2"

    def fake_get(url: str, **kwargs: Any) -> Any:
        return _FakeResp(status_error=RuntimeError("404"))

    monkeypatch.setattr(f"{QDFONT_MODULE}.requests.get", fake_get)

    with pytest.raises(RuntimeError, match="404"):
        YuewenQDFontMixin._load_or_download_fixed_font(
            "https://example.com/abc.woff2", dest
        )


def test_load_fixed_font_falls_back_when_cache_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "fixed_fonts" / "abc.woff2"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"corrupt")

    real_read_bytes = Path.read_bytes

    def broken_read_bytes(self: Path) -> bytes:
        if self == dest:
            msg = "disk error"
            raise OSError(msg)
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", broken_read_bytes)
    monkeypatch.setattr(
        f"{QDFONT_MODULE}.requests.get",
        lambda url, **kwargs: _FakeResp(b"fresh-bytes"),
    )

    result = YuewenQDFontMixin._load_or_download_fixed_font(
        "https://example.com/abc.woff2", dest
    )
    assert result == b"fresh-bytes"
    # read via open(): Path.read_bytes is still patched in this test
    assert dest.open("rb").read() == b"fresh-bytes"


# ---------------------------------------------------------------------------
# _decode_qdfont: orchestration
# ---------------------------------------------------------------------------


def test_decode_empty_text_returns_empty(tmp_path: Path) -> None:
    parser = StubParser(tmp_path)
    assert (
        parser._decode_qdfont(
            text="",
            fixed_font_url="https://example.com/abc.woff2",
            random_font_data=b"",
            reflected_chars=[],
        )
        == ""
    )
    assert parser.font_calls == []
    assert list(tmp_path.rglob("*.json")) == []


def test_decode_ignored_chars_only_returns_text_untouched(tmp_path: Path) -> None:
    parser = StubParser(tmp_path)
    text = " \n\u3000 "
    result = parser._decode_qdfont(
        text=text,
        fixed_font_url="https://example.com/abc.woff2",
        random_font_data=b"",
        reflected_chars=[],
    )
    assert result == text
    assert parser.build_calls == []
    assert list(tmp_path.rglob("*.json")) == []


def test_decode_applies_mapping_and_keeps_unmapped_chars(tmp_path: Path) -> None:
    parser = StubParser(tmp_path, build_additions={"a": "甲", "b": "乙"})
    result = parser._decode_qdfont(
        text="ab c",
        fixed_font_url="https://example.com/abc.woff2",
        random_font_data=b"random",
        reflected_chars=[],
    )
    assert result == "甲乙 c"
    # space is ignored, c still reaches the builder as a direct char
    assert parser.build_calls[0]["direct_chars"] == {"a", "b", "c"}
    assert " " not in parser.build_calls[0]["direct_chars"]


def test_decode_splits_direct_and_reflected_chars(tmp_path: Path) -> None:
    parser = StubParser(tmp_path, build_additions={"a": "甲"})
    result = parser._decode_qdfont(
        text="aR b",
        fixed_font_url="https://example.com/abc.woff2",
        random_font_data=b"random",
        reflected_chars=["R"],
    )
    assert result == "甲R b"
    call = parser.build_calls[0]
    assert call["direct_chars"] == {"a", "b"}
    assert call["reflected_chars"] == {"R"}


def test_decode_seeds_mapping_from_cache_file(tmp_path: Path) -> None:
    cache_dir = tmp_path / "fixed_font_maps"
    cache_dir.mkdir()
    (cache_dir / "abc.json").write_text(
        json.dumps({"z": "之"}, ensure_ascii=False), encoding="utf-8"
    )
    parser = StubParser(tmp_path, build_additions={"a": "甲"})

    result = parser._decode_qdfont(
        text="az",
        fixed_font_url="https://example.com/fonts/abc.woff2",
        random_font_data=b"random",
        reflected_chars=[],
    )
    # seeded entry from the cache file plus the newly built one
    assert result == "甲之"
    assert parser.build_calls[0]["existing_map"] == {"z": "之"}


def test_decode_persists_updated_mapping_cache(tmp_path: Path) -> None:
    (tmp_path / "fixed_font_maps").mkdir()
    parser = StubParser(tmp_path, build_additions={"a": "甲"})

    parser._decode_qdfont(
        text="a",
        fixed_font_url="https://example.com/fonts/abc.woff2",
        random_font_data=b"random",
        reflected_chars=[],
    )

    cache_file = tmp_path / "fixed_font_maps" / "abc.json"
    assert cache_file.is_file()
    assert json.loads(cache_file.read_text(encoding="utf-8")) == {"a": "甲"}

    # A second run on a fresh stub picks the persisted entry up.
    second = StubParser(tmp_path)
    second_result = second._decode_qdfont(
        text="a",
        fixed_font_url="https://example.com/fonts/abc.woff2",
        random_font_data=b"random",
        reflected_chars=[],
    )
    assert second_result == "甲"
    assert second.build_calls[0]["existing_map"] == {"a": "甲"}


def test_decode_derives_font_name_from_url_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "fixed_font_maps").mkdir()
    parser = StubParser(tmp_path)
    # Query string must not leak into the font file name.
    parser._decode_qdfont(
        text="a",
        fixed_font_url="https://example.com/fonts/abc.woff2?x=1",
        random_font_data=b"",
        reflected_chars=[],
    )
    url, dest_path = parser.font_calls[0]
    assert url == "https://example.com/fonts/abc.woff2?x=1"
    assert dest_path == tmp_path / "fixed_fonts" / "abc.woff2"
    assert parser.build_calls[0]["fixed_font_bytes"] == b"stub-fixed-font"


def test_decode_url_without_filename_uses_default_font_name(tmp_path: Path) -> None:
    (tmp_path / "fixed_font_maps").mkdir()
    parser = StubParser(tmp_path)
    parser._decode_qdfont(
        text="a",
        fixed_font_url="https://example.com/",
        random_font_data=b"",
        reflected_chars=[],
    )
    _, dest_path = parser.font_calls[0]
    assert dest_path.name == "font.woff2"


def test_decode_empty_mapping_returns_text_unchanged(tmp_path: Path) -> None:
    (tmp_path / "fixed_font_maps").mkdir()
    parser = StubParser(tmp_path, build_additions={})  # builder yields nothing new
    text = "a b"
    result = parser._decode_qdfont(
        text=text,
        fixed_font_url="https://example.com/abc.woff2",
        random_font_data=b"random",
        reflected_chars=[],
    )
    assert result == text
    # the (empty) mapping cache is still persisted
    cache_file = tmp_path / "fixed_font_maps" / "abc.json"
    assert json.loads(cache_file.read_text(encoding="utf-8")) == {}


def test_decode_survives_corrupt_mapping_cache(tmp_path: Path) -> None:
    cache_dir = tmp_path / "fixed_font_maps"
    cache_dir.mkdir()
    (cache_dir / "abc.json").write_text("{corrupt", encoding="utf-8")
    parser = StubParser(tmp_path, build_additions={"a": "甲"})

    result = parser._decode_qdfont(
        text="a",
        fixed_font_url="https://example.com/abc.woff2",
        random_font_data=b"random",
        reflected_chars=[],
    )
    assert result == "甲"
    assert parser.build_calls[0]["existing_map"] == {}


def test_decode_forwards_batch_size(tmp_path: Path) -> None:
    (tmp_path / "fixed_font_maps").mkdir()
    parser = StubParser(tmp_path, batch_size=7)
    parser._decode_qdfont(
        text="a",
        fixed_font_url="https://example.com/abc.woff2",
        random_font_data=b"random",
        reflected_chars=[],
    )
    assert parser.build_calls[0]["batch_size"] == 7


# ---------------------------------------------------------------------------
# _build_font_mapping (real implementation, OCR stubbed)
# ---------------------------------------------------------------------------


def _make_font_parser(
    ocr_result: list[tuple[str, float]] | None = None, **kwargs: Any
) -> OcrStubParser:
    return OcrStubParser(ocr_result=ocr_result, **kwargs)


def test_build_mapping_returns_cached_entries_without_parsing_fonts() -> None:
    _require_font_deps()
    parser = _make_font_parser()
    existing = {"A": "甲"}

    # Garbage font bytes would explode if the fonts were ever parsed.
    mapping = parser._build_font_mapping(
        fixed_font_bytes=b"not a font",
        random_font_bytes=b"not a font",
        direct_chars={"A"},
        reflected_chars=set(),
        existing_map=existing,
    )

    assert mapping == {"A": "甲"}
    assert existing == {"A": "甲"}  # not mutated
    assert parser.ocr_calls == []


def test_build_mapping_resolves_char_from_fixed_font() -> None:
    _require_font_deps()
    import numpy as np

    fixed = _build_ttf(["A"])
    parser = _make_font_parser(ocr_result=[("好", 0.98)], batch_size=8)
    existing: dict[str, str] = {}

    mapping = parser._build_font_mapping(
        fixed_font_bytes=fixed,
        random_font_bytes=_build_ttf([]),
        direct_chars={"A"},
        reflected_chars=set(),
        existing_map=existing,
        batch_size=8,
    )

    assert mapping == {"A": "好"}
    assert existing == {"A": "好"}  # mutated in place for cache persistence
    assert len(parser.ocr_calls) == 1
    assert parser.ocr_calls[0]["batch_size"] == 8
    assert len(parser.ocr_calls[0]["images"]) == 1
    img = parser.ocr_calls[0]["images"][0]
    assert isinstance(img, np.ndarray)
    # the glyph was actually rendered (not a blank frame)
    assert img.min() < 128


def test_build_mapping_reflected_char_is_rendered_flipped() -> None:
    _require_font_deps()
    import numpy as np

    from novel_downloader.libs import font_utils

    fixed = _build_ttf(["A"])
    parser = _make_font_parser(ocr_result=[("好", 0.9)])

    mapping = parser._build_font_mapping(
        fixed_font_bytes=fixed,
        random_font_bytes=_build_ttf([]),
        direct_chars=set(),
        reflected_chars={"A"},
        existing_map={},
    )

    assert mapping == {"A": "好"}
    img = parser.ocr_calls[0]["images"][0]
    font = font_utils.load_render_font_bytes(fixed)
    expected_plain = font_utils.render_char_image_array("A", font, is_reflect=False)
    expected_flipped = font_utils.render_char_image_array("A", font, is_reflect=True)
    assert np.array_equal(img, expected_flipped)
    assert not np.array_equal(img, expected_plain)


def test_build_mapping_resolves_char_from_random_font() -> None:
    _require_font_deps()

    random = _build_ttf(["B"])
    parser = _make_font_parser(ocr_result=[("乙", 0.9)])

    mapping = parser._build_font_mapping(
        fixed_font_bytes=_build_ttf([]),  # fixed font never consulted for "B"
        random_font_bytes=random,
        direct_chars={"B"},
        reflected_chars=set(),
        existing_map={},
    )

    assert mapping == {"B": "乙"}
    assert len(parser.ocr_calls) == 1


def test_build_mapping_skips_chars_missing_from_both_fonts() -> None:
    _require_font_deps()
    parser = _make_font_parser()

    mapping = parser._build_font_mapping(
        fixed_font_bytes=_build_ttf(["A"]),
        random_font_bytes=_build_ttf(["B"]),
        direct_chars={"X"},
        reflected_chars=set(),
        existing_map={},
    )

    assert mapping == {}
    assert parser.ocr_calls == []  # nothing to render, OCR never invoked


def test_build_mapping_ocr_failure_keeps_cached_entries_only() -> None:
    _require_font_deps()
    parser = _make_font_parser(ocr_error=RuntimeError("model exploded"))
    existing = {"Z": "旧"}

    mapping = parser._build_font_mapping(
        fixed_font_bytes=_build_ttf(["A"]),
        random_font_bytes=_build_ttf([]),
        direct_chars={"A", "Z"},
        reflected_chars=set(),
        existing_map=existing,
    )

    assert mapping == {"Z": "旧"}
    assert existing == {"Z": "旧"}
    assert len(parser.ocr_calls) == 1


def test_build_mapping_skips_empty_ocr_predictions() -> None:
    _require_font_deps()
    parser = _make_font_parser(ocr_result=[("", 0.5)])

    mapping = parser._build_font_mapping(
        fixed_font_bytes=_build_ttf(["A"]),
        random_font_bytes=_build_ttf([]),
        direct_chars={"A"},
        reflected_chars=set(),
        existing_map={},
    )

    assert mapping == {}


def test_build_mapping_handles_truncated_ocr_output() -> None:
    _require_font_deps()
    # Two tasks, but OCR only returns one prediction: zip(strict=False) must
    # not raise, and only one char is mapped.  Which char wins depends on the
    # (hash-randomized) set iteration order, so assert order-independently.
    parser = _make_font_parser(ocr_result=[("好", 0.9)])

    mapping = parser._build_font_mapping(
        fixed_font_bytes=_build_ttf(["A", "B"]),
        random_font_bytes=_build_ttf([]),
        direct_chars={"A", "B"},
        reflected_chars=set(),
        existing_map={},
    )

    assert len(mapping) == 1
    ((ch, real),) = mapping.items()
    assert ch in {"A", "B"}
    assert real == "好"
