#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.export
-----------------------------------------

"""

import contextlib
import json
import os
import sys
import tempfile
from argparse import ArgumentParser, Namespace
from pathlib import Path

from novel_downloader.apps.cli import prompts, ui
from novel_downloader.apps.constants import DOWNLOAD_SUPPORT_SITES
from novel_downloader.apps.utils import load_or_init_config
from novel_downloader.infra.config import ConfigAdapter
from novel_downloader.infra.i18n import t
from novel_downloader.plugins import registrar

from ..ui_adapters import CLIExportUI
from .base import (
    EXIT_FAILURE,
    EXIT_USAGE,
    Command,
    expand_config_paths,
    expand_path,
    fail,
    parse_book_args,
    require_safe_book_id,
    require_safe_site,
)

#: Format used when neither ``--format`` nor the configuration names one.  This
#: mirrors ``CommonClient.export_book``'s own fallback so the CLI knows which
#: formats to hold accountable for producing no file.
_DEFAULT_FORMATS: tuple[str, ...] = ("epub",)


def _normalize_formats(formats: list[str] | None) -> list[str]:
    """
    Strip, lower-case and de-duplicate a format list, preserving order.

    ``--format EPUB`` used to reach the client verbatim: the exporter looked the
    method up case-insensitively but reported the path under the original key,
    so ``--json`` keys changed with the spelling.  Unknown names are kept (a
    plugin may register its own exporter); they surface as a per-format failure
    when no file is produced.
    """
    normalized: list[str] = []
    for raw in formats or ():
        fmt = str(raw).strip().lower()
        if fmt and fmt not in normalized:
            normalized.append(fmt)
    return normalized


def _detect_stage(book_dir: Path) -> str:
    """
    Mirror the exporter's stage selection so the preflight check agrees with it.

    ``CommonClient`` picks the last stage in ``pipeline.json`` that has both a
    chapter database and metadata, then falls back to ``raw``.  Checking a
    different stage would either reject a book the exporter can read or accept
    one it cannot.
    """
    pipeline: list[str] = []
    meta_path = book_dir / "pipeline.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            meta = None
        if isinstance(meta, dict):
            recorded = meta.get("pipeline")
            if isinstance(recorded, list):
                pipeline = [item for item in recorded if isinstance(item, str) and item]

    for candidate in reversed(pipeline):
        if (book_dir / f"chapter.{candidate}.sqlite").is_file() and (
            book_dir / f"book_info.{candidate}.json"
        ).is_file():
            return candidate
    return "raw"


def _metadata_has_chapters(data: object) -> bool | None:
    """
    Return whether a parsed ``book_info`` document lists any chapter.

    ``None`` means the document is not the expected shape (a JSON object with
    a list of volumes and chapters).  The exporters index these structures
    directly, so an unexpected shape is a corrupt library rather than an empty
    book.
    """
    if not isinstance(data, dict):
        return None
    volumes = data.get("volumes", [])
    if not isinstance(volumes, list):
        return None
    for volume in volumes:
        if not isinstance(volume, dict):
            return None
        chapters = volume.get("chapters", [])
        if not isinstance(chapters, list):
            return None
        for chapter in chapters:
            if not isinstance(chapter, dict):
                return None
            if chapter.get("chapterId"):
                return True
    return False


def _book_data_problem(book_dir: Path, stage: str | None) -> tuple[str, Path] | None:
    """
    Return why ``book_dir`` cannot be exported, or ``None`` when it looks usable.

    The exporter treats a missing directory or metadata as "nothing to do" and
    returns an empty list, which the CLI can only report as a generic failure.
    Checking the layout first turns that into a message naming the path and
    distinguishes "never downloaded" from a half-deleted library.  An explicit
    ``--stage`` must have that stage's metadata: the exporter does not fall back
    to another stage when one is requested.

    Metadata that cannot be decoded, is not a JSON object, or has no chapter is
    also rejected here: the exporters return an empty list (or raise an obscure
    ``AttributeError`` that ``export_book`` swallows) for those, hiding the real
    cause behind "Export failed".
    """
    if not book_dir.is_dir():
        return "book data not found", book_dir

    resolved_stage = stage or _detect_stage(book_dir)
    info_path = book_dir / f"book_info.{resolved_stage}.json"
    if not info_path.is_file():
        return "book metadata is missing", info_path

    try:
        data = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        return f"book metadata is corrupt ({err})", info_path

    has_chapters = _metadata_has_chapters(data)
    if has_chapters is None:
        return "book metadata is corrupt (unexpected structure)", info_path
    if not has_chapters:
        return "book has no chapters", info_path
    return None


def _path_key(path_text: str) -> Path:
    """
    Normalize an exported path for duplicate detection.

    ``resolve`` makes relative paths comparable, ``normcase`` folds the
    case-insensitive filesystems (Windows, macOS) where ``A.txt`` and ``a.txt``
    are one file, and a resolution failure keeps the reported spelling instead
    of aborting the whole run.
    """
    with contextlib.suppress(OSError):
        path_text = str(Path(path_text).resolve())
    return Path(os.path.normcase(path_text))


def _check_output_dir(path: Path) -> None:
    """
    Validate (and create) the output directory before any book is exported.

    A path that is an existing file can never receive exports, a missing
    output directory should be created once instead of failing per book, and an
    existing directory that rejects the exporter's temporary file will fail
    every format of every book.  All three are configuration errors worth one
    clear message instead of one generic "Export failed" per book.
    """
    if path.exists() and not path.is_dir():
        fail(
            t("Could not create {path}: {err}").format(
                path=str(path), err="path exists and is not a directory"
            )
        )
    if not path.is_dir():
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            fail(t("Could not create {path}: {err}").format(path=str(path), err=err))
        return

    try:
        fd, probe = tempfile.mkstemp(prefix=".novel-export.", dir=str(path))
    except OSError as err:
        fail(t("Could not create {path}: {err}").format(path=str(path), err=err))
    os.close(fd)
    with contextlib.suppress(OSError):
        os.unlink(probe)


class ExportCmd(Command):
    name = "export"
    help = t("Export previously downloaded novels.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "book_ids",
            nargs="*",
            help=t("Book ID(s) to export (optional; choose interactively if omitted)"),
        )
        parser.add_argument(
            "--format",
            nargs="+",
            help=t("Output format(s): txt, epub, html. Default: from config."),
        )
        parser.add_argument(
            "--site",
            help=t("Source site key (optional; choose interactively if omitted)"),
        )
        parser.add_argument(
            "--config", type=str, help=t("Path to the configuration file")
        )
        parser.add_argument(
            "--start",
            type=str,
            help=t("Start chapter ID (applies only to the first book)"),
        )
        parser.add_argument(
            "--end",
            type=str,
            help=t("End chapter ID (applies only to the first book)"),
        )
        parser.add_argument(
            "--stage",
            type=str,
            help=t("Export stage (e.g. raw, cleaner). Defaults to last stage."),
        )
        parser.add_argument(
            "--out",
            metavar="DIR",
            help=t("Output directory"),
        )
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help=t("Print machine-readable JSON on stdout and exit."),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        as_json = bool(getattr(args, "as_json", False))
        site: str | None = args.site
        stage: str | None = args.stage
        book_ids: list[str] = list(args.book_ids or [])
        config_path: Path | None = expand_path(args.config) if args.config else None
        formats: list[str] | None = args.format

        # Reject an escaping --site before it is joined onto the raw-data root
        # (the same rule ``clean`` applies to its destructive paths).
        if site:
            require_safe_site(site)

        config_data = load_or_init_config(config_path)
        if config_data is None:
            # ``load_or_init_config`` already reported the reason on stderr.
            raise SystemExit(EXIT_FAILURE)

        # ``raw_data_dir`` is read directly below (site/book selection) and via
        # the client config later; expand it once so both agree with the
        # ``~``/env handling of every CLI path option.
        config_data = expand_config_paths(config_data)

        raw_cfg = config_data.get("general") or {}
        raw_dir = Path(raw_cfg.get("raw_data_dir", "./raw_data"))

        # site selection
        if not site:
            book_ids = []  # ignore passed-in ids when site is not specified
            if as_json or not sys.stdin.isatty():
                # --json must never enter interactive selection (documented
                # contract); non-TTY stdin could not answer it either way.
                fail(
                    t(
                        "Interactive selection requires a terminal; "
                        "pass the required arguments explicitly."
                    ),
                    EXIT_USAGE,
                )
            site = prompts.select_site(raw_dir)
            if site is None:
                # A missing raw-data directory is a failure; an explicit
                # cancel (empty selection) is not.
                if not raw_dir.exists():
                    raise SystemExit(EXIT_FAILURE)
                ui.warn(t("No site selected."))
                return

        ui.info(
            t("Using site: {site}").format(
                site=DOWNLOAD_SUPPORT_SITES.get(site, site),
            )
        )

        # book selection
        if not book_ids:
            if as_json or not sys.stdin.isatty():
                fail(
                    t(
                        "Interactive selection requires a terminal; "
                        "pass the required arguments explicitly."
                    ),
                    EXIT_USAGE,
                )
            selected = prompts.select_books(raw_dir, site)
            if not selected:
                if not (raw_dir / site).exists():
                    raise SystemExit(EXIT_FAILURE)
                ui.warn(t("No books selected."))
                return
            book_ids = selected

        # ``--out`` overrides only where the finished books land; the raw-data
        # cache still comes from the configuration (export reads it, it does
        # not write it).  Expand before validation so ``~``/env work on every
        # platform, matching ``batch --out``.
        out_option = getattr(args, "out", None)
        if out_option:
            out_dir = expand_path(out_option).resolve()
            general = dict(config_data.get("general") or {})
            general["output_dir"] = str(out_dir)
            config_data = {**config_data, "general": general}
            ui.info(t("Output directory: {path}").format(path=str(out_dir)))

        adapter = ConfigAdapter(config=config_data)
        ui.setup_logging(
            log_dir=adapter.get_log_dir(),
            console_level=adapter.get_log_level(),
            console=not as_json,
        )

        plugins_cfg = adapter.get_plugins_config()
        if plugins_cfg.get("enable_local_plugins"):
            registrar.enable_local_plugins(
                plugins_cfg.get("local_plugins_path"),
                override=plugins_cfg.get("override_builtins", False),
            )

        # An explicit ``--format`` that normalizes to nothing is a usage error;
        # an empty configured list keeps the client's own default.
        if formats is not None and not _normalize_formats(formats):
            fail(f"{t('Invalid input.')} (--format)", EXIT_USAGE)
        formats = _normalize_formats(
            formats if formats is not None else adapter.get_export_fmt(site)
        ) or list(_DEFAULT_FORMATS)

        books = parse_book_args(book_ids, args.start, args.end)
        # A book ID becomes a directory name under the raw-data root; refuse
        # values that would step outside it instead of reading elsewhere.
        for book in books:
            require_safe_book_id(book.book_id)

        client_config = adapter.get_client_config(site)
        _check_output_dir(Path(client_config.output_dir))

        # Locate every requested book before any export runs: the exporter
        # returns an empty list for "no data", which cannot be told apart from a
        # rejected write in its result.  A missing directory or metadata is a
        # per-book failure that names the path instead.
        book_problems: dict[str, tuple[str, Path]] = {}
        for book in books:
            problem = _book_data_problem(raw_dir / site / book.book_id, stage)
            if problem is not None:
                book_problems[book.book_id] = problem

        exportable = [book for book in books if book.book_id not in book_problems]
        client = None
        if exportable:
            try:
                client = registrar.get_client(site, client_config)
            except Exception as err:
                fail(
                    t("Site error ({site}): {err}").format(site=site, err=err),
                    EXIT_FAILURE,
                )

        export_ui = CLIExportUI()

        exported: dict[str, dict[str, list[str]]] = {}
        failed_books: list[str] = []
        collisions: list[str] = []
        # Normalized output path -> book that wrote it first.  Two same-named
        # books can resolve to one filename (timestamp granularity is a second),
        # and the second atomic replace would silently discard the first.
        first_writer: dict[Path, str] = {}

        for book in books:
            book_id = book.book_id
            exported[book_id] = {}

            problem = book_problems.get(book_id)
            if problem is not None:
                reason, problem_path = problem
                ui.error(
                    t("Could not read {path}: {err}").format(
                        path=str(problem_path), err=reason
                    )
                )
                failed_books.append(book_id)
                continue

            assert client is not None  # every problem-free book is in ``exportable``
            try:
                results = client.export_book(
                    book,
                    cfg=adapter.get_exporter_config(site),
                    formats=formats,
                    stage=stage,
                    ui=export_ui,
                )
                if not isinstance(results, dict):
                    # Older/third-party clients may return ``None``; without
                    # this the ``results.items()`` below raises a traceback
                    # that skips the JSON report and the remaining books.
                    raise TypeError(
                        f"unexpected export result: {type(results).__name__}"
                    )
            except Exception as err:
                # A plugin client is free to raise instead of returning an
                # empty list; isolate it like ``download`` does so the remaining
                # books (and the JSON report) still complete.
                ui.error(
                    t("Process/export failed for book {book_id}: {err}").format(
                        book_id=book_id, err=err
                    )
                )
                failed_books.append(book_id)
                continue

            paths_by_fmt: dict[str, list[str]] = {}
            for fmt, paths in results.items():
                key = str(fmt).strip().lower()
                paths_by_fmt.setdefault(key, []).extend(
                    str(path) for path in (paths or ())
                )
            exported[book_id] = paths_by_fmt

            # Every requested format is accountable: one format succeeding must
            # not hide another that produced nothing (unsupported, missing
            # dependency, rejected write, ...).
            missing = [fmt for fmt in formats if not paths_by_fmt.get(fmt)]
            if missing:
                failed_books.append(f"{book_id} ({', '.join(missing)})")

            for path_text in dict.fromkeys(
                path for paths in paths_by_fmt.values() for path in paths
            ):
                path = _path_key(path_text)
                first = first_writer.setdefault(path, book_id)
                if first != book_id:
                    collisions.append(
                        t(
                            "Books {first} and {second} exported to the same "
                            "file: {path}"
                        ).format(first=first, second=book_id, path=path_text)
                    )

        if as_json:
            ui.emit(
                json.dumps(
                    {
                        "site": site,
                        "books": [
                            {
                                "book_id": book.book_id,
                                "exported": exported.get(book.book_id, {}),
                            }
                            for book in books
                        ],
                    },
                    ensure_ascii=False,
                    # One line, like the other machine-readable reports: a
                    # line-oriented consumer must not have to reassemble JSON.
                    separators=(",", ":"),
                )
            )

        for message in collisions:
            ui.error(message)
        if failed_books:
            # Machine consumers already got the JSON above; tell the human (and
            # the logs) which book(s)/format(s) produced no file before failing.
            ui.error(t("Export failed: {err}").format(err=", ".join(failed_books)))
        if failed_books or collisions:
            raise SystemExit(EXIT_FAILURE)
