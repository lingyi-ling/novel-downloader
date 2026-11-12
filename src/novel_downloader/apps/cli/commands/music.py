#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.music
----------------------------------------

CLI entry points for the optional music engine.

The subcommands are thin wrappers around :mod:`novel_downloader.apps.music`:
list platforms, search a platform and download search results.  The engine's
third-party dependencies live in the optional ``music`` extra, so every run
starts with a dependency probe and prints an install hint instead of a
traceback when it is missing.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import re
import sys
import tempfile
from argparse import ArgumentParser, Namespace, _SubParsersAction
from dataclasses import dataclass
from typing import Any

from novel_downloader.apps.cli import ui
from novel_downloader.infra.i18n import t

from .base import EXIT_FAILURE, EXIT_USAGE, Command, localize_help

#: Quality levels understood by the engine (NetEase names, the common subset).
_LEVELS = ("hires", "lossless", "exhigh", "higher", "standard")

#: Rows per page in the interactive result selector.
_PER_PAGE = 10

#: Handy aliases for the platform keys printed by ``music platforms``.  Keys
#: are compared case-insensitively; a platform's display label (``网易云音乐``
#: or ``国内 · 网易云音乐``) is accepted as well, so users can paste either.
_PLATFORM_ALIASES: dict[str, str] = {
    "163": "netease",
    "wangyiyun": "netease",
    "neteasecloudmusic": "netease",
    "网易云": "netease",
    "网易云音乐": "netease",
    "qqmusic": "qq",
    "qq音乐": "qq",
    "kugoumusic": "kugou",
    "酷狗": "kugou",
    "酷狗音乐": "kugou",
    "kuwomusic": "kuwo",
    "酷我": "kuwo",
    "酷我音乐": "kuwo",
    "migu music": "migu",
    "咪咕": "migu",
    "咪咕音乐": "migu",
    "qianqianmusic": "qianqian",
    "baidu": "qianqian",
    "千千": "qianqian",
    "千千音乐": "qianqian",
    "b站": "bilibili",
    "哔哩哔哩": "bilibili",
    "汽水": "soda",
    "汽水音乐": "soda",
    "apple music": "apple",
    "applemusic": "apple",
    "youtube music": "youtube",
    "ytmusic": "youtube",
}


class _MusicUnavailable(Exception):
    """Raised when the optional music dependencies cannot be imported."""


@dataclass
class _MusicEngine:
    """Lazily imported pieces of the optional music engine."""

    download_options: Any
    download_many: Any
    available_labels: Any
    create: Any
    platform_note: Any
    default_download_dir: Any
    settings: Any
    login_expired: type[BaseException]
    health_check: Any
    export_m3u: Any
    download_mixed: Any

    def labels(self) -> list[tuple[str, str]]:
        """Return ``(key, label)`` pairs for every registered platform."""
        return list(self.available_labels())

    def platform_keys(self) -> list[str]:
        """Return the registered platform keys, sorted."""
        return sorted(key for key, _label in self.labels())


def _load_engine() -> _MusicEngine:
    """
    Import the optional music engine.

    :raises _MusicUnavailable: When a dependency from the ``music`` extra is
        missing (or the engine fails to import for another reason).
    """
    try:
        import mutagen  # noqa: F401
        import qrcode  # noqa: F401
        import websocket  # noqa: F401

        from novel_downloader.apps.music import settings as music_settings
        from novel_downloader.apps.music.downloader import (
            DownloadOptions,
            download_many,
            download_mixed,
            export_m3u,
        )
        from novel_downloader.apps.music.paths import default_download_dir
        from novel_downloader.apps.music.providers import (
            available_labels,
            create,
            platform_note,
        )
        from novel_downloader.apps.music.providers.health import health_check
        from novel_downloader.apps.music.weapi import LoginExpired
    except Exception as exc:  # optional dependency guard
        raise _MusicUnavailable(str(exc)) from exc

    return _MusicEngine(
        download_options=DownloadOptions,
        download_many=download_many,
        available_labels=available_labels,
        create=create,
        platform_note=platform_note,
        default_download_dir=default_download_dir,
        settings=music_settings,
        login_expired=LoginExpired,
        health_check=health_check,
        export_m3u=export_m3u,
        download_mixed=download_mixed,
    )


def _require_engine() -> _MusicEngine:
    """Load the engine or exit with a friendly install hint."""
    try:
        return _load_engine()
    except _MusicUnavailable as exc:
        ui.error(
            t(
                "Music support is unavailable because optional dependencies "
                "are missing. Install them with: "
                'pip install "novel-downloader[music]" ({error})'
            ).format(error=exc)
        )
        raise SystemExit(EXIT_FAILURE) from exc


def _create_provider(engine: _MusicEngine, platform: str) -> Any:
    """Create a provider, exiting with a clear message on failure."""
    try:
        return engine.create(platform)
    except KeyError as exc:
        ui.error(
            t("Unknown music platform: {platform}. Available: {keys}").format(
                platform=platform,
                keys=", ".join(engine.platform_keys()),
            )
        )
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        ui.error(t("Could not start this platform: {error}").format(error=exc))
        raise SystemExit(EXIT_FAILURE) from exc


def _print_platform_note(engine: _MusicEngine, platform: str) -> None:
    """Show the platform-specific usage note, when one is defined."""
    note = str(engine.platform_note(platform) or "")
    if note:
        ui.warn(note)


def _login_expired_hint() -> None:
    """Explain what to do when the NetEase session has expired."""
    ui.error(
        t(
            "NetEase login has expired. Log in again in a browser to refresh "
            "the music session (or run 'novel-cli login fanqienovel' for "
            "novel sites), then retry."
        )
    )


def _search(
    engine: _MusicEngine,
    provider: Any,
    keyword: str,
    limit: int,
    *,
    show_status: bool,
) -> tuple[list[Any], int]:
    """Run a provider search, exiting with a readable error on failure."""
    try:
        if show_status:
            with ui.status(t("Searching for '{keyword}'...").format(keyword=keyword)):
                tracks, total = provider.search(keyword, limit=limit)
        else:
            tracks, total = provider.search(keyword, limit=limit)
    except Exception as exc:
        if isinstance(exc, engine.login_expired):
            _login_expired_hint()
        else:
            ui.error(t("Search failed: {err}").format(err=exc))
        raise SystemExit(EXIT_FAILURE) from exc

    result = list(tracks)[: max(1, limit)]
    return result, int(total or len(result))


def _format_duration(seconds: int) -> str:
    """Format a track duration as ``m:ss`` or ``h:mm:ss``."""
    if seconds <= 0:
        return "-"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _search_all(
    keyword: str,
    limit: int,
    *,
    show_status: bool,
) -> tuple[list[Any], int, list[Any]]:
    """Cross-platform search; returns ``(merged_tracks, total, outcomes)``."""
    from novel_downloader.apps.music.aggregate import search_all_isolated

    try:
        if show_status:
            with ui.status(
                t("Searching every platform for '{keyword}'...").format(keyword=keyword)
            ):
                result = search_all_isolated(keyword, limit=limit)
        else:
            result = search_all_isolated(keyword, limit=limit)
    except Exception as exc:
        ui.error(t("Search failed: {err}").format(err=exc))
        raise SystemExit(EXIT_FAILURE) from exc
    return list(result.tracks), int(result.total), list(result.platforms)


def _is_all_platforms(platform: str) -> bool:
    """Whether the request asks for every platform (``all`` or empty)."""
    from novel_downloader.apps.music.aggregate import is_all_platforms

    return is_all_platforms(platform)


def _every_platform_failed(outcomes: list[Any]) -> bool:
    """
    Whether every platform in an aggregate search failed.

    A genuinely empty result set (every platform answered with zero songs) is
    a successful search; every platform erroring is not, and must not report
    exit code 0 as if nothing had gone wrong.
    """
    return bool(outcomes) and all(
        not bool(getattr(outcome, "ok", False)) for outcome in outcomes
    )


def _platform_lookup(engine: _MusicEngine) -> dict[str, str]:
    """
    Build a case-insensitive lookup from keys, labels and aliases to keys.

    ``music platforms`` prints keys like ``netease`` and localized labels like
    ``国内 · 网易云音乐``.  Users should be able to pass either (and common
    aliases such as ``163``) instead of guessing the exact spelling.
    """
    lookup: dict[str, str] = {}
    labels = engine.labels()
    for key, label in labels:
        lookup.setdefault(key.casefold(), key)
        text = str(label or "").strip()
        if not text:
            continue
        lookup.setdefault(text.casefold(), key)
        # ``国内 · 网易云音乐`` -> also accept the part after the group prefix.
        tail = text.split("·", 1)[-1].strip()
        if tail and tail != text:
            lookup.setdefault(tail.casefold(), key)

    registered = {key for key, _label in labels}
    for alias, target in _PLATFORM_ALIASES.items():
        if target in registered:
            lookup.setdefault(alias.casefold(), target)
    return lookup


def _normalize_platform(engine: _MusicEngine, platform: object) -> str:
    """
    Resolve a user-supplied platform name to its canonical registry key.

    Empty input and ``all`` (case-insensitive) become :data:`ALL_PLATFORM_KEY`
    (``"all"``).  Unknown names are returned unchanged (trimmed) so the
    caller's ``create`` path still reports the usual "unknown platform" usage
    error -- showing exactly what the user typed -- while listing the
    registered keys.
    """
    text = str(platform or "").strip()
    if _is_all_platforms(text):
        return "all"
    return _platform_lookup(engine).get(text.casefold(), text)


def _unknown_platform(engine: _MusicEngine, platform: str) -> None:
    """Report an unknown platform and exit with the usage status."""
    ui.error(
        t("Unknown music platform: {platform}. Available: {keys}").format(
            platform=platform,
            keys=", ".join(engine.platform_keys()),
        )
    )
    raise SystemExit(EXIT_USAGE)


def _sources_text(track: Any) -> str:
    """Compact ``netease,qq`` source list for CLI tables."""
    sources = [str(key) for key in (getattr(track, "sources", None) or []) if key]
    if not sources:
        provider = str(getattr(track, "provider", "") or "")
        return provider
    if len(sources) <= 3:
        return ",".join(sources)
    return ",".join(sources[:3]) + f"+{len(sources) - 3}"


def _track_rows(tracks: list[Any], *, show_sources: bool = False) -> list[list[str]]:
    """Build the table rows shared by the search and picker views."""
    rows: list[list[str]] = []
    for index, track in enumerate(tracks, 1):
        row = [
            str(index),
            str(track.name),
            str(track.artists),
            str(track.album),
            _format_duration(int(track.duration_s or 0)),
            t("VIP") if track.vip else "-",
        ]
        if show_sources:
            row.append(_sources_text(track))
        rows.append(row)
    return rows


def _render_tracks(tracks: list[Any], *, show_sources: bool = False) -> None:
    """Print the search result table."""
    headers = [t("#"), t("Title"), t("Artist"), t("Album"), t("Duration"), t("VIP")]
    if show_sources:
        headers.append(t("Sources"))
    ui.render_table(
        t("Search results"),
        headers,
        _track_rows(tracks, show_sources=show_sources),
    )


def _track_payload(index: int, track: Any) -> dict[str, Any]:
    """Serialise one track for ``--json`` output."""
    return {
        "index": index,
        "id": str(track.id),
        "name": str(track.name),
        "artists": str(track.artists),
        "album": str(track.album),
        "duration_s": int(track.duration_s or 0),
        "vip": bool(track.vip),
        "platform": str(getattr(track, "provider", "")),
        "sources": [str(key) for key in (getattr(track, "sources", None) or []) if key],
    }


def _parse_indices(value: str, total: int) -> list[int] | None:
    """Parse ``1,3,5`` into validated 1-based indices."""
    try:
        indices = sorted(
            {int(part.strip()) for part in value.split(",") if part.strip()}
        )
    except ValueError:
        return None
    if not indices or any(index < 1 or index > total for index in indices):
        return None
    return indices


def _select_interactive(tracks: list[Any]) -> list[Any]:
    """Show a paged picker and return the chosen tracks (empty = cancel)."""
    total = len(tracks)
    total_pages = max(1, (total + _PER_PAGE - 1) // _PER_PAGE)
    page = 1
    rows = _track_rows(tracks)

    while True:
        start = (page - 1) * _PER_PAGE + 1
        end = min(page * _PER_PAGE, total)
        ui.render_table(
            t("Search Results · Page {page}/{total_pages}").format(
                page=page, total_pages=total_pages
            ),
            [t("#"), t("Title"), t("Artist"), t("Album"), t("Duration"), t("VIP")],
            rows[start - 1 : end],
        )

        choice = (
            ui.prompt(
                t(
                    "Enter numbers (e.g. 1,3,5), 'a' for all, 'n' next, 'p' previous "
                    "(Enter to cancel)"
                )
            )
            .strip()
            .lower()
        )

        if choice == "":
            return []
        if choice == "a":
            return list(tracks)
        if choice == "n" and page < total_pages:
            page += 1
            continue
        if choice == "p" and page > 1:
            page -= 1
            continue
        if choice in {"n", "p"}:
            continue

        selected = _parse_indices(choice, total)
        if selected is None:
            ui.warn(t("Invalid input."))
            continue
        return [tracks[index - 1] for index in selected]


def _pick_tracks(tracks: list[Any], pick: str) -> list[Any]:
    """Resolve ``--pick`` values (1-based indices or song IDs) to tracks."""
    selected: list[Any] = []
    seen: set[int] = set()
    by_id = {str(track.id): index for index, track in enumerate(tracks, 1)}

    for raw in pick.split(","):
        token = raw.strip()
        if not token:
            continue

        index: int | None = None
        if token.isdigit():
            candidate = int(token)
            if 1 <= candidate <= len(tracks):
                index = candidate
        if index is None:
            index = by_id.get(token)
        if index is None:
            ui.error(
                t("No search result matches --pick value '{value}'.").format(
                    value=token
                )
            )
            raise SystemExit(EXIT_USAGE)

        if index not in seen:
            seen.add(index)
            selected.append(tracks[index - 1])

    return selected


def _is_logged_in(provider: Any) -> bool:
    """Best-effort login check that never fails the command."""
    try:
        return bool(provider.is_logged_in())
    except Exception:
        return False


class MusicCmd(Command):
    name = "music"
    help = t("Search and download songs from music platforms.")

    @classmethod
    def register(cls, subparsers: _SubParsersAction[ArgumentParser]) -> None:
        parser = subparsers.add_parser(
            cls.name, help=cls.help, description=cls.help, add_help=False
        )
        localize_help(parser)
        sub = parser.add_subparsers(
            dest="action", required=True, title=t("Subcommands")
        )

        for subcmd in (
            MusicPlatformsCmd,
            MusicSearchCmd,
            MusicDownloadCmd,
            MusicExportM3uCmd,
            MusicHealthCmd,
        ):
            subcmd.register(sub)

    @classmethod
    def run(cls, args: Namespace) -> None:
        raise NotImplementedError("MusicCmd should not be executed directly")


class MusicPlatformsCmd(Command):
    name = "platforms"
    help = t("List available music platforms.")

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--json",
            action="store_true",
            help=t("Print machine-readable JSON on stdout and exit."),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        engine = _require_engine()
        labels = engine.labels()
        if getattr(args, "json", False):
            # ``ui.emit`` (not ``print``): platform labels are localized (e.g.
            # "国内 · 网易云音乐"), and a redirected Windows stdout uses a
            # legacy code page where the raw ``print`` path raises
            # ``UnicodeEncodeError``. ``emit`` falls back to UTF-8 bytes.
            ui.emit(json.dumps(dict(labels), ensure_ascii=False))
            return
        if not labels:
            ui.warn(t("No music platforms are available."))
            return

        rows = [
            [key, label, str(engine.platform_note(key) or "-")] for key, label in labels
        ]
        ui.render_table(
            t("Music platforms"),
            [t("Platform"), t("Name"), t("Note")],
            rows,
        )


class MusicSearchCmd(Command):
    name = "search"
    help = t("Search songs on a music platform.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument("keyword", help=t("Search keyword"))
        parser.add_argument(
            "--platform",
            default="all",
            metavar="KEY",
            help=t(
                "Music platform key, or 'all' to search every platform (default: all)."
            ),
        )
        parser.add_argument(
            "--limit",
            "-l",
            type=int,
            default=20,
            metavar="N",
            help=t("Maximum number of results (default: 20)."),
        )
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help=t("Print search results as JSON on stdout and exit."),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        engine = _require_engine()
        keyword = str(args.keyword or "").strip()
        if not keyword:
            ui.error(t("No keyword entered."))
            raise SystemExit(EXIT_USAGE)

        platform = _normalize_platform(engine, args.platform)
        limit = max(1, int(args.limit))
        as_json = bool(args.as_json)

        if _is_all_platforms(platform):
            tracks, total, outcomes = _search_all(
                keyword, limit, show_status=not as_json
            )
        else:
            provider = _create_provider(engine, platform)
            _print_platform_note(engine, platform)
            tracks, total = _search(
                engine,
                provider,
                keyword,
                limit,
                show_status=not as_json,
            )
            outcomes = []

        if as_json:
            payload: dict[str, Any] = {
                "platform": platform or "all",
                "keyword": keyword,
                "total": total,
                "tracks": [
                    _track_payload(index, track)
                    for index, track in enumerate(tracks, 1)
                ],
            }
            if outcomes:
                payload["platforms"] = [item.as_dict() for item in outcomes]
            # One line: a consumer can ``json.loads`` the whole stdout even
            # when the command is run through a pipe or a log collector.
            ui.emit(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            if _every_platform_failed(outcomes):
                raise SystemExit(EXIT_FAILURE)
            return

        # Per-platform errors belong on screen even when the merged list is
        # empty; only then can the user tell "nothing matches" apart from
        # "every platform is down".
        for outcome in outcomes:
            if not outcome.ok:
                ui.warn(
                    t("Platform {platform} failed: {error}").format(
                        platform=outcome.platform, error=outcome.error
                    )
                )

        if not tracks:
            if _every_platform_failed(outcomes):
                raise SystemExit(EXIT_FAILURE)
            ui.warn(t("No results found for '{keyword}'.").format(keyword=keyword))
            return

        ui.info(
            t("Found {count} result(s) for '{keyword}'.").format(
                count=len(tracks), keyword=keyword
            )
        )
        _render_tracks(tracks, show_sources=_is_all_platforms(platform))


class MusicDownloadCmd(Command):
    name = "download"
    help = t("Search and download songs from a music platform.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "query",
            nargs="?",
            metavar="KEYWORD",
            help=t("Keyword to search for before downloading."),
        )
        parser.add_argument(
            "--keyword",
            metavar="KEYWORD",
            help=t("Keyword to search for (alternative to the positional form)."),
        )
        parser.add_argument(
            "--platform",
            default="all",
            metavar="KEY",
            help=t(
                "Music platform key, or 'all' to search every platform (default: all)."
            ),
        )
        parser.add_argument(
            "--limit",
            "-l",
            type=int,
            default=20,
            metavar="N",
            help=t("Maximum number of results (default: 20)."),
        )
        parser.add_argument(
            "--level",
            choices=_LEVELS,
            default=None,
            help=t(
                "Audio quality level (default: from music settings, usually exhigh)."
            ),
        )
        parser.add_argument(
            "--out",
            metavar="DIR",
            help=t("Output directory (default: from music settings)."),
        )
        parser.add_argument(
            "--pick",
            metavar="N[,N|ID]",
            help=t(
                "Download the given search result index/indices (1-based) or "
                "song ID(s) without prompting."
            ),
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help=t("Download every search result without prompting."),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        engine = _require_engine()
        keyword = str(args.keyword or args.query or "").strip()
        if not keyword:
            ui.error(t("No keyword entered."))
            raise SystemExit(EXIT_USAGE)

        platform = _normalize_platform(engine, args.platform)
        limit = max(1, int(args.limit))
        settings = engine.settings.load_settings()
        out_dir = _expand_path(
            str(args.out or settings.get("out_dir") or engine.default_download_dir())
        )
        level = str(args.level or settings.get("level") or "exhigh")
        workers = max(1, int(settings.get("workers") or 3))

        all_platforms = _is_all_platforms(platform)
        provider = None
        outcomes: list[Any] = []
        if all_platforms:
            tracks, _total, outcomes = _search_all(keyword, limit, show_status=True)
        else:
            provider = _create_provider(engine, platform)
            _print_platform_note(engine, platform)
            tracks, _total = _search(engine, provider, keyword, limit, show_status=True)
        if not tracks:
            for outcome in outcomes:
                if not outcome.ok:
                    ui.warn(
                        t("Platform {platform} failed: {error}").format(
                            platform=outcome.platform, error=outcome.error
                        )
                    )
            if _every_platform_failed(outcomes):
                raise SystemExit(EXIT_FAILURE)
            ui.warn(t("No results found for '{keyword}'.").format(keyword=keyword))
            return

        ui.info(
            t("Found {count} result(s) for '{keyword}'.").format(
                count=len(tracks), keyword=keyword
            )
        )
        _render_tracks(tracks, show_sources=all_platforms)

        if args.pick:
            selected = _pick_tracks(tracks, str(args.pick))
        elif args.yes:
            selected = list(tracks)
        elif sys.stdin.isatty():
            selected = _select_interactive(tracks)
        else:
            ui.error(
                t("Non-interactive input requires --pick N or --yes to choose songs.")
            )
            raise SystemExit(EXIT_USAGE)

        if not selected:
            ui.warn(t("Cancelled."))
            return

        if (
            platform == "netease"
            and provider is not None
            and not _is_logged_in(provider)
        ):
            ui.warn(
                t(
                    "NetEase is not logged in; member-only tracks may fail. "
                    "Log in via the browser to download them."
                )
            )

        options = engine.download_options(
            out_dir=out_dir,
            level=level,
            embed_tags=bool(settings.get("embed_tags", True)),
            embed_cover=bool(settings.get("embed_cover", True)),
            save_lyric=bool(settings.get("save_lyric", True)),
            split_folder=bool(settings.get("split_folder", True)),
            skip_existing=bool(settings.get("skip_existing", True)),
        )

        ui.info(t("Output directory: {path}").format(path=out_dir))
        ui.info(t("Audio quality: {level}").format(level=level))

        progress = ui.ProgressUI(prefix=t("Downloading"), unit=t("song"))
        progress.start()
        try:
            if all_platforms:
                ok, skip, fail, failed_pairs, _failed = engine.download_mixed(
                    engine.create,
                    selected,
                    options,
                    workers=workers,
                    on_tick=progress.update_sync,
                )
            else:
                ok, skip, fail, failed_pairs, _failed = engine.download_many(
                    provider,
                    selected,
                    options,
                    workers=workers,
                    on_tick=progress.update_sync,
                )
        except Exception as exc:
            if isinstance(exc, engine.login_expired):
                _login_expired_hint()
            else:
                ui.error(t("Download failed: {error}").format(error=exc))
            raise SystemExit(EXIT_FAILURE) from exc
        finally:
            progress.stop()

        for name, reason in failed_pairs:
            ui.warn(t("Failed: {name} ({reason})").format(name=name, reason=reason))
        failed_total = int(fail or 0)
        summary = t("Done: {ok} succeeded, {skip} skipped, {fail} failed").format(
            ok=ok, skip=skip, fail=fail
        )
        if failed_total > 0:
            # A batch with failed songs is a failed run: scripts must not see
            # exit 0 while some downloads were silently missing.
            ui.warn(summary)
            raise SystemExit(EXIT_FAILURE)
        ui.success(summary)


# ---------------------------------------------------------------------------
# export-m3u / health helpers
# ---------------------------------------------------------------------------

#: Characters that cannot appear in a Windows/Unix file name.
_ILLEGAL_FILENAME = re.compile(r'[<>:"/\|?*\x00-\x1f]')

#: Windows DOS device names that cannot be used as a file stem, even with an
#: extension: ``AUX.m3u8`` fails with ENOENT and ``CON.m3u8`` silently opens
#: the console device instead of creating a file.
_RESERVED_FILENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)

#: Extensions the downloader can write; used to find an already-downloaded
#: track when ``export-m3u`` gets a fresh search result (no ``Track.path``).
_AUDIO_EXTENSIONS = frozenset(
    {
        ".aac",
        ".aiff",
        ".alac",
        ".ape",
        ".flac",
        ".m4a",
        ".m4b",
        ".mp3",
        ".mp4",
        ".ogg",
        ".opus",
        ".wav",
        ".wma",
    }
)


def _expand_path(value: str) -> str:
    """Expand ``~``/environment variables in a user-supplied path."""
    return os.path.expandvars(os.path.expanduser(str(value)))


def _playlist_filename(keyword: str) -> str:
    """Build a safe ``<keyword>.m3u8`` file name."""
    stem = _ILLEGAL_FILENAME.sub("_", keyword).strip().rstrip(". ")
    if stem.split(".", 1)[0].upper() in _RESERVED_FILENAMES:
        stem = f"_{stem}"
    return f"{stem[:120] or 'playlist'}.m3u8"


def _default_playlist_path(keyword: str, out_dir: str) -> str:
    """Return the default playlist path inside the music output directory."""
    return os.path.join(out_dir, _playlist_filename(keyword))


def _write_playlist_atomic(
    engine: _MusicEngine, tracks: list[Any], out_path: str
) -> None:
    """
    Write the playlist through a sibling temp file, then atomically replace.

    ``export_m3u`` opens the destination directly.  A full disk or an I/O
    error halfway through would otherwise leave a truncated ``.m3u8`` at the
    user's path -- and, when replacing an existing playlist, destroy the
    previous good one.  Writing next to the target keeps the relative paths
    the exporter computes intact (they are relative to the playlist folder).

    The temp file keeps the destination's extension: ``export_m3u`` picks the
    encoding from the path suffix (``.m3u`` with non-ASCII content gets a
    UTF-8 BOM, ``.m3u8`` stays plain UTF-8).  A generic ``.tmp`` name would
    look like an unknown format and silently drop that BOM.
    """
    target = os.path.abspath(out_path)
    if os.path.isdir(target):
        # Opening a directory raises a platform-specific error (EACCES on
        # Windows); make the message unambiguous everywhere.
        raise IsADirectoryError(errno.EISDIR, "Is a directory", target)

    parent = os.path.dirname(target) or os.curdir
    os.makedirs(parent, exist_ok=True)
    suffix = os.path.splitext(target)[1]
    prefix = f".{os.path.basename(target)}."
    if len(prefix) > 120:
        # Stay inside filesystem component limits; only the suffix carries
        # meaning for the exporter, the prefix is for human recognition.
        prefix = f".{os.path.basename(target)[:100]}."
    fd, temp_path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=parent)
    os.close(fd)
    try:
        engine.export_m3u(tracks, temp_path)
        os.replace(temp_path, target)
    except BaseException:
        # Never leave the half-written temp file (or a partial target) behind.
        with contextlib.suppress(OSError):
            os.unlink(temp_path)
        raise


def _track_path(track: Any) -> str:
    """Return the local file path recorded on ``track``, if it is usable."""
    path = getattr(track, "path", "")
    return path.strip() if isinstance(path, str) else ""


def _split_playable(tracks: list[Any]) -> tuple[list[Any], list[Any]]:
    """
    Split search results into ``(tracks with a local path, skipped tracks)``.

    ``export_m3u`` needs the downloaded file location, so tracks that were
    never downloaded (no ``Track.path``) cannot be written to the playlist.
    """
    ready: list[Any] = []
    skipped: list[Any] = []
    for track in tracks:
        if _track_path(track):
            ready.append(track)
        else:
            skipped.append(track)
    return ready, skipped


def _find_downloaded_file(track: Any, out_dir: str, split_folder: bool) -> str:
    """
    Locate an already-downloaded file for a fresh search result.

    A search always returns new ``Track`` objects with an empty ``path``, so
    the playlist export needs to look the file up by the downloader's naming
    convention. Returns ``""`` when nothing matches.
    """
    from novel_downloader.apps.music.downloader import (
        DownloadOptions,
        build_dir,
        build_stem,
    )

    options = DownloadOptions(out_dir=out_dir, split_folder=split_folder)
    folder = build_dir(options, track)
    stem = build_stem(track)  # type: ignore[no-untyped-call]
    stem_key = os.path.normcase(stem)
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return ""

    matches: list[tuple[int, str, str]] = []
    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        base, ext = os.path.splitext(entry.name)
        if ext.lower() not in _AUDIO_EXTENSIONS:
            continue
        base_key = os.path.normcase(base)
        if base_key == stem_key:
            matches.append((0, entry.name, entry.path))
        elif base_key.startswith(stem_key + " ["):
            # ``build_stem [id].ext``: the fallback name for same-titled songs.
            matches.append((1, entry.name, entry.path))
    if not matches:
        return ""
    # Deterministic pick: the plain name first, then the id-suffixed fallback.
    return sorted(matches)[0][2]


def _resolve_playable(
    tracks: list[Any], out_dir: str, *, split_folder: bool
) -> tuple[list[Any], list[Any]]:
    """
    Fill ``track.path`` from the music library, then split playable tracks.

    ``export-m3u`` searches again instead of relying on an in-memory download
    session, so this is what makes the command usable: already downloaded
    files are matched by artist/album folder and ``name - artist`` stem.
    """
    for track in tracks:
        if _track_path(track):
            continue
        found = _find_downloaded_file(track, out_dir, split_folder)
        if found:
            try:
                track.path = found
            except (AttributeError, TypeError):  # read-only/odd track object
                continue
    return _split_playable(tracks)


class MusicExportM3uCmd(Command):
    name = "export-m3u"
    help = t("Export search results to an M3U playlist (no downloading).")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--keyword",
            "-k",
            required=True,
            metavar="KEYWORD",
            help=t("Keyword used to search for the tracks to add."),
        )
        parser.add_argument(
            "--platform",
            default="netease",
            metavar="KEY",
            help=t("Music platform key (default: netease)."),
        )
        parser.add_argument(
            "--limit",
            "-l",
            type=int,
            default=20,
            metavar="N",
            help=t("Maximum number of results (default: 20)."),
        )
        parser.add_argument(
            "--out",
            metavar="FILE",
            help=t("Playlist path (default: <output directory>/<keyword>.m3u8)."),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        engine = _require_engine()
        keyword = str(args.keyword or "").strip()
        if not keyword:
            ui.error(t("No keyword entered."))
            raise SystemExit(EXIT_USAGE)

        platform = _normalize_platform(engine, args.platform)
        limit = max(1, int(args.limit))

        provider = _create_provider(engine, platform)
        _print_platform_note(engine, platform)
        tracks, _total = _search(engine, provider, keyword, limit, show_status=True)
        if not tracks:
            ui.warn(t("No results found for '{keyword}'.").format(keyword=keyword))
            return

        ui.info(
            t("Found {count} result(s) for '{keyword}'.").format(
                count=len(tracks), keyword=keyword
            )
        )

        settings = engine.settings.load_settings()
        out_dir = _expand_path(
            str(settings.get("out_dir") or engine.default_download_dir())
        )
        split_folder = bool(settings.get("split_folder", True))

        ready, skipped = _resolve_playable(tracks, out_dir, split_folder=split_folder)
        if skipped:
            ui.warn(
                t(
                    "Skipped {count} track(s) without a local file path; only "
                    "already downloaded tracks are written to the playlist."
                ).format(count=len(skipped))
            )
        if not ready:
            ui.error(
                t(
                    "No matched track has a local file path; no playlist was "
                    "written. Download the tracks first."
                )
            )
            raise SystemExit(EXIT_FAILURE)

        out_path = _expand_path(
            str(args.out or _default_playlist_path(keyword, out_dir))
        )

        try:
            _write_playlist_atomic(engine, ready, out_path)
        except Exception as exc:
            ui.error(t("Could not write playlist: {error}").format(error=exc))
            raise SystemExit(EXIT_FAILURE) from exc

        ui.success(
            t("Exported {count} track(s) to {path}").format(
                count=len(ready), path=out_path
            )
        )


class MusicHealthCmd(Command):
    name = "health"
    help = t("Check whether music platforms can run a search.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--platform",
            metavar="KEY",
            help=t("Music platform key to check (default: every platform)."),
        )
        parser.add_argument(
            "--keyword",
            default="test",
            metavar="KEYWORD",
            help=t("Keyword used for the connectivity probe (default: test)."),
        )
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help=t("Print machine-readable JSON on stdout and exit."),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        engine = _require_engine()
        keyword = str(args.keyword or "test").strip() or "test"
        as_json = bool(getattr(args, "as_json", False))
        keys = engine.platform_keys()
        if not keys:
            if as_json:
                # Keep the promised machine-readable contract even when the
                # registry is empty: an empty document still parses.
                ui.emit(
                    json.dumps(
                        {"keyword": keyword, "platforms": [], "failed": 0},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
            else:
                ui.warn(t("No music platforms are available."))
            return

        requested_raw = str(args.platform or "").strip()
        if requested_raw and not _is_all_platforms(requested_raw):
            requested = _normalize_platform(engine, requested_raw)
            if requested not in keys:
                _unknown_platform(engine, requested)
            selected = [requested]
        else:
            selected = keys

        rows: list[list[str]] = []
        payload: list[dict[str, Any]] = []
        failed = 0

        def _probe(key: str) -> dict[str, Any]:
            # ``health_check`` already reports failures as data, but a mock or
            # a future provider can still raise; one platform must never take
            # the whole command (and its JSON document) down.
            try:
                result = engine.health_check(key, keyword)
                return dict(result or {})
            except Exception as exc:  # noqa: BLE001 - report as unhealthy
                return {
                    "ok": False,
                    "elapsed_ms": 0,
                    "error": str(exc) or type(exc).__name__,
                }

        # 并发探测：以前是逐站 15 秒串行，25 个平台要等好几分钟。
        from concurrent.futures import ThreadPoolExecutor

        status = (
            contextlib.nullcontext()
            if as_json
            else ui.status(
                t("Checking {count} platform(s)...").format(count=len(selected))
            )
        )
        with status, ThreadPoolExecutor(max_workers=max(1, len(selected))) as pool:
            results = list(pool.map(_probe, selected))

        for key, result in zip(selected, results, strict=True):
            ok = bool(result.get("ok"))
            try:
                elapsed = int(result.get("elapsed_ms") or 0)
            except (TypeError, ValueError):
                elapsed = 0
            error_text = str(result.get("error") or "")
            if not ok:
                failed += 1
            payload.append(
                {
                    "platform": key,
                    "ok": ok,
                    "elapsed_ms": elapsed,
                    "error": error_text,
                }
            )
            rows.append(
                [
                    key,
                    t("ok") if ok else t("FAIL"),
                    f"{elapsed} ms",
                    error_text or "-",
                ]
            )

        if as_json:
            # Field names match ``GET /api/v1/music/health`` (``platform``,
            # ``ok``, ``elapsed_ms``, ``error``), so scripts can reuse them.
            ui.emit(
                json.dumps(
                    {
                        "keyword": keyword,
                        "platforms": payload,
                        "failed": failed,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        else:
            ui.render_table(
                t("Music platform health"),
                [t("Platform"), t("Status"), t("Time (ms)"), t("Error")],
                rows,
            )

        if failed:
            raise SystemExit(EXIT_FAILURE)
