#!/usr/bin/env python3
"""Contract tests for the ``TypedDict`` models in ``schemas.book``."""

from __future__ import annotations

import json
from typing import get_args, get_type_hints, is_typeddict

from novel_downloader.schemas.book import (
    BookInfoDict,
    ChapterDict,
    ChapterInfoDict,
    MediaResource,
    MediaType,
    VolumeInfoDict,
)


class TestChapterDict:
    def test_required_and_optional_keys(self) -> None:
        assert ChapterDict.__required_keys__ == frozenset(
            {"id", "title", "content", "extra"}
        )
        assert ChapterDict.__optional_keys__ == frozenset()

    def test_type_hints(self) -> None:
        hints = get_type_hints(ChapterDict)

        assert hints["id"] is str
        assert hints["title"] is str
        assert hints["content"] is str
        # ``extra`` is ``dict[str, Any]``; ``Any`` cannot be compared reliably,
        # so check the container type and key type instead.
        assert getattr(hints["extra"], "__origin__", None) is dict
        assert get_args(hints["extra"])[0] is str

    def test_construction(self) -> None:
        chapter = ChapterDict(
            id="c1",
            title="Chapter 1",
            content="Hello",
            extra={"resources": []},
        )

        assert chapter["id"] == "c1"
        assert chapter["extra"]["resources"] == []

    def test_unknown_fields_are_tolerated(self) -> None:
        chapter = ChapterDict(
            id="c1",
            title="Chapter 1",
            content="Hello",
            extra={},
            unknown="kept",
        )

        assert chapter["unknown"] == "kept"

    def test_no_runtime_type_validation(self) -> None:
        # TypedDict is a static contract; dict construction performs no checks.
        chapter = ChapterDict(id=1, title=None, content=2, extra=[])  # type: ignore[typeddict-item]

        assert chapter["id"] == 1

    def test_json_round_trip(self) -> None:
        chapter = ChapterDict(id="c1", title="T", content="C", extra={"n": 1})

        assert json.loads(json.dumps(chapter)) == chapter


class TestMediaResource:
    def test_required_and_optional_keys(self) -> None:
        assert MediaResource.__required_keys__ == frozenset({"type"})
        assert MediaResource.__optional_keys__ == frozenset(
            {
                "paragraph_index",
                "range",
                "url",
                "base64",
                "mime",
                "text",
                "alt",
                "width",
                "height",
            }
        )

    def test_type_hints(self) -> None:
        hints = get_type_hints(MediaResource)

        assert get_args(MediaType) == (
            "image",
            "font",
            "css",
            "script",
            "audio",
            "other",
        )
        assert hints["type"] == MediaType
        assert hints["paragraph_index"] is int
        assert hints["range"] == dict[str, int]
        assert hints["url"] is str
        assert hints["width"] is int
        assert hints["height"] is int

    def test_minimal_resource(self) -> None:
        resource = MediaResource(type="image")

        assert resource == {"type": "image"}

    def test_full_resource(self) -> None:
        resource = MediaResource(
            type="font",
            range={"start": 1, "end": 5},
            base64="AAAA",
            mime="font/woff2",
            text="font",
            alt="font",
        )

        assert resource["range"] == {"start": 1, "end": 5}
        assert resource["mime"] == "font/woff2"

    def test_unknown_fields_are_tolerated(self) -> None:
        resource = MediaResource(type="image", url="https://example.com/a.png", x="y")

        assert resource["x"] == "y"


class TestChapterInfoDict:
    def test_required_and_optional_keys(self) -> None:
        assert ChapterInfoDict.__required_keys__ == frozenset(
            {"title", "url", "chapterId"}
        )
        assert ChapterInfoDict.__optional_keys__ == frozenset({"accessible"})

    def test_type_hints(self) -> None:
        hints = get_type_hints(ChapterInfoDict)

        assert hints["title"] is str
        assert hints["url"] is str
        assert hints["chapterId"] is str
        assert hints["accessible"] is bool

    def test_optional_accessible_may_be_omitted(self) -> None:
        info = ChapterInfoDict(title="Ch 1", url="/1", chapterId="1")

        assert "accessible" not in info

    def test_accessible_may_be_set(self) -> None:
        info = ChapterInfoDict(title="Ch 1", url="/1", chapterId="1", accessible=False)

        assert info["accessible"] is False


class TestVolumeInfoDict:
    def test_required_and_optional_keys(self) -> None:
        assert VolumeInfoDict.__required_keys__ == frozenset(
            {"volume_name", "chapters"}
        )
        assert VolumeInfoDict.__optional_keys__ == frozenset(
            {"volume_cover", "update_time", "word_count", "volume_intro"}
        )

    def test_type_hints(self) -> None:
        hints = get_type_hints(VolumeInfoDict)

        assert hints["volume_name"] is str
        assert hints["volume_cover"] is str
        assert hints["update_time"] is str
        assert hints["word_count"] is str
        assert hints["volume_intro"] is str
        assert getattr(hints["chapters"], "__origin__", None) is list

    def test_nested_chapters(self) -> None:
        volume = VolumeInfoDict(
            volume_name="Volume 1",
            volume_intro="intro",
            chapters=[
                ChapterInfoDict(title="Ch 1", url="/1", chapterId="1"),
                ChapterInfoDict(
                    title="Ch 2", url="/2", chapterId="2", accessible=False
                ),
            ],
        )

        assert len(volume["chapters"]) == 2
        assert volume["chapters"][1]["accessible"] is False

    def test_minimal_volume(self) -> None:
        volume = VolumeInfoDict(volume_name="V", chapters=[])

        assert volume["chapters"] == []

    def test_unknown_fields_are_tolerated(self) -> None:
        volume = VolumeInfoDict(volume_name="V", chapters=[], extra_field=True)

        assert volume["extra_field"] is True


class TestBookInfoDict:
    def test_required_and_optional_keys(self) -> None:
        assert BookInfoDict.__required_keys__ == frozenset(
            {
                "book_name",
                "author",
                "cover_url",
                "update_time",
                "summary",
                "extra",
                "volumes",
            }
        )
        assert BookInfoDict.__optional_keys__ == frozenset(
            {
                "tags",
                "word_count",
                "serial_status",
                "summary_brief",
                "last_checked",
            }
        )

    def test_type_hints(self) -> None:
        hints = get_type_hints(BookInfoDict)

        assert hints["book_name"] is str
        assert hints["author"] is str
        assert hints["cover_url"] is str
        assert hints["update_time"] is str
        assert hints["summary"] is str
        assert getattr(hints["extra"], "__origin__", None) is dict
        assert getattr(hints["volumes"], "__origin__", None) is list
        assert getattr(hints["tags"], "__origin__", None) is list
        assert hints["word_count"] is str
        assert hints["last_checked"] is float

    def test_minimal_book(self) -> None:
        book = BookInfoDict(
            book_name="Book",
            author="Author",
            cover_url="",
            update_time="",
            summary="",
            extra={},
            volumes=[],
        )

        assert book["book_name"] == "Book"
        assert book["volumes"] == []
        assert "tags" not in book

    def test_optional_fields_may_be_present(self) -> None:
        book = BookInfoDict(
            book_name="Book",
            author="Author",
            cover_url="",
            update_time="2026-01-01",
            summary="Summary",
            extra={},
            volumes=[],
            tags=["fantasy"],
            word_count="1万字",
            serial_status="连载中",
            summary_brief="Brief",
            last_checked=123.5,
        )

        assert book["tags"] == ["fantasy"]
        assert book["word_count"] == "1万字"
        assert book["serial_status"] == "连载中"
        assert book["summary_brief"] == "Brief"
        assert book["last_checked"] == 123.5

    def test_nested_structure(self) -> None:
        book = BookInfoDict(
            book_name="Book",
            author="Author",
            cover_url="https://example.com/cover.png",
            update_time="2026-01-01",
            summary="Summary",
            extra={"download_url": "https://example.com/book"},
            volumes=[
                VolumeInfoDict(
                    volume_name="Main",
                    chapters=[ChapterInfoDict(title="Ch 1", url="/1", chapterId="1")],
                )
            ],
        )

        assert book["volumes"][0]["chapters"][0]["chapterId"] == "1"
        assert book["extra"]["download_url"].endswith("/book")

    def test_unknown_fields_are_tolerated(self) -> None:
        book = BookInfoDict(
            book_name="Book",
            author="Author",
            cover_url="",
            update_time="",
            summary="",
            extra={},
            volumes=[],
            source_url="https://example.com",
        )

        assert book["source_url"] == "https://example.com"

    def test_json_round_trip_preserves_known_and_unknown_keys(self) -> None:
        book = BookInfoDict(
            book_name="Book",
            author="Author",
            cover_url="",
            update_time="",
            summary="Summary",
            extra={"resources": [{"type": "image", "url": "/a.png"}]},
            volumes=[
                VolumeInfoDict(
                    volume_name="Main",
                    chapters=[ChapterInfoDict(title="Ch 1", url="/1", chapterId="1")],
                )
            ],
            tags=["a"],
            extra_unknown=42,
        )

        restored = json.loads(json.dumps(book))

        assert restored == book
        assert restored["extra_unknown"] == 42
        assert restored["volumes"][0]["chapters"][0]["title"] == "Ch 1"


def test_all_models_are_typed_dicts() -> None:
    for model in (
        ChapterDict,
        MediaResource,
        ChapterInfoDict,
        VolumeInfoDict,
        BookInfoDict,
    ):
        assert is_typeddict(model)
