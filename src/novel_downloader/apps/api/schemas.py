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
from datetime import date
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, Field, field_validator

from novel_downloader.infra.paths import is_safe_path_component

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

#: Longest search keyword accepted. Every keyword fans out to one provider
#: request per site, so a multi-kilobyte value only spends provider quota and
#: cache memory; real book titles are far shorter.
MAX_KEYWORD_LENGTH = 200

#: Longest display title accepted for a download task or watchlist entry.
MAX_TITLE_LENGTH = 512

#: Longest browse category id accepted.
MAX_CATEGORY_ID_LENGTH = 128

#: Longest site key accepted. Registered keys are short module basenames.
MAX_SITE_KEY_LENGTH = 64

#: Shape of a site key. Site keys name plugin modules and appear in log lines,
#: so only the characters used by the registry are accepted; whitespace, path
#: separators and control characters never reach module lookup.
_SITE_KEY_CHARS = re.compile(r"[A-Za-z0-9_.-]+")

#: Maximum number of sites one request may explicitly select.
MAX_SEARCH_SITES = 100

#: Upper bound for ``SearchRequest.per_site_limit``. A provider search is a
#: network request (and, on some sites, a paid call), so one request cannot
#: ask for an arbitrarily large page.
MAX_PER_SITE_LIMIT = 100

#: Upper bound for the aggregate result ``limit`` (applied after ranking).
MAX_SEARCH_LIMIT = 500

#: Upper bound for a per-site search timeout, in seconds. The endpoint's own
#: wall-clock budget defaults to 120 s, so a longer per-site wait can never be
#: honoured and would only forward a meaningless value to aiohttp.
MAX_SEARCH_TIMEOUT = 120.0

#: Upper bound for the optional ``min_words``/``max_words`` search filters.
#: Real books are far below it, so a larger value is either a unit mix-up or
#: an attempt to spell "no limit" explicitly; it is rejected with ``422``
#: rather than clamped silently.
MAX_SEARCH_WORDS = 1_000_000_000

#: Upper bound for the music paging offset. Providers translate it into a page
#: number; an absurd offset would ask them for an impossible page.
MAX_MUSIC_OFFSET = 10_000

#: Control characters (C0 plus DEL). Legitimate titles, ids and URLs never
#: contain them; they enable log injection and break header/URL handling.
_CONTROL_CHARS = re.compile(r"[\x00-\x1F\x7F]")

#: Limits for the interactive login payload.
MAX_LOGIN_FIELDS = 50
MAX_LOGIN_FIELD_NAME_LENGTH = 64
MAX_LOGIN_VALUE_LENGTH = 65536

#: Limits for ``MusicTrack`` text fields.
MAX_TRACK_TEXT_LENGTH = 512
MAX_TRACK_LYRIC_LENGTH = 100_000
MAX_MEDIA_URL_LENGTH = 2048
MAX_TRACK_PATH_LENGTH = 1024
MAX_PLATFORM_LENGTH = 64
MAX_LEVEL_LENGTH = 64

#: Limits for client-supplied per-track HTTP headers.
MAX_HEADER_COUNT = 32
MAX_HEADER_NAME_LENGTH = 64
MAX_HEADER_VALUE_LENGTH = 2048
_HEADER_NAME_CHARS = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
MAX_TRACK_SOURCES = 32


def _has_surrogate(value: str) -> bool:
    """Return ``True`` when ``value`` holds an unpaired surrogate code point.

    JSON permits lone ``\\ud800``-style escapes (and Pydantic's ``str | None``
    union accepts them without the unicode check a bare ``str`` field gets).
    Such a string cannot be UTF-8 encoded, so echoing it back in a response
    would raise inside the JSON encoder and turn a malformed request into a
    ``500``. Valid surrogate pairs are combined into one code point by the JSON
    parser, so only genuinely unpaired values are rejected.
    """
    return any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def _validate_text(
    value: str,
    field: str,
    max_length: int,
    *,
    allow_controls: bool = False,
) -> str:
    """Common bound checks shared by the public validators below."""
    if _has_surrogate(value):
        raise ValueError(f"{field} must not contain unpaired surrogate characters")
    if len(value) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters long")
    if not allow_controls and _CONTROL_CHARS.search(value):
        raise ValueError(f"{field} must not contain control characters")
    return value


def _clean_text(
    value: str,
    field: str,
    max_length: int,
    *,
    allow_blank: bool = False,
    allow_controls: bool = False,
    strip: bool = True,
) -> str:
    """Strip and validate a short, single-line text value."""
    cleaned = value.strip() if strip else value
    if not cleaned.strip() and not allow_blank:
        raise ValueError(f"{field} must not be empty or whitespace-only")
    return _validate_text(
        cleaned,
        field,
        max_length,
        allow_controls=allow_controls,
    )


#: Characters a ``book_id`` may never contain. ``book_id`` is joined onto the
#: raw-data directory to place the chapter cache and media files
#: (``raw_data/<site>/<book_id>``); path separators, the Windows drive colon
#: and the other Windows-reserved characters would let a crafted request write
#: outside that tree, and control characters are never part of a real id.
_BOOK_ID_INVALID_CHARS = re.compile(r'[/\\<>:"|?*\x00-\x1F\x7F]')

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
    if _has_surrogate(cleaned):
        raise ValueError("book_id must not contain unpaired surrogate characters")
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
    # Keep the request validator and the queue/client boundary in agreement:
    # Windows device names (``CON``, ``NUL`` …) pass the character checks but
    # do not name a file, and the queue now refuses them as well.
    if not is_safe_path_component(cleaned):
        raise ValueError("book_id must be a usable directory name")
    return cleaned


#: Request field type for ``book_id`` values.
BookId = Annotated[str, AfterValidator(validate_book_id)]


def validate_site_key(value: str) -> str:
    """
    Normalize one site key (``"qidian"``, ``"mangg_net"`` …).

    Site keys select plugin modules and end up in log lines. Path separators,
    whitespace, control characters and arbitrarily long strings are rejected;
    the registry lower-cases the value again, so the original case is kept for
    the response/echo. Shared with the ``sites`` query parser.
    """
    cleaned = _clean_text(value, "site", MAX_SITE_KEY_LENGTH)
    if not _SITE_KEY_CHARS.fullmatch(cleaned):
        raise ValueError("site must contain only letters, digits, '_', '-' and '.'")
    # Windows strips trailing dots/spaces and treats device names specially;
    # the queue uses the same check before creating ``raw_data/<site>``.
    if not is_safe_path_component(cleaned):
        raise ValueError("site must contain only letters, digits, '_', '-' and '.'")
    return cleaned


def validate_keyword(value: str) -> str:
    """Reject blank, control-containing or overlong search keywords."""
    # Keywords are forwarded verbatim (the registry/search cache treat them as
    # opaque), so only the bounds are enforced -- unlike titles, no stripping.
    return _clean_text(value, "keyword", MAX_KEYWORD_LENGTH, strip=False)


def validate_title(value: str | None) -> str | None:
    """Normalize an optional display title; blank becomes ``None``."""
    if value is None:
        return None
    cleaned = _clean_text(value, "title", MAX_TITLE_LENGTH, allow_blank=True)
    return cleaned or None


def validate_category_id(value: str | None) -> str | None:
    """Normalize an optional browse category id; blank means "ranking page"."""
    if value is None:
        return None
    cleaned = _clean_text(
        value, "category_id", MAX_CATEGORY_ID_LENGTH, allow_blank=True
    )
    return cleaned or None


def validate_platform(value: str) -> str:
    """Bound a music platform key without changing its historical semantics.

    An empty value is rejected (the models previously used ``min_length=1``).
    Whitespace is intentionally *not* stripped: ``is_all_platforms`` treats a
    blank-but-non-empty value such as ``" "`` as "all", and the embedded
    transport documents the same behaviour. Only pathological values are
    rejected.
    """
    if not value:
        raise ValueError("platform must not be empty")
    if len(value) > MAX_PLATFORM_LENGTH:
        raise ValueError(
            f"platform must be at most {MAX_PLATFORM_LENGTH} characters long"
        )
    return _validate_text(value, "platform", MAX_PLATFORM_LENGTH)


def validate_track_text(value: str) -> str:
    """Bound one free-form ``MusicTrack`` text field (name, id, artist …)."""
    return _validate_text(value, "value", MAX_TRACK_TEXT_LENGTH)


def validate_track_path(value: str) -> str:
    """Bound a local media path echoed back for playlist export."""
    if not value:
        return ""
    return _clean_text(value, "path", MAX_TRACK_PATH_LENGTH, allow_blank=True)


def validate_lyric(value: str) -> str:
    """Bound a lyric payload; newlines/tabs are legitimate here."""
    return _validate_text(value, "lyric", MAX_TRACK_LYRIC_LENGTH, allow_controls=True)


def validate_media_url(value: str) -> str:
    """
    Reject non-http(s) media URLs.

    The downloader fetches ``download_url``/``cover_url`` server-side, so a
    ``file:``, ``ftp:`` or ``javascript:`` value would make the server reach
    outside HTTP(S). Relative values are left alone: they cannot be fetched
    either, and providers occasionally echo them. Host filtering (private
    ranges) belongs to the downloader, not the request schema.
    """
    url = value.strip()
    if not url:
        return ""
    if _has_surrogate(url):
        raise ValueError("URL must not contain unpaired surrogate characters")
    if len(url) > MAX_MEDIA_URL_LENGTH:
        raise ValueError(f"URL must be at most {MAX_MEDIA_URL_LENGTH} characters long")
    if _CONTROL_CHARS.search(url):
        raise ValueError("URL must not contain control characters")
    scheme = urlsplit(url).scheme.lower()
    if scheme and scheme not in {"http", "https"}:
        raise ValueError("URL scheme must be http or https")
    return url


def validate_optional_level(value: str | None) -> str | None:
    """Normalize an optional audio level; blank means the engine default."""
    if value is None:
        return None
    cleaned = _clean_text(value, "level", MAX_LEVEL_LENGTH, allow_blank=True)
    return cleaned or None


def validate_optional_path(value: str | None) -> str | None:
    """Normalize an optional output path; the route still confines it."""
    if value is None:
        return None
    cleaned = _clean_text(value, "path", MAX_TRACK_PATH_LENGTH, allow_blank=True)
    return cleaned or None


def validate_http_headers(value: dict[str, str]) -> dict[str, str]:
    """Bound client-supplied per-track request headers."""
    if len(value) > MAX_HEADER_COUNT:
        raise ValueError(
            f"http_headers must contain at most {MAX_HEADER_COUNT} entries"
        )
    for name, header_value in value.items():
        if (
            not name
            or len(name) > MAX_HEADER_NAME_LENGTH
            or not _HEADER_NAME_CHARS.fullmatch(name)
        ):
            raise ValueError("http_headers contains an invalid header name")
        if _has_surrogate(header_value):
            raise ValueError(
                "http_headers values must not contain unpaired surrogate characters"
            )
        if len(header_value) > MAX_HEADER_VALUE_LENGTH:
            raise ValueError(
                "http_headers values must be at most "
                f"{MAX_HEADER_VALUE_LENGTH} characters long"
            )
        if "\r" in header_value or "\n" in header_value:
            raise ValueError("http_headers values must not contain newlines")
    return value


def validate_sources(value: list[str]) -> list[str]:
    """Bound the aggregate-search ``sources`` list."""
    if len(value) > MAX_TRACK_SOURCES:
        raise ValueError(f"sources must contain at most {MAX_TRACK_SOURCES} entries")
    for item in value:
        _validate_text(item, "sources", MAX_TRACK_TEXT_LENGTH)
    return value


#: A normalized site key.
SiteKey = Annotated[str, AfterValidator(validate_site_key)]
#: A bounded, non-blank search keyword.
Keyword = Annotated[str, AfterValidator(validate_keyword)]
#: An optional display title (blank collapses to ``None``).
Title = Annotated[str | None, AfterValidator(validate_title)]
#: An optional browse category id (blank collapses to ``None``).
CategoryId = Annotated[str | None, AfterValidator(validate_category_id)]
#: A bounded music platform key.
Platform = Annotated[str, AfterValidator(validate_platform)]
#: A bounded single-line track text field.
TrackText = Annotated[str, AfterValidator(validate_track_text)]
#: A local media path (no control characters, bounded length).
TrackPath = Annotated[str, AfterValidator(validate_track_path)]
#: A lyric payload (newlines allowed).
TrackLyric = Annotated[str, AfterValidator(validate_lyric)]
#: An http(s)-only media URL (empty allowed).
TrackUrl = Annotated[str, AfterValidator(validate_media_url)]
#: Bounded client-supplied request headers.
HttpHeaders = Annotated[dict[str, str], AfterValidator(validate_http_headers)]
#: Bounded aggregate-search source list.
SourceList = Annotated[list[str], AfterValidator(validate_sources)]
#: An optional audio level (blank collapses to ``None``).
OptionalLevel = Annotated[str | None, AfterValidator(validate_optional_level)]
#: An optional filesystem path (blank collapses to ``None``).
OptionalPath = Annotated[str | None, AfterValidator(validate_optional_path)]


class SearchRequest(BaseModel):
    """Body of ``POST /api/v1/search``."""

    keyword: Keyword
    sites: list[SiteKey] | None = Field(default=None, max_length=MAX_SEARCH_SITES)
    per_site_limit: int = Field(default=5, ge=1, le=MAX_PER_SITE_LIMIT)
    timeout: float = Field(
        default=5.0,
        gt=0,
        le=MAX_SEARCH_TIMEOUT,
        allow_inf_nan=False,
    )
    sort: SearchSortKey = "quality"
    order: Literal["asc", "desc"] | None = None
    limit: int | None = Field(default=None, ge=1, le=MAX_SEARCH_LIMIT)
    #: Fold same-book results into one merged entry per book: each entry is
    #: the book's best source plus a ``sources`` list of every site carrying
    #: it (best first).  Off by default so the response shape stays the flat
    #: one existing clients (Android) expect.
    group: bool = False
    #: Optional local filters applied to the ranked rows before grouping and
    #: ``limit``.  A row whose field cannot be parsed is dropped only while
    #: the corresponding filter is set; omitting all three keeps every row.
    min_words: int | None = Field(
        default=None,
        ge=0,
        le=MAX_SEARCH_WORDS,
        description=(
            "Drop ranked rows whose word count is below this value. Rows "
            "with an unparseable word count are dropped only while this "
            "filter is set."
        ),
    )
    max_words: int | None = Field(
        default=None,
        ge=0,
        le=MAX_SEARCH_WORDS,
        description=(
            "Drop ranked rows whose word count is above this value. Rows "
            "with an unparseable word count are dropped only while this "
            "filter is set."
        ),
    )
    #: Inclusive lower bound on ``update_date`` (``YYYY-MM-DD``); a malformed
    #: date is a ``422``.
    updated_after: date | None = Field(
        default=None,
        description=(
            "Inclusive lower bound on ``update_date`` (``YYYY-MM-DD``). A "
            "malformed date is rejected with ``422``."
        ),
    )

    @field_validator("sites")
    @classmethod
    def _dedupe_sites(cls, value: list[str] | None) -> list[str] | None:
        """Drop repeated sites (the registry is case-insensitive)."""
        if value is None:
            return None
        unique: list[str] = []
        seen: set[str] = set()
        for site in value:
            key = site.lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(site)
        return unique


class BrowseRequest(BaseModel):
    """Body of ``POST /api/v1/browse``."""

    site: SiteKey
    #: Omitted means "the site's ranking page"; set to a category id to list
    #: that category instead.
    category_id: CategoryId = None
    gender: Literal["male", "female", "1", "2"] = "male"
    limit: int = Field(default=20, ge=1, le=100)


class TaskCreateRequest(BaseModel):
    """Body of ``POST /api/v1/tasks``."""

    title: Title = None
    site: SiteKey
    book_id: BookId = Field(min_length=1)


class LoginSubmitRequest(BaseModel):
    """
    Body of ``POST /api/v1/login/requests/{req_id}``.

    ``values`` maps login-field names to the raw strings a user would type.
    ``type=cookie`` fields carry the unpasted cookie string (e.g.
    ``"key=value; other=value"``); the server parses it like the Web UI did.
    """

    values: dict[str, str] = Field(default_factory=dict)

    @field_validator("values")
    @classmethod
    def _validate_values(cls, values: dict[str, str]) -> dict[str, str]:
        """Bound the field count and each raw value.

        The values are later merged into cookies/headers, so CR/LF (header
        injection) and NUL are rejected. The value itself is never echoed in
        the error message: it may be a password.
        """
        if len(values) > MAX_LOGIN_FIELDS:
            raise ValueError(f"values must contain at most {MAX_LOGIN_FIELDS} fields")
        for name, raw in values.items():
            if not name or len(name) > MAX_LOGIN_FIELD_NAME_LENGTH:
                raise ValueError("values contains an invalid field name")
            if _CONTROL_CHARS.search(name):
                raise ValueError("values contains an invalid field name")
            if _has_surrogate(raw):
                raise ValueError(
                    "values must not contain unpaired surrogate characters"
                )
            if len(raw) > MAX_LOGIN_VALUE_LENGTH:
                raise ValueError(
                    f"values must be at most {MAX_LOGIN_VALUE_LENGTH} characters long"
                )
            if any(char in raw for char in "\r\n\x00"):
                raise ValueError("values must not contain newlines or NUL")
        return values


class WatchlistAddRequest(BaseModel):
    """Body of ``POST /api/v1/watchlist``."""

    site: SiteKey
    book_id: BookId = Field(min_length=1)
    title: Title = None


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

    platform: Platform = "all"
    keyword: Keyword
    limit: int = Field(default=20, ge=1, le=MAX_MUSIC_SEARCH_LIMIT)
    offset: int = Field(default=0, ge=0, le=MAX_MUSIC_OFFSET)


class MusicTrack(BaseModel):
    """
    Serialised :class:`novel_downloader.apps.music.providers.base.Track`.

    Clients normally send back track dictionaries they received from
    ``POST /api/v1/music/search`` unchanged.
    """

    provider: TrackText = ""
    id: TrackText = ""
    name: TrackText = "未知歌曲"
    artists: TrackText = "未知歌手"
    album: TrackText = "未知专辑"
    duration_s: int = 0
    cover_url: TrackUrl = ""
    track_no: int = 0
    ext: TrackText = ""
    vip: bool = False
    download_url: TrackUrl = ""
    http_headers: HttpHeaders = Field(default_factory=dict)
    #: Local file populated by the downloader (or by the caller); used by
    #: ``POST /api/v1/music/export``.
    path: TrackPath = ""
    lyric: TrackLyric = ""
    #: Platform keys that carry this song, best first (aggregate search only).
    sources: SourceList = Field(default_factory=list)
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

    platform: Platform = "all"
    tracks: list[MusicTrack] = Field(min_length=1, max_length=MAX_MUSIC_TRACKS)
    search_id: MusicSearchId | None = None
    level: OptionalLevel = None
    out_dir: OptionalPath = None

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

    platform: Platform = "all"
    tracks: list[MusicTrack] = Field(min_length=1, max_length=MAX_MUSIC_TRACKS)
    out_path: OptionalPath = None
