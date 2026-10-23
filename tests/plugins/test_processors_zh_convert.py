#!/usr/bin/env python3
"""
Offline tests for :mod:`novel_downloader.plugins.processors.zh_convert`.

OpenCC is an optional dependency and is not installed in the test
environment, so the suite covers both sides of the contract:

* the *degraded* path: a missing dependency raises a clear ``ImportError``
  with an install hint instead of crashing at processing time;
* the *working* path: a deterministic fake ``opencc`` module injected through
  ``sys.modules`` exercises direction handling, the ``apply_*`` switches and
  the metadata/chapter walk.
"""

from __future__ import annotations

from typing import Any

import pytest

from novel_downloader.plugins.processors.zh_convert import ZhConvertProcessor
from tests.plugins._processor_test_utils import (
    RecordingOpenCC,
    break_module,
    install_fake_opencc,
    make_book_info,
    make_chapter,
)


@pytest.fixture(autouse=True)
def _reset_recorder() -> None:
    RecordingOpenCC.reset()


def build(config: dict[str, Any] | None = None) -> ZhConvertProcessor:
    return ZhConvertProcessor(config or {})


# --------------------------------------------------------------------------- #
# optional dependency degradation
# --------------------------------------------------------------------------- #
def test_missing_opencc_raises_import_error_with_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    break_module(monkeypatch, "opencc")

    with pytest.raises(ImportError) as excinfo:
        build({})

    message = str(excinfo.value)
    assert "opencc-python-reimplemented" in message
    assert "requires OpenCC" in message


def test_missing_opencc_is_reported_at_construction_not_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing dep must fail fast (pipeline decides), never mid-book."""
    break_module(monkeypatch, "opencc")

    with pytest.raises(ImportError):
        build({"direction": "s2t"})


# --------------------------------------------------------------------------- #
# direction handling
# --------------------------------------------------------------------------- #
def test_default_direction_is_t2s(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_opencc(monkeypatch, RecordingOpenCC)

    build({})

    assert RecordingOpenCC.instances[-1].direction == "t2s"


def test_nullish_direction_falls_back_to_t2s(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_opencc(monkeypatch, RecordingOpenCC)

    for value in (None, ""):
        build({"direction": value})

    assert [inst.direction for inst in RecordingOpenCC.instances] == ["t2s", "t2s"]


@pytest.mark.parametrize("direction", sorted(ZhConvertProcessor.DIRECTION_WHITELIST))
def test_all_documented_directions_are_accepted(
    monkeypatch: pytest.MonkeyPatch, direction: str
) -> None:
    install_fake_opencc(monkeypatch, RecordingOpenCC)

    build({"direction": direction})

    assert RecordingOpenCC.instances[-1].direction == direction


def test_direction_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_opencc(monkeypatch, RecordingOpenCC)

    build({"direction": "  S2T  "})

    assert RecordingOpenCC.instances[-1].direction == "s2t"


def test_unknown_direction_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_opencc(monkeypatch)

    with pytest.raises(ValueError, match="Unknown OpenCC conversion direction"):
        build({"direction": "s2xx"})


def test_non_string_direction_is_reported_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_opencc(monkeypatch)

    with pytest.raises(ValueError, match="123"):
        build({"direction": 123})


# --------------------------------------------------------------------------- #
# conversion behaviour
# --------------------------------------------------------------------------- #
def test_chapter_title_and_content_are_converted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_opencc(monkeypatch, RecordingOpenCC)
    proc = build({"direction": "t2s"})

    chapter = proc.process_chapter(make_chapter(title="龍國開始", content="體會"))

    assert chapter["title"] == "龙国开始"
    assert chapter["content"] == "体会"


def test_book_info_conversion_and_apply_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_opencc(monkeypatch, RecordingOpenCC)
    proc = build(
        {
            "direction": "t2s",
            "apply_title": True,
            "apply_content": True,
            "apply_author": True,
            "apply_tags": True,
        }
    )

    book_info = proc.process_book_info(
        make_book_info(
            book_name="龍國",
            author="作者體",
            summary="簡介體",
            tags=["標籤", 42],
            volumes=[
                {
                    "volume_name": "卷一體",
                    "volume_intro": "卷介體",
                    "chapters": [
                        {
                            "title": "章標題體",
                            "url": "https://example.com/1",
                            "chapterId": "1",
                        }
                    ],
                }
            ],
        )
    )

    assert book_info["book_name"] == "龙国"
    assert book_info["author"] == "作者体"
    assert book_info["summary"] == "简介体"
    assert book_info["tags"] == ["标签", 42]
    volume = book_info["volumes"][0]
    assert volume["volume_name"] == "卷一体"
    assert volume["volume_intro"] == "卷介体"
    assert volume["chapters"][0]["title"] == "章标题体"


def test_author_and_tags_are_off_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_opencc(monkeypatch)
    proc = build({"direction": "t2s"})

    book_info = proc.process_book_info(make_book_info(author="作者體", tags=["標籤"]))

    assert book_info["author"] == "作者體"
    assert book_info["tags"] == ["標籤"]


def test_apply_switches_disable_title_and_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_opencc(monkeypatch)
    proc = build(
        {
            "direction": "t2s",
            "apply_title": False,
            "apply_content": False,
        }
    )

    chapter = proc.process_chapter(make_chapter(title="龍國", content="體會"))
    assert chapter["title"] == "龍國"
    assert chapter["content"] == "體會"

    book_info = proc.process_book_info(make_book_info(book_name="龍國", summary="體會"))
    assert book_info["book_name"] == "龍國"
    assert book_info["summary"] == "體會"


def test_content_newlines_and_structure_are_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_opencc(monkeypatch)
    proc = build({"direction": "t2s"})

    chapter = proc.process_chapter(make_chapter(content="國\n\n龍　體"))
    assert chapter["content"] == "国\n\n龙　体"


# --------------------------------------------------------------------------- #
# edge cases
# --------------------------------------------------------------------------- #
def test_empty_text_never_reaches_opencc(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_opencc(monkeypatch, RecordingOpenCC)
    proc = build({"direction": "t2s"})

    chapter = proc.process_chapter(make_chapter(title="", content=""))
    assert chapter["title"] == ""
    assert chapter["content"] == ""
    assert RecordingOpenCC.instances[-1].calls == []


def test_whitespace_only_text_is_passed_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_opencc(monkeypatch, RecordingOpenCC)
    proc = build({"direction": "t2s"})

    assert proc.process_chapter(make_chapter(title="", content="  "))["content"] == "  "
    assert RecordingOpenCC.instances[-1].calls == ["  "]


def test_non_string_content_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_opencc(monkeypatch)
    proc = build({"direction": "t2s"})

    assert proc.process_chapter(make_chapter(content=None))["content"] is None


def test_very_long_text_is_converted(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_opencc(monkeypatch)
    proc = build({"direction": "t2s"})

    text = "國" * 50_000
    chapter = proc.process_chapter(make_chapter(content=text))
    assert chapter["content"] == "国" * 50_000


def test_conversion_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_opencc(monkeypatch)
    proc = build({"direction": "s2t"})

    once = proc.process_chapter(make_chapter(title="龙国", content="体会"))
    twice = proc.process_chapter(once)

    assert once == twice
    assert once["title"] == "龍國"


def test_input_is_not_mutated(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_opencc(monkeypatch)
    proc = build({"direction": "t2s"})

    chapter = make_chapter(title="龍國", content="體會")
    proc.process_chapter(chapter)

    assert chapter["title"] == "龍國"
    assert chapter["content"] == "體會"


def test_converter_is_built_once_per_processor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_opencc(monkeypatch, RecordingOpenCC)
    proc = build({"direction": "t2s"})

    for _ in range(5):
        proc.process_chapter(make_chapter())

    assert len(RecordingOpenCC.instances) == 1
