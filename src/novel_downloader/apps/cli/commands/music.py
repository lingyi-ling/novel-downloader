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
import json
import os
import re
import sys
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
        raise SystemExit(1) from exc


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
        raise SystemExit(2) from exc
    except Exception as exc:
        ui.error(t("Could not start this platform: {error}").format(error=exc))
        raise SystemExit(1) from exc


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
        raise SystemExit(1) from exc

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


def _track_rows(tracks: list[Any]) -> list[list[str]]:
    """Build the table rows shared by the search and picker views."""
    rows: list[list[str]] = []
    for index, track in enumerate(tracks, 1):
        rows.append(
            [
                str(index),
                str(track.name),
                str(track.artists),
                str(track.album),
                _format_duration(int(track.duration_s or 0)),
                t("VIP") if track.vip else "-",
            ]
        )
    return rows


def _render_tracks(tracks: list[Any]) -> None:
    """Print the search result table."""
    ui.render_table(
        t("Search results"),
        [t("#"), t("Title"), t("Artist"), t("Album"), t("Duration"), t("VIP")],
        _track_rows(tracks),
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
            raise SystemExit(2)

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
            import json

            print(json.dumps(dict(labels), ensure_ascii=False))
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
            raise SystemExit(2)

        platform = str(args.platform)
        limit = max(1, int(args.limit))
        as_json = bool(args.as_json)

        provider = _create_provider(engine, platform)
        _print_platform_note(engine, platform)
        tracks, total = _search(
            engine,
            provider,
            keyword,
            limit,
            show_status=not as_json,
        )

        if as_json:
            payload = {
                "platform": platform,
                "keyword": keyword,
                "total": total,
                "tracks": [
                    _track_payload(index, track)
                    for index, track in enumerate(tracks, 1)
                ],
            }
            ui.emit(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        if not tracks:
            ui.warn(t("No results found for '{keyword}'.").format(keyword=keyword))
            return

        ui.info(
            t("Found {count} result(s) for '{keyword}'.").format(
                count=len(tracks), keyword=keyword
            )
        )
        _render_tracks(tracks)


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
            raise SystemExit(2)

        platform = str(args.platform)
        limit = max(1, int(args.limit))
        settings = engine.settings.load_settings()
        out_dir = str(
            args.out or settings.get("out_dir") or engine.default_download_dir()
        )
        level = str(args.level or settings.get("level") or "exhigh")
        workers = max(1, int(settings.get("workers") or 3))

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
        _render_tracks(tracks)

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
            raise SystemExit(2)

        if not selected:
            ui.warn(t("Cancelled."))
            return

        if platform == "netease" and not _is_logged_in(provider):
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
            raise SystemExit(1) from exc
        finally:
            progress.stop()

        for name, reason in failed_pairs:
            ui.warn(t("Failed: {name} ({reason})").format(name=name, reason=reason))
        ui.success(
            t("Done: {ok} succeeded, {skip} skipped, {fail} failed").format(
                ok=ok, skip=skip, fail=fail
            )
        )


# ---------------------------------------------------------------------------
# export-m3u / health helpers
# ---------------------------------------------------------------------------

#: Characters that cannot appear in a Windows/Unix file name.
_ILLEGAL_FILENAME = re.compile(r'[<>:"/\|?*\x00-\x1f]')


def _playlist_filename(keyword: str) -> str:
    """Build a safe ``<keyword>.m3u8`` file name."""
    stem = _ILLEGAL_FILENAME.sub("_", keyword).strip().rstrip(". ")
    return f"{stem[:120] or 'playlist'}.m3u8"


def _default_playlist_path(keyword: str, out_dir: str) -> str:
    """Return the default playlist path inside the music output directory."""
    return os.path.join(out_dir, _playlist_filename(keyword))


def _split_playable(tracks: list[Any]) -> tuple[list[Any], list[Any]]:
    """
    Split search results into ``(tracks with a local path, skipped tracks)``.

    ``export_m3u`` needs the downloaded file location, so tracks that were
    never downloaded (no ``Track.path``) cannot be written to the playlist.
    """
    ready: list[Any] = []
    skipped: list[Any] = []
    for track in tracks:
        path = getattr(track, "path", "")
        if isinstance(path, str) and path.strip():
            ready.append(track)
        else:
            skipped.append(track)
    return ready, skipped


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

        platform = str(args.platform)
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

        ready, skipped = _split_playable(tracks)
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

        settings = engine.settings.load_settings()
        out_dir = str(settings.get("out_dir") or engine.default_download_dir())
        out_path = str(args.out or _default_playlist_path(keyword, out_dir))

        try:
            playlist = engine.export_m3u(ready, out_path)
        except Exception as exc:
            ui.error(t("Could not write playlist: {error}").format(error=exc))
            raise SystemExit(EXIT_FAILURE) from exc

        ui.success(
            t("Exported {count} track(s) to {path}").format(
                count=len(ready), path=playlist
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

    @classmethod
    def run(cls, args: Namespace) -> None:
        engine = _require_engine()
        keys = engine.platform_keys()
        if not keys:
            ui.warn(t("No music platforms are available."))
            return

        requested = str(args.platform or "").strip()
        if requested:
            if requested not in keys:
                ui.error(
                    t("Unknown music platform: {platform}. Available: {keys}").format(
                        platform=requested,
                        keys=", ".join(keys),
                    )
                )
                raise SystemExit(EXIT_USAGE)
            selected = [requested]
        else:
            selected = keys

        keyword = str(args.keyword or "test").strip() or "test"
        rows: list[list[str]] = []
        failed = 0

        for key in selected:
            with ui.status(t("Checking {platform}...").format(platform=key)):
                result = engine.health_check(key, keyword)

            ok = bool(result.get("ok"))
            elapsed = int(result.get("elapsed_ms") or 0)
            error_text = str(result.get("error") or "")
            if not ok:
                failed += 1
            rows.append(
                [
                    key,
                    t("ok") if ok else t("FAIL"),
                    f"{elapsed} ms",
                    error_text or "-",
                ]
            )

        ui.render_table(
            t("Music platform health"),
            [t("Platform"), t("Status"), t("Time (ms)"), t("Error")],
            rows,
        )

        if failed:
            raise SystemExit(EXIT_FAILURE)
