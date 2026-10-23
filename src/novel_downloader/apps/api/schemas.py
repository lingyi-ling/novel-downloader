#!/usr/bin/env python3
"""
novel_downloader.apps.api.schemas
---------------------------------

Request models for the REST API.

Response payloads are intentionally plain dictionaries so the data mirrors the
underlying service objects (``SearchResult``, ``DownloadTask`` …) without an
extra translation layer.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

SearchSortKey = Literal[
    "quality",
    "priority",
    "site",
    "title",
    "author",
    "words",
    "update",
]

#: Maximum number of tracks accepted by the music download endpoints in one
#: request. Bounds the work a single (synchronous) call can queue up and keeps
#: the in-memory job table meaningful.
MAX_MUSIC_TRACKS = 200


class SearchRequest(BaseModel):
    """Body of ``POST /api/v1/search``."""

    keyword: str = Field(min_length=1)
    sites: list[str] | None = None
    per_site_limit: int = Field(default=5, ge=1)
    timeout: float = Field(default=5.0, gt=0)
    sort: SearchSortKey = "quality"
    order: Literal["asc", "desc"] | None = None
    limit: int | None = Field(default=None, ge=1)


class TaskCreateRequest(BaseModel):
    """Body of ``POST /api/v1/tasks``."""

    title: str | None = None
    site: str = Field(min_length=1)
    book_id: str = Field(min_length=1)


class MusicSearchRequest(BaseModel):
    """Body of ``POST /api/v1/music/search``."""

    platform: str = Field(min_length=1)
    keyword: str = Field(min_length=1)
    limit: int = Field(default=20, ge=1)


class MusicTrack(BaseModel):
    """
    Serialised :class:`novel_downloader.apps.music.providers.base.Track`.

    Clients normally send back track dictionaries they received from
    ``POST /api/v1/music/search`` unchanged.
    """

    provider: str = ""
    id: str = ""
    name: str = "未知歌曲"
    artists: str = "未知歌手"
    album: str = "未知专辑"
    duration_s: int = 0
    cover_url: str = ""
    track_no: int = 0
    ext: str = ""
    vip: bool = False
    download_url: str = ""
    http_headers: dict[str, str] = Field(default_factory=dict)
    lyric: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


class MusicDownloadRequest(BaseModel):
    """
    Body of ``POST /api/v1/music/download`` and ``POST /api/v1/music/jobs``.

    At most :data:`MAX_MUSIC_TRACKS` tracks may be submitted per request; a
    larger list is rejected with ``422`` rather than queued.
    """

    platform: str = Field(min_length=1)
    tracks: list[MusicTrack] = Field(min_length=1, max_length=MAX_MUSIC_TRACKS)
    level: str | None = None
    out_dir: str | None = None
