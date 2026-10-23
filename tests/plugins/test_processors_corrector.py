#!/usr/bin/env python3
"""
Offline tests for :mod:`novel_downloader.plugins.processors.corrector`.

``pycorrector`` is an optional dependency (and pulls in torch), so the suite
pins three contracts:

* a missing dependency surfaces as an actionable ``ImportError`` - both at
  construction time (import/self-test) and mid-flight (engine runtime);
* engine selection and the ``_subset`` option filtering behave as documented;
* degradation is graceful: an engine error on one line keeps the original text
  instead of aborting the whole stage, and the batch path falls back to
  per-item calls when the engine misbehaves.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from novel_downloader.plugins.processors.corrector import CorrectorProcessor
from tests.plugins._processor_test_utils import (
    FakeCorrector,
    break_module,
    install_fake_pycorrector,
    make_book_info,
    make_chapter,
)

CORRECTOR_LOGGER = "novel_downloader.plugins.processors.corrector"


# --------------------------------------------------------------------------- #
# fake engines with specific failure modes
# --------------------------------------------------------------------------- #
class DictBatchCorrector(FakeCorrector):
    """pycorrector engines commonly return ``[{"target": ...}, ...]``."""

    def correct_batch(self, texts: list[str]) -> list[Any]:  # type: ignore[override]
        self.batch_calls.append(list(texts))
        return [{"target": t.replace("错", "对")} for t in texts]


class ItemWithoutTargetCorrector(FakeCorrector):
    """A dict payload without a usable ``target`` must fall back to the input."""

    def correct_batch(self, texts: list[str]) -> list[Any]:  # type: ignore[override]
        self.batch_calls.append(list(texts))
        return [{"source": t} for t in texts]


class FailingBatchCorrector(FakeCorrector):
    """``correct_batch`` explodes generically, ``correct`` still works."""

    def correct_batch(self, texts: list[str]) -> list[str]:
        self.batch_calls.append(list(texts))
        raise RuntimeError("batch exploded")


class TotallyBrokenCorrector(FakeCorrector):
    """Every engine call explodes generically."""

    def correct_batch(self, texts: list[str]) -> list[str]:
        self.batch_calls.append(list(texts))
        raise RuntimeError("batch exploded")

    def correct(self, text: str) -> str:
        raise RuntimeError("single exploded")


class ShortBatchCorrector(FakeCorrector):
    """Returns fewer results than requested (engine bug)."""

    def correct_batch(self, texts: list[str]) -> list[str]:
        self.batch_calls.append(list(texts))
        return ["only-one"]


class MissingTorchCorrector(FakeCorrector):
    """Passes the self-test, then hits a missing runtime dependency."""

    def correct_batch(self, texts: list[str]) -> list[str]:
        self.batch_calls.append(list(texts))
        if texts == ["。"]:
            return ["。"]
        raise ModuleNotFoundError("No module named 'torch'", name="torch")


class InstantlyMissingTorchCorrector(FakeCorrector):
    """The dependency is missing even for the construction-time self-test."""

    def correct_batch(self, texts: list[str]) -> list[str]:
        raise ModuleNotFoundError("No module named 'torch'", name="torch")


class SelfTestFailCorrector(FakeCorrector):
    """The warm-up call fails (e.g. no model files) but the engine still works."""

    def correct_batch(self, texts: list[str]) -> list[str]:
        self.batch_calls.append(list(texts))
        if len(self.batch_calls) == 1:
            raise RuntimeError("warmup exploded")
        return [self.correct(t) for t in texts]


@pytest.fixture(autouse=True)
def _reset_instances() -> None:
    FakeCorrector.reset()


# --------------------------------------------------------------------------- #
# optional dependency degradation
# --------------------------------------------------------------------------- #
def test_missing_pycorrector_reports_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    break_module(monkeypatch, "pycorrector")

    with pytest.raises(ImportError) as excinfo:
        CorrectorProcessor({})

    message = str(excinfo.value)
    assert "pip install pycorrector" in message
    assert "pip install torch torchvision torchaudio" in message
    assert "engine='kenlm'" in message


def test_missing_dependency_within_self_test_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_pycorrector(monkeypatch, InstantlyMissingTorchCorrector)

    with pytest.raises(ImportError, match="runtime dependency missing"):
        CorrectorProcessor({})


def test_missing_dependency_mid_flight_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_pycorrector(monkeypatch, MissingTorchCorrector)
    proc = CorrectorProcessor({})

    with pytest.raises(ImportError, match="torch"):
        proc.process_chapter(make_chapter(content="这里有错字"))


# --------------------------------------------------------------------------- #
# engine selection and option filtering
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("engine", "expected"),
    [
        (
            "kenlm",
            {
                "language_model_path": "m",
                "custom_confusion_path_or_dict": "d",
            },
        ),
        ("macbert", {"model_name_or_path": "m"}),
        ("t5", {"model_name_or_path": "m"}),
        ("ernie_csc", {"model_name_or_path": "m"}),
        (
            "gpt",
            {"model_name_or_path": "m", "model_type": "qwen", "peft_name": "p"},
        ),
        ("mucgec_bart", {"model_name_or_path": "m"}),
    ],
)
def test_engine_kwargs_are_filtered_per_engine(
    engine: str, expected: dict[str, Any]
) -> None:
    config: dict[str, Any] = {
        "language_model_path": "m",
        "custom_confusion_path_or_dict": "d",
        "model_name_or_path": "m",
        "model_type": "qwen",
        "peft_name": "p",
        "unrelated_option": "ignored",
    }

    assert CorrectorProcessor._engine_kwargs(engine, config) == expected


def test_engine_kwargs_reject_unknown_engine() -> None:
    with pytest.raises(ValueError, match="Unknown pycorrector engine"):
        CorrectorProcessor._engine_kwargs("bogus", {})


@pytest.mark.parametrize(
    "engine",
    ["kenlm", "macbert", "t5", "ernie_csc", "gpt", "mucgec_bart"],
)
def test_every_documented_engine_can_be_constructed(
    monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    install_fake_pycorrector(monkeypatch)
    proc = CorrectorProcessor({"engine": engine})

    assert proc.process_chapter(make_chapter(content="错"))["content"] == "对"


def test_engine_option_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_pycorrector(monkeypatch)
    proc = CorrectorProcessor({"engine": "KENLM"})

    assert proc.process_chapter(make_chapter(content="错"))["content"] == "对"


def test_unknown_engine_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_pycorrector(monkeypatch)

    with pytest.raises(ValueError, match="Unknown pycorrector engine: bogus"):
        CorrectorProcessor({"engine": "bogus"})


def test_non_string_engine_is_reported_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_pycorrector(monkeypatch)

    with pytest.raises(ValueError, match="Unknown pycorrector engine: 5"):
        CorrectorProcessor({"engine": 5})


def test_only_declared_options_reach_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_pycorrector(monkeypatch)

    CorrectorProcessor(
        {
            "engine": "kenlm",
            "language_model_path": "model.bin",
            "not_an_engine_option": True,
        }
    )

    assert FakeCorrector.last().kwargs == {"language_model_path": "model.bin"}


def test_engine_self_test_is_run_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_pycorrector(monkeypatch)

    CorrectorProcessor({})

    assert FakeCorrector.last().batch_calls == [["。"]]


def test_self_test_failure_warns_and_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install_fake_pycorrector(monkeypatch, SelfTestFailCorrector)

    with caplog.at_level(logging.WARNING, logger=CORRECTOR_LOGGER):
        proc = CorrectorProcessor({})

    assert "Self-test failed" in caplog.text
    assert proc.process_chapter(make_chapter(content="错"))["content"] == "对"


# --------------------------------------------------------------------------- #
# batch handler normalisation
# --------------------------------------------------------------------------- #
def test_dict_results_use_target(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_pycorrector(monkeypatch, DictBatchCorrector)
    proc = CorrectorProcessor({})

    chapter = proc.process_chapter(make_chapter(content="这里有错\n也有错"))
    assert chapter["content"] == "这里有对\n也有对"


def test_dict_results_without_target_keep_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_pycorrector(monkeypatch, ItemWithoutTargetCorrector)
    proc = CorrectorProcessor({})

    chapter = proc.process_chapter(make_chapter(content="这里有错"))
    assert chapter["content"] == "这里有错"


def test_batch_failure_falls_back_to_per_item(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install_fake_pycorrector(monkeypatch, FailingBatchCorrector)
    proc = CorrectorProcessor({})

    with caplog.at_level(logging.WARNING, logger=CORRECTOR_LOGGER):
        chapter = proc.process_chapter(
            make_chapter(title="", content="这里有错\n也有错")
        )

    assert FakeCorrector.last().batch_calls == [["。"], ["这里有错", "也有错"]]
    assert chapter["content"] == "这里有对\n也有对"
    assert ".correct_batch() failed" in caplog.text


def test_batch_size_mismatch_falls_back_to_per_item(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install_fake_pycorrector(monkeypatch, ShortBatchCorrector)
    proc = CorrectorProcessor({})

    with caplog.at_level(logging.WARNING, logger=CORRECTOR_LOGGER):
        result = proc.process_chapter(make_chapter(content="这里有错\n也有错"))

    assert result["content"] == "这里有对\n也有对"
    assert FakeCorrector.last().correct_calls == ["这里有错", "也有错"]


def test_broken_engine_keeps_original_text_instead_of_aborting(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One bad engine call must not lose the whole chapter."""
    install_fake_pycorrector(monkeypatch, TotallyBrokenCorrector)
    proc = CorrectorProcessor({})

    with caplog.at_level(logging.WARNING, logger=CORRECTOR_LOGGER):
        chapter = proc.process_chapter(make_chapter(content="这里有错\n也有错"))

    assert chapter["content"] == "这里有错\n也有错"
    assert ".correct() failed" in caplog.text


# --------------------------------------------------------------------------- #
# per-line processing
# --------------------------------------------------------------------------- #
def test_lines_are_corrected_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_pycorrector(monkeypatch)
    proc = CorrectorProcessor({})

    chapter = proc.process_chapter(make_chapter(content="第一行错\n第二行错"))
    assert chapter["content"] == "第一行对\n第二行对"
    assert FakeCorrector.last().batch_calls[-1] == ["第一行错", "第二行错"]


def test_blank_lines_are_never_sent_to_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_pycorrector(monkeypatch)
    proc = CorrectorProcessor({})

    chapter = proc.process_chapter(make_chapter(content="错\n\n   \n错"))
    assert chapter["content"] == "对\n\n   \n对"
    assert FakeCorrector.last().batch_calls[-1] == ["错", "错"]


@pytest.mark.parametrize("threshold", [2, 3])
def test_skip_if_len_le_skips_short_lines(
    monkeypatch: pytest.MonkeyPatch, threshold: int
) -> None:
    install_fake_pycorrector(monkeypatch)
    proc = CorrectorProcessor({"skip_if_len_le": threshold})

    chapter = proc.process_chapter(make_chapter(content="错错\n错错错错"))
    assert chapter["content"] == "错错\n对对对对"
    assert FakeCorrector.last().batch_calls[-1] == ["错错错错"]


def test_skip_if_len_le_ignores_surrounding_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_pycorrector(monkeypatch)
    proc = CorrectorProcessor({"skip_if_len_le": 2})

    chapter = proc.process_chapter(make_chapter(content="  错  \n错错错"))
    assert chapter["content"] == "  错  \n对对对"


def test_all_lines_skipped_skips_the_engine_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_pycorrector(monkeypatch)
    proc = CorrectorProcessor({"skip_if_len_le": 100})

    chapter = proc.process_chapter(make_chapter(content="错\n错"))
    assert chapter["content"] == "错\n错"
    assert FakeCorrector.last().batch_calls == [["。"]]  # self-test only


def test_invalid_skip_if_len_le_is_ignored_with_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install_fake_pycorrector(monkeypatch)

    with caplog.at_level(logging.WARNING, logger=CORRECTOR_LOGGER):
        proc = CorrectorProcessor({"skip_if_len_le": "not-a-number"})

    assert "invalid skip_if_len_le" in caplog.text
    assert proc.process_chapter(make_chapter(content="错"))["content"] == "对"


def test_numeric_string_skip_if_len_le_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_pycorrector(monkeypatch)
    proc = CorrectorProcessor({"skip_if_len_le": "2"})

    chapter = proc.process_chapter(make_chapter(content="错错\n错错错"))
    assert chapter["content"] == "错错\n对对对"


# --------------------------------------------------------------------------- #
# metadata walk and apply flags
# --------------------------------------------------------------------------- #
def test_book_info_defaults_only_touch_titles_and_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_pycorrector(monkeypatch)
    proc = CorrectorProcessor({})

    book_info = proc.process_book_info(
        make_book_info(
            book_name="书名错",
            author="作者错",
            summary="简介错",
            tags=["标签错"],
            volumes=[
                {
                    "volume_name": "卷名错",
                    "volume_intro": "卷介错",
                    "chapters": [
                        {
                            "title": "章名错",
                            "url": "https://example.com/1",
                            "chapterId": "1",
                        }
                    ],
                }
            ],
        )
    )

    assert book_info["book_name"] == "书名对"
    assert book_info["summary"] == "简介对"
    assert book_info["author"] == "作者错"  # apply_author defaults to False
    assert book_info["tags"] == ["标签错"]  # apply_tags defaults to False
    volume = book_info["volumes"][0]
    assert volume["volume_name"] == "卷名对"
    assert volume["volume_intro"] == "卷介对"
    assert volume["chapters"][0]["title"] == "章名对"


def test_apply_author_and_tags_can_be_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_pycorrector(monkeypatch)
    proc = CorrectorProcessor({"apply_author": True, "apply_tags": True})

    book_info = proc.process_book_info(
        make_book_info(author="作者错", tags=["标签错", 7])
    )

    assert book_info["author"] == "作者对"
    assert book_info["tags"] == ["标签对", 7]


def test_apply_flags_can_disable_title_and_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_pycorrector(monkeypatch)
    proc = CorrectorProcessor({"apply_title": False, "apply_content": False})

    chapter = proc.process_chapter(make_chapter(title="题错", content="文错"))
    assert chapter["title"] == "题错"
    assert chapter["content"] == "文错"

    book_info = proc.process_book_info(make_book_info(book_name="书错", summary="介错"))
    assert book_info["book_name"] == "书错"
    assert book_info["summary"] == "介错"


# --------------------------------------------------------------------------- #
# edge cases
# --------------------------------------------------------------------------- #
def test_empty_content_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_pycorrector(monkeypatch)
    proc = CorrectorProcessor({})

    assert proc.process_chapter(make_chapter(title="", content=""))["content"] == ""
    assert FakeCorrector.last().batch_calls == [["。"]]


def test_content_of_only_newlines_is_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documented existing behaviour: line structure is re-joined with ``\\n``."""
    install_fake_pycorrector(monkeypatch)
    proc = CorrectorProcessor({})

    assert proc.process_chapter(make_chapter(content="\n\n"))["content"] == "\n"
    assert proc.process_chapter(make_chapter(content="\n"))["content"] == ""


def test_trailing_newline_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Known limitation of the line-wise walk (``splitlines`` + ``"\\n".join``)."""
    install_fake_pycorrector(monkeypatch)
    proc = CorrectorProcessor({})

    assert proc.process_chapter(make_chapter(content="错\n"))["content"] == "对"


def test_carriage_returns_are_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_pycorrector(monkeypatch)
    proc = CorrectorProcessor({})

    chapter = proc.process_chapter(make_chapter(content="错\r\n错"))
    assert chapter["content"] == "对\n对"


def test_non_string_fields_are_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_pycorrector(monkeypatch)
    proc = CorrectorProcessor({})

    assert proc.process_chapter(make_chapter(content=None))["content"] is None
    assert proc.process_chapter(make_chapter(title=None))["title"] is None
    assert proc.process_book_info(make_book_info(book_name=None))["book_name"] is None


def test_very_long_content_is_processed_once_per_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_pycorrector(monkeypatch)
    proc = CorrectorProcessor({})

    content = "\n".join(["错" * 100] * 400)
    chapter = proc.process_chapter(make_chapter(content=content))

    assert chapter["content"] == "\n".join(["对" * 100] * 400)
    assert FakeCorrector.last().batch_calls[-1] == ["错" * 100] * 400


def test_single_line_uses_one_batch_call(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_pycorrector(monkeypatch)
    proc = CorrectorProcessor({})

    proc.process_chapter(make_chapter(content="错"))

    assert FakeCorrector.last().batch_calls[-1] == ["错"]


def test_correction_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_pycorrector(monkeypatch)
    proc = CorrectorProcessor({})

    once = proc.process_chapter(make_chapter(content="第一行错\n第二行错"))
    twice = proc.process_chapter(once)

    assert once == twice


def test_input_is_not_mutated(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_pycorrector(monkeypatch)
    proc = CorrectorProcessor({})

    chapter = make_chapter(title="题错", content="文错")
    proc.process_chapter(chapter)

    assert chapter["title"] == "题错"
    assert chapter["content"] == "文错"


def test_engine_instance_is_reused_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_pycorrector(monkeypatch)
    proc = CorrectorProcessor({})

    for _ in range(3):
        proc.process_chapter(make_chapter(content="错"))

    assert len(FakeCorrector.instances) == 1
