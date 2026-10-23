#!/usr/bin/env python3
"""
Shared, dependency-free helpers for the processor test suite.

The processors live behind optional third-party dependencies (OpenCC,
pycorrector) and network endpoints, so their tests rely on two things defined
here:

* ``make_chapter`` / ``make_book_info``: small, deterministic fixtures shaped
  exactly like the pipeline's ``ChapterDict`` / ``BookInfoDict``.
* ``install_fake_opencc`` / ``install_fake_pycorrector``: in-memory stand-ins
  injected through ``sys.modules`` so conversion/correction logic can be tested
  offline and deterministically.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

from novel_downloader.schemas import BookInfoDict, ChapterDict


def make_chapter(**overrides: Any) -> ChapterDict:
    """Return a minimal but schema-complete ``ChapterDict``."""
    chapter: ChapterDict = {
        "id": "1",
        "title": "第1章 开始",
        "content": "第一行。\n第二行。",
        "extra": {},
    }
    chapter.update(overrides)  # type: ignore[typeddict-item]
    return chapter


def make_book_info(**overrides: Any) -> BookInfoDict:
    """Return a minimal but schema-complete ``BookInfoDict`` with one volume."""
    book_info: BookInfoDict = {
        "book_name": "测试小说",
        "author": "测试作者",
        "cover_url": "https://example.com/cover.jpg",
        "update_time": "2024-01-01",
        "summary": "这是一段简介。",
        "extra": {"source": "unit-test"},
        "volumes": [
            {
                "volume_name": "第一卷",
                "volume_intro": "本卷简介。",
                "chapters": [
                    {
                        "title": "第1章 开始",
                        "url": "https://example.com/1",
                        "chapterId": "1",
                    },
                ],
            },
        ],
        "tags": ["玄幻", "测试"],
    }
    book_info.update(overrides)  # type: ignore[typeddict-item]
    return book_info


# --------------------------------------------------------------------------- #
# fake opencc
# --------------------------------------------------------------------------- #
class FakeOpenCC:
    """
    Deterministic stand-in for ``opencc.OpenCC``.

    Only the characters used by the test suite are mapped; everything else is
    passed through unchanged, which mirrors OpenCC's behaviour (it only
    rewrites characters present in its dictionaries).
    """

    _T2S: dict[str, str] = {
        "龍": "龙",
        "國": "国",
        "體": "体",
        "簡": "简",
        "標": "标",
        "籤": "签",
        "題": "题",
        "會": "会",
        "開": "开",
    }
    TABLES: dict[str, dict[str, str]] = {
        "t2s": _T2S,
        "s2t": {v: k for k, v in _T2S.items()},
    }

    def __init__(self, direction: str) -> None:
        self.direction = direction
        self.calls: list[str] = []

    def convert(self, text: str) -> str:
        self.calls.append(text)
        table = self.TABLES.get(self.direction, {})
        return "".join(table.get(ch, ch) for ch in text)


class RecordingOpenCC(FakeOpenCC):
    """``FakeOpenCC`` that records every instance created (for assertions)."""

    instances: list[RecordingOpenCC] = []

    def __init__(self, direction: str) -> None:
        super().__init__(direction)
        RecordingOpenCC.instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances = []


def install_fake_opencc(
    monkeypatch: Any,
    opencc_cls: type[FakeOpenCC] = FakeOpenCC,
) -> ModuleType:
    """
    Register a fake ``opencc`` module in ``sys.modules``.

    ``ZhConvertProcessor`` imports ``opencc`` lazily inside the constructor,
    so replacing the module entry is enough to exercise the conversion paths
    without the real (optional) dependency.
    """
    module = ModuleType("opencc")
    module.OpenCC = opencc_cls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opencc", module)
    return module


def break_module(monkeypatch: Any, name: str) -> None:
    """
    Force ``import name`` to fail by shadowing it with ``None``.

    ``sys.modules[name] = None`` makes the import system raise
    ``ModuleNotFoundError`` deterministically, whether or not the real package
    happens to be installed on the machine running the tests.
    """
    monkeypatch.setitem(sys.modules, name, None)


# --------------------------------------------------------------------------- #
# fake pycorrector
# --------------------------------------------------------------------------- #
class FakeCorrector:
    """
    Stand-in for the pycorrector engines used by ``CorrectorProcessor``.

    Mimics the subset of the API the processor relies on and records the calls
    it receives. The default transform is deterministic and idempotent.
    """

    #: every instance built through this class (the processor builds them)
    instances: list[FakeCorrector] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.correct_calls: list[str] = []
        self.batch_calls: list[list[str]] = []
        #: optional per-call behaviour hooks
        self.batch_hook: Any = None
        self.single_hook: Any = None
        type(self).instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances = []

    @classmethod
    def last(cls) -> FakeCorrector:
        """Return the most recently constructed instance."""
        assert cls.instances, "no FakeCorrector instance was created"
        return cls.instances[-1]

    def correct_batch(self, texts: list[str]) -> list[str]:
        self.batch_calls.append(list(texts))
        if self.batch_hook is not None:
            return self.batch_hook(list(texts))  # type: ignore[no-any-return]
        return [self.correct(t) for t in texts]

    def correct(self, text: str) -> str:
        self.correct_calls.append(text)
        if self.single_hook is not None:
            return self.single_hook(text)  # type: ignore[no-any-return]
        return text.replace("错", "对")


#: engines the processor imports as ``pycorrector.<Attr>``
_ENGINE_ATTRS = {
    "kenlm": "Corrector",
    "macbert": "MacBertCorrector",
    "t5": "T5Corrector",
    "ernie_csc": "ErnieCscCorrector",
}

#: engines imported from a nested submodule: ``pycorrector.gpt.gpt_corrector``
_ENGINE_SUBMODULES = {
    "pycorrector.gpt.gpt_corrector": "GptCorrector",
    "pycorrector.mucgec_bart.mucgec_bart_corrector": "MuCGECBartCorrector",
}


def install_fake_pycorrector(
    monkeypatch: Any,
    corrector_cls: type[FakeCorrector] = FakeCorrector,
) -> ModuleType:
    """
    Register a fake ``pycorrector`` package (including nested engine modules).

    Every engine attribute and every ``pycorrector.<pkg>.<mod>`` submodule
    referenced by ``CorrectorProcessor`` is wired to ``corrector_cls``, so all
    six documented engines can be constructed offline.
    """
    root = ModuleType("pycorrector")
    for attr in _ENGINE_ATTRS.values():
        setattr(root, attr, corrector_cls)

    patched: dict[str, ModuleType] = {"pycorrector": root}

    for modname, attr in _ENGINE_SUBMODULES.items():
        parent_name, _, child_name = modname.rpartition(".")
        parent = patched.get(parent_name)
        if parent is None:
            parent = ModuleType(parent_name)
            patched[parent_name] = parent
            # expose the intermediate package (e.g. ``pycorrector.gpt``)
            setattr(root, parent_name.split(".", 1)[1], parent)

        child = ModuleType(modname)
        setattr(child, attr, corrector_cls)
        setattr(parent, child_name, child)
        patched[modname] = child

    for name, module in patched.items():
        monkeypatch.setitem(sys.modules, name, module)
    return root
