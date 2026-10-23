#!/usr/bin/env python3
"""
Registry + chained-pipeline tests for the text processors.

The pipeline (``plugins/mixins/process._pc_run_stage``) resolves each stage
through ``registrar.get_processor`` and feeds a stage's output into the next
one, for both book metadata and chapters. These tests reproduce that loop with
in-memory data so the *contract between stages* is pinned:

* every documented processor name resolves through the registry;
* stages run strictly left-to-right and the result depends on that order;
* a stage never mutates its input (the pipeline keeps the previous stage's
  data alive for incremental reuse).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.plugins import registry
from novel_downloader.plugins.processors.cleaner import CleanerProcessor
from novel_downloader.plugins.processors.corrector import CorrectorProcessor
from novel_downloader.plugins.processors.translator.edge import (
    EdgeTranslaterProcessor,
)
from novel_downloader.plugins.processors.translator.google import (
    GoogleTranslaterProcessor,
)
from novel_downloader.plugins.processors.translator.youdao import (
    YoudaoTranslaterProcessor,
)
from novel_downloader.plugins.processors.zh_convert import ZhConvertProcessor
from novel_downloader.schemas import BookInfoDict, ChapterDict, ProcessorConfig
from tests.plugins._processor_test_utils import (
    FakeOpenCC,
    install_fake_opencc,
    install_fake_pycorrector,
    make_book_info,
    make_chapter,
)

registrar = registry.registrar

#: key -> processor class, exactly as documented in processors-reference.md
DOCUMENTED_PROCESSORS: dict[str, type[Any]] = {
    "cleaner": CleanerProcessor,
    "zh_convert": ZhConvertProcessor,
    "corrector": CorrectorProcessor,
    "translator.google": GoogleTranslaterProcessor,
    "translator.edge": EdgeTranslaterProcessor,
    "translator.youdao": YoudaoTranslaterProcessor,
}


def write_json(tmp_path: Path, name: str, payload: Any) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def run_pipeline(
    processors: list[Any],
    book_info: BookInfoDict,
    chapters: list[ChapterDict],
) -> tuple[BookInfoDict, list[ChapterDict]]:
    """
    Mirror ``ProcessMixin._pc_run_stage``: one stage sees the previous output.

    ``processors`` is a list of already-built processor instances; the pipeline
    wires one stage's output into the next stage's input.
    """
    out_chapters = list(chapters)
    for proc in processors:
        book_info = proc.process_book_info(book_info)
        out_chapters = [proc.process_chapter(ch) for ch in out_chapters]
    return book_info, out_chapters


# --------------------------------------------------------------------------- #
# registry wiring
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("name", "expected"), sorted(DOCUMENTED_PROCESSORS.items()))
def test_documented_processor_names_resolve(
    monkeypatch: pytest.MonkeyPatch, name: str, expected: type[Any]
) -> None:
    install_fake_opencc(monkeypatch)
    install_fake_pycorrector(monkeypatch)

    proc = registrar.get_processor(name, {})

    assert isinstance(proc, expected)


def test_processor_lookup_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_opencc(monkeypatch)

    proc = registrar.get_processor("  Cleaner  ", {})

    assert isinstance(proc, CleanerProcessor)


def test_unknown_processor_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unsupported processor"):
        registrar.get_processor("does-not-exist", {})


@pytest.mark.parametrize("name", sorted(DOCUMENTED_PROCESSORS))
def test_processors_expose_the_processor_protocol(name: str) -> None:
    cls = DOCUMENTED_PROCESSORS[name]

    assert callable(cls.process_book_info)
    assert callable(cls.process_chapter)


def test_config_options_are_forwarded_to_the_processor(
    tmp_path: Path,
) -> None:
    removes = write_json(tmp_path, "r.json", ["广告"])
    pconf = ProcessorConfig(
        name="cleaner", overwrite=False, options={"content_removes": removes}
    )

    proc = registrar.get_processor(pconf.name, pconf.options)

    assert proc.process_chapter(make_chapter(content="广告文"))["content"] == "文"


# --------------------------------------------------------------------------- #
# chained stages
# --------------------------------------------------------------------------- #
def build_chain(
    tmp_path: Path, order: list[str], monkeypatch: pytest.MonkeyPatch
) -> list[Any]:
    """Build the requested stages; ``order`` names must be known processors."""
    install_fake_opencc(monkeypatch, FakeOpenCC)
    replace = write_json(tmp_path, "replace.json", {"龙": "X"})

    builders: dict[str, Any] = {
        "cleaner": lambda: CleanerProcessor({"content_replace": replace}),
        "s2t": lambda: ZhConvertProcessor({"direction": "s2t"}),
        "cleaner_title": lambda: CleanerProcessor({"title_replace": replace}),
    }
    return [builders[name]() for name in order]


def test_stage_order_changes_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clean-then-convert and convert-then-clean are genuinely different."""
    clean_first = build_chain(tmp_path, ["cleaner", "s2t"], monkeypatch)
    convert_first = build_chain(tmp_path, ["s2t", "cleaner"], monkeypatch)

    chapter = make_chapter(title="", content="龙")

    _, after_clean_first = run_pipeline(clean_first, make_book_info(), [chapter])
    _, after_convert_first = run_pipeline(convert_first, make_book_info(), [chapter])

    assert after_clean_first[0]["content"] == "X"  # replaced, then unconvertible
    assert after_convert_first[0]["content"] == "龍"  # converted, then unmatched
    assert after_clean_first != after_convert_first


def test_book_info_flows_through_every_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = build_chain(tmp_path, ["cleaner_title", "s2t"], monkeypatch)

    book_info = make_book_info(book_name="龙国", summary="龙国简介")
    result, _ = run_pipeline(pipeline, book_info, [])

    # cleaner replaced 龙 -> X in titles, then s2t converted 国 -> 國
    assert result["book_name"] == "X國"
    # summary is content-like: the title rule (龙 -> X) must not touch it, but
    # the converter (apply_content, s2t) rewrites all of it.
    assert result["summary"] == "龍國簡介"


def test_repeating_the_pipeline_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = build_chain(tmp_path, ["cleaner", "s2t"], monkeypatch)

    first_bi, first_ch = run_pipeline(
        pipeline, make_book_info(book_name="龙"), [make_chapter(content="龙")]
    )
    second_bi, second_ch = run_pipeline(pipeline, first_bi, first_ch)

    assert first_bi == second_bi
    assert first_ch == second_ch


def test_stages_do_not_mutate_previous_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = build_chain(tmp_path, ["cleaner", "s2t"], monkeypatch)

    original_book = make_book_info(book_name="龙国")
    original_chapter = make_chapter(title="龙国", content="龙国")
    book_snapshot = json.loads(json.dumps(original_book))
    chapter_snapshot = json.loads(json.dumps(original_chapter))

    run_pipeline(pipeline, original_book, [original_chapter])

    assert original_book == book_snapshot
    assert original_chapter == chapter_snapshot


def test_rebuilding_the_pipeline_reproduces_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministic: identical config + input => identical output."""
    chapter = make_chapter(title="龙", content="龙")

    first = run_pipeline(
        build_chain(tmp_path, ["cleaner", "s2t"], monkeypatch),
        make_book_info(),
        [chapter],
    )
    second = run_pipeline(
        build_chain(tmp_path, ["cleaner", "s2t"], monkeypatch),
        make_book_info(),
        [chapter],
    )

    assert first == second


def test_empty_stage_list_leaves_data_untouched() -> None:
    chapter = make_chapter(title="题", content="文")

    _, result = run_pipeline([], make_book_info(), [chapter])

    assert result == [chapter]


def test_pipeline_handles_empty_chapter_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = build_chain(tmp_path, ["cleaner", "s2t"], monkeypatch)

    book_info, chapters = run_pipeline(pipeline, make_book_info(), [])

    assert chapters == []
    assert book_info["book_name"] == "测试小说"
