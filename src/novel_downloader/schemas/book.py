#!/usr/bin/env python3
"""
novel_downloader.schemas.book
-----------------------------

"""

from typing import Any, Literal, NotRequired, TypedDict

MediaType = Literal["image", "font", "css", "script", "audio", "other"]


class ChapterDict(TypedDict):
    id: str
    title: str
    content: str
    extra: dict[str, Any]


class MediaResource(TypedDict):
    type: MediaType
    paragraph_index: NotRequired[int]
    range: NotRequired[dict[str, int]]
    url: NotRequired[str]
    base64: NotRequired[str]
    mime: NotRequired[str]
    text: NotRequired[str]
    alt: NotRequired[str]
    width: NotRequired[int]
    height: NotRequired[int]


class ChapterInfoDict(TypedDict):
    title: str
    url: str
    chapterId: str
    accessible: NotRequired[bool]


class VolumeInfoDict(TypedDict):
    volume_name: str
    volume_cover: NotRequired[str]
    update_time: NotRequired[str]
    word_count: NotRequired[str]
    volume_intro: NotRequired[str]
    chapters: list[ChapterInfoDict]


class BookInfoDict(TypedDict):
    book_name: str
    author: str
    cover_url: str
    update_time: str
    summary: str
    extra: dict[str, Any]
    volumes: list[VolumeInfoDict]
    tags: NotRequired[list[str]]
    word_count: NotRequired[str]
    serial_status: NotRequired[str]
    summary_brief: NotRequired[str]
    last_checked: NotRequired[float]  # Unix timestamp


class ChapterFailureDict(TypedDict):
    """One chapter that could not be downloaded in a given run."""

    chapter_id: str
    chapter_title: str
    url: str
    reason: str
    attempts: int
    failed_at: str  # ISO-8601 UTC, e.g. "2026-11-01T08:30:00Z"


class DownloadSummary(TypedDict):
    """
    Outcome of a single :meth:`download_book` call.

    Invariants (pinned by tests for every code path: range filters,
    ``ignore_ids``, an all-cached book, an empty plan, a failing storage
    upsert, and both ``strict`` modes):

    * ``total == succeeded + failed``: every planned chapter either ends with
      usable content (``succeeded``) or is counted as a failure (``failed``).
    * ``skipped <= succeeded``: ``skipped`` counts the chapters that were not
      fetched because a valid cached copy already existed, so they are part of
      the chapters that ended the run complete.
    * ``failed == len(failures)``: the list is the detail behind the count.
    * ``failed == 0`` is the only condition under which the download is
      complete.  Progress reaching 100% on its own does not imply that: the bar
      reports "all planned chapters processed", which is also true when every
      chapter failed.

    :attr:`ledger_path` points at the per-run failure ledger
    (``chapter.failures.jsonl``).  It describes *this* run only -- a previous
    run's records are removed before this one starts -- and the file does not
    exist when ``failed == 0``.
    """

    book_id: str
    site: str
    total: int
    succeeded: int
    failed: int
    skipped: int
    failures: list[ChapterFailureDict]
    ledger_path: str
