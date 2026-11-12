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

import re
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field, field_validator

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

#: Maximum ``limit`` accepted by ``POST /api/v1/music/search``. A provider
#: search is a fan-out over third-party (and paid) APIs, so an unbounded
#: ``limit`` would let one request buy an arbitrarily large result page; the
#: ceiling is far above what the music UIs request (20 per page) while still
#: being a sane single-request budget. Larger values are rejected with ``422``
#: instead of being clamped silently, so a client bug is visible.
MAX_MUSIC_SEARCH_LIMIT = 100

#: Shape of the opaque ids the music endpoints expose: ``search_id`` (echoed
#: back by download requests) and ``job_id`` (the async job path parameter).
#: The server generates lowercase hex, but the pattern accepts any short
#: ``[A-Za-z0-9_-]`` token so a future id format keeps working. The ceiling
#: keeps meaningless lookups and log-injection payloads out; malformed values
#: are a ``422`` on both transports.
MUSIC_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"

#: A ``search_id`` with the right shape (unknown ids stay a graceful "search
#: expired" fallback; only malformed values are rejected).
MusicSearchId = Annotated[str, Field(pattern=MUSIC_ID_PATTERN)]

#: Characters a ``book_id`` may never contain. ``book_id`` is joined onto the
#: raw-data directory to place the chapter cache and media files
#: (``raw_data/<site>/<book_id>``); path separators, the Windows drive colon
#: and the other Windows-reserved characters would let a crafted request write
#: outside that tree, and control characters are never part of a real id.
_BOOK_ID_INVALID_CHARS = re.compile(r'[/\\<>:"|?*\x00-\x1F]')

#: Length ceiling for ``book_id``: identifiers are short; anything huge is
#: hostile, and it keeps the resulting directory name sane.
_MAX_BOOK_ID_LENGTH = 128


def validate_book_id(value: str) -> str:
    """
    Reject ``book_id`` values that cannot safely name a cache directory.

    ``book_id`` arrives in request bodies (task creation, watchlist) and later
    becomes the directory ``raw_data/<site>/<book_id>`` that holds the chapter
    SQLite cache and the media files. Values containing path separators, a
    drive-letter colon, other Windows-reserved characters or control
    characters — or the dot-only names ``.`` / ``..`` — would place those
    writes outside the raw-data tree, so they are rejected with ``422``. The
    value is stripped, mirroring how titles are normalized.

    Shared with the embedded server, whose task and watchlist endpoints feed
    the same download pipeline.
    """
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("book_id must not be empty or whitespace-only")
    if len(cleaned) > _MAX_BOOK_ID_LENGTH:
        raise ValueError(
            f"book_id must be at most {_MAX_BOOK_ID_LENGTH} characters long"
        )
    if _BOOK_ID_INVALID_CHARS.search(cleaned):
        raise ValueError(
            "book_id must not contain path separators, ':' or other "
            "Windows-reserved characters"
        )
    if cleaned in {".", ".."} or cleaned.endswith("."):
        raise ValueError("book_id must be a usable directory name")
    return cleaned


#: Request field type for ``book_id`` values.
BookId = Annotated[str, AfterValidator(validate_book_id)]


class SearchRequest(BaseModel):
    """Body of ``POST /api/v1/search``."""

    keyword: str = Field(min_length=1)
    sites: list[str] | None = None
    per_site_limit: int = Field(default=5, ge=1)
    timeout: float = Field(default=5.0, gt=0)
    sort: SearchSortKey = "quality"
    order: Literal["asc", "desc"] | None = None
    limit: int | None = Field(default=None, ge=1)
    #: Fold same-book results into one merged entry per book: each entry is
    #: the book's best source plus a ``sources`` list of every site carrying
    #: it (best first).  Off by default so the response shape stays the flat
    #: one existing clients (Android) expect.
    group: bool = False


class BrowseRequest(BaseModel):
    """Body of ``POST /api/v1/browse``."""

    site: str = Field(min_length=1)
    #: Omitted means "the site's ranking page"; set to a category id to list
    #: that category instead.
    category_id: str | None = None
    gender: Literal["male", "female", "1", "2"] = "male"
    limit: int = Field(default=20, ge=1, le=100)


class TaskCreateRequest(BaseModel):
    """Body of ``POST /api/v1/tasks``."""

    title: str | None = None
    site: str = Field(min_length=1)
    book_id: BookId = Field(min_length=1)


class LoginSubmitRequest(BaseModel):
    """
    Body of ``POST /api/v1/login/requests/{req_id}``.

    ``values`` maps login-field names to the raw strings a user would type.
    ``type=cookie`` fields carry the unpasted cookie string (e.g.
    ``"key=value; other=value"``); the server parses it like the Web UI did.
    """

    values: dict[str, str] = Field(default_factory=dict)


class WatchlistAddRequest(BaseModel):
    """Body of ``POST /api/v1/watchlist``."""

    site: str = Field(min_length=1)
    book_id: BookId = Field(min_length=1)
    title: str | None = None


class WatchlistCheckRequest(BaseModel):
    """
    Body of ``POST /api/v1/watchlist/check``.

    ``download`` never starts a download. When true, entries that need an
    update gain a ready-to-post ``task`` body in the response, so the caller
    can queue the download itself.
    """

    download: bool = False


class MusicSearchRequest(BaseModel):
    """Body of ``POST /api/v1/music/search``.

    ``platform`` may be ``"all"`` (the default) to search every registered
    platform at once: results are merged per song, each row carrying
    ``sources``. Responses then also include ``search_id`` and ``platforms``.

    ``limit`` is capped at :data:`MAX_MUSIC_SEARCH_LIMIT`; a larger value is a
    ``422`` (never forwarded to a provider). ``offset`` is forwarded to a
    single-platform provider as the number of hits to skip, but not every
    provider implements paging — the response's ``supports_offset`` says
    whether this one does, so a "load more" control can stop instead of asking
    forever. Aggregate searches always start at the first hit (``offset`` is
    only honoured for an explicit platform).
    """

    platform: str = Field(default="all", min_length=1)
    keyword: str = Field(min_length=1)
    limit: int = Field(default=20, ge=1, le=MAX_MUSIC_SEARCH_LIMIT)
    offset: int = Field(default=0, ge=0)


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
    #: Local file populated by the downloader (or by the caller); used by
    #: ``POST /api/v1/music/export``.
    path: str = ""
    lyric: str = ""
    #: Platform keys that carry this song, best first (aggregate search only).
    sources: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


class MusicDownloadRequest(BaseModel):
    """
    Body of ``POST /api/v1/music/download`` and ``POST /api/v1/music/jobs``.

    At most :data:`MAX_MUSIC_TRACKS` tracks may be submitted per request; a
    larger list is rejected with ``422`` rather than queued.

    When the tracks came from an aggregate search, echo that response's
    ``search_id``: it lets the server restore the original provider objects
    (their resolve data is not JSON-serialisable) instead of the reduced
    dictionaries. ``platform="all"`` (the default) downloads each track on
    its own ``provider``; an explicit platform key overrides that.
    """

    platform: str = Field(default="all", min_length=1)
    tracks: list[MusicTrack] = Field(min_length=1, max_length=MAX_MUSIC_TRACKS)
    search_id: MusicSearchId | None = None
    level: str | None = None
    out_dir: str | None = None

    @field_validator("search_id", mode="before")
    @classmethod
    def _blank_search_id_is_absent(cls, value: Any) -> Any:
        """Keep the historical "blank means no id" behaviour, then validate.

        Clients that always send the field use ``""`` (or whitespace) when
        they have no id; that used to mean "fall back to the submitted
        dictionaries". Treat those as absent so only a *non-empty* value
        has to match :data:`MUSIC_ID_PATTERN`; a malformed id is a ``422``
        instead of a pointless cache lookup.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value


class MusicExportRequest(BaseModel):
    """
    Body of ``POST /api/v1/music/export``.

    ``out_path`` may be a file name or a directory. When omitted the playlist
    is written into the music settings' ``out_dir`` with a timestamped name.

    Playlists only store file locations, so ``platform`` is informational and
    accepts ``"all"`` for tracks that came from an aggregate search.
    """

    platform: str = Field(default="all", min_length=1)
    tracks: list[MusicTrack] = Field(min_length=1, max_length=MAX_MUSIC_TRACKS)
    out_path: str | None = None
