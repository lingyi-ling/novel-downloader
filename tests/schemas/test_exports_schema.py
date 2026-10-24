#!/usr/bin/env python3
"""Tests for the public surface of the :mod:`novel_downloader.schemas` package."""

from __future__ import annotations

from typing import get_args

from novel_downloader import schemas
from novel_downloader.schemas import auth, book, config, process, search

EXPECTED_EXPORTS = {
    # config
    "BookConfig",
    "ClientConfig",
    "ParserConfig",
    "FetcherConfig",
    "OCRConfig",
    "ExporterConfig",
    "ProcessorConfig",
    # book
    "BookInfoDict",
    "ChapterDict",
    "ChapterInfoDict",
    "MediaResource",
    "MediaType",
    "VolumeInfoDict",
    # auth
    "LoginField",
    # search
    "SearchResult",
    # process
    "ExecutedStageMeta",
    "PipelineMeta",
}


def test_all_matches_expected_exports() -> None:
    assert set(schemas.__all__) == EXPECTED_EXPORTS


def test_all_has_no_duplicates() -> None:
    assert len(schemas.__all__) == len(set(schemas.__all__))


def test_every_exported_name_is_resolvable() -> None:
    for name in schemas.__all__:
        assert hasattr(schemas, name), f"{name} is listed in __all__ but missing"


def test_exports_are_the_submodule_objects() -> None:
    expected = {
        "BookConfig": config.BookConfig,
        "ClientConfig": config.ClientConfig,
        "ParserConfig": config.ParserConfig,
        "FetcherConfig": config.FetcherConfig,
        "OCRConfig": config.OCRConfig,
        "ExporterConfig": config.ExporterConfig,
        "ProcessorConfig": config.ProcessorConfig,
        "BookInfoDict": book.BookInfoDict,
        "ChapterDict": book.ChapterDict,
        "ChapterInfoDict": book.ChapterInfoDict,
        "MediaResource": book.MediaResource,
        "MediaType": book.MediaType,
        "VolumeInfoDict": book.VolumeInfoDict,
        "LoginField": auth.LoginField,
        "SearchResult": search.SearchResult,
        "ExecutedStageMeta": process.ExecutedStageMeta,
        "PipelineMeta": process.PipelineMeta,
    }

    for name, obj in expected.items():
        assert getattr(schemas, name) is obj


def test_media_type_members() -> None:
    assert get_args(book.MediaType) == (
        "image",
        "font",
        "css",
        "script",
        "audio",
        "other",
    )
