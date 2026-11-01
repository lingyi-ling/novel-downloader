#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.batch
----------------------------------------

Batch search books from a plain ``title author`` list and, optionally,
download the matches.

The input format matches what the community tools use: one book per line,
``title author`` separated by any run of whitespace (ASCII spaces, tabs and
the full-width U+3000 space included), ``#`` lines are comments and blank
lines are ignored. A line with only one field is treated as a title and
matched by title; the user is warned about it. When the title itself contains
spaces, write a Tab between title and author (``title<TAB>author``): the first
Tab is the explicit separator and spaces before it stay part of the title.
Lines that cannot be parsed are **not** dropped silently: they are collected,
reported, and preserved as comments in the not-found file.

Searches run strictly one after another: a batch file with 100 entries must
not turn into 100 concurrent *book* searches. Within one entry every selected
site is queried concurrently by :mod:`novel_downloader.plugins.search`, and
that search path (raw ``aiohttp`` requests in the searchers) does not apply
``request_interval``, so back-to-back entries reach a site without a pause.

The not-found file is self-contained: it keeps the same ``title author``
format, starts with a ``# site=... searched_at=... matches=...`` header and
ends with a ``#`` marker, so it can be fed straight back into
``batch --from-not-found`` -- with ``--site`` switched, exactly the workflow
this command is meant to support.

``--download`` reuses the plugin client pipeline (``registrar.get_client`` +
``download_book``/``process_book``/``export_book``) that ``download``,
``search`` and the desktop task manager also drive. It is deliberately not
implemented by importing ``DownloadCmd``: that module has no output-directory
override and exits the process on a partial failure, neither of which fits a
batch run (and it is owned by a concurrent change).
"""

from __future__ import annotations

import asyncio
import difflib
import re
import time
import unicodedata
from argparse import ArgumentParser, Namespace
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from novel_downloader.apps.cli import ui
from novel_downloader.apps.cli.ui_adapters import (
    CLIDownloadUI,
    CLIExportUI,
    CLILoginUI,
    CLIProcessUI,
)
from novel_downloader.apps.utils import load_or_init_config
from novel_downloader.infra.config import ConfigAdapter
from novel_downloader.infra.i18n import t
from novel_downloader.libs.filesystem import sanitize_filename
from novel_downloader.plugins import registrar
from novel_downloader.plugins.ranking import normalize_text
from novel_downloader.schemas import BookConfig, SearchResult

from .base import EXIT_FAILURE, EXIT_USAGE, Command, fail

#: Guards against a pathological line being used as a search keyword or
#: written back verbatim into the not-found file.
_MAX_TITLE_LEN = 200
_MAX_AUTHOR_LEN = 100

#: Candidate results requested per site. More than a handful is never needed:
#: matching happens locally on the title/author strings.
_PER_SITE_LIMIT = 10
_SEARCH_TIMEOUT = 5.0

#: Default directory for ``logs/not_found_<site>.txt`` (kept as a module-level
#: path so tests can redirect it without touching the process CWD).
_NOT_FOUND_DIR = Path("logs")

#: ``# site=<key>`` header written by a previous run.
_SITE_HEADER_RE = re.compile(r"^#\s*site=(\S+)")

#: Marker that terminates a not-found file. Any line starting with ``#`` is a
#: comment on read, so this keeps the file parseable as its own input.
_END_MARKER = "#"

#: Unicode categories that make a title unusable as a search keyword.
_CONTROL_CATEGORIES = frozenset({"Cc", "Cs"})


@dataclass(frozen=True)
class BatchEntry:
    """One usable ``title``/``author`` pair from the input file."""

    line_no: int
    title: str
    author: str | None


@dataclass(frozen=True)
class MalformedLine:
    """A non-comment line that could not be turned into a :class:`BatchEntry`."""

    line_no: int
    text: str
    reason: str


@dataclass
class ParsedBatch:
    """Result of parsing one batch file."""

    entries: list[BatchEntry] = field(default_factory=list)
    malformed: list[MalformedLine] = field(default_factory=list)
    #: ``site`` metadata read from the header of a previous not-found file.
    site_hint: str | None = None


@dataclass(frozen=True)
class MatchOutcome:
    """The result chosen for one entry (``None`` when nothing matched)."""

    result: SearchResult | None
    candidates: int
    #: True when the chosen result's author differs from the requested one.
    author_mismatch: bool


@dataclass(frozen=True)
class DownloadReport:
    """Outcome of one ``--download`` pass over the matched books."""

    #: Sites that could not be downloaded from at all (login, session, ...).
    site_failures: int = 0
    #: Books that were downloaded and exported but report missing chapters.
    incomplete_books: int = 0

    @property
    def ok(self) -> bool:
        """True only when every site and every book finished completely."""
        return self.site_failures == 0 and self.incomplete_books == 0


class BatchCmd(Command):
    name = "batch"
    help = t(
        "Batch search books from a 'title author' list and optionally download matches."
    )

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument(
            "--file",
            metavar="PATH",
            help=t(
                "Read 'title author' lines from PATH. A single field is the "
                "title; use a Tab between title and author when the title "
                "contains spaces."
            ),
        )
        source.add_argument(
            "--from-not-found",
            dest="from_not_found",
            metavar="PATH",
            help=t("Continue from a not-found file written by a previous batch run."),
        )
        parser.add_argument(
            "--site",
            metavar="SITE",
            help=t("Search only this site key (default: all sites)."),
        )
        parser.add_argument(
            "--out",
            metavar="DIR",
            help=t(
                "Write exported books to DIR and the raw cache to DIR/raw_data "
                "(default: from config)."
            ),
        )
        parser.add_argument(
            "--strict-match",
            action="store_true",
            help=t(
                "Require both title and author to match exactly "
                "(default: title exact, best author similarity)."
            ),
        )
        parser.add_argument(
            "--not-found",
            metavar="PATH",
            help=t(
                "Write unmatched entries to PATH (default: logs/not_found_<site>.txt)."
            ),
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help=t(
                "Overwrite an existing --not-found file that was not written "
                "by this command."
            ),
        )
        parser.add_argument(
            "--download",
            action="store_true",
            help=t("Download and export matched books (default: report only)."),
        )
        parser.add_argument(
            "--interval",
            type=float,
            default=0.0,
            metavar="SECONDS",
            help=t("Seconds to wait between book searches (default: 0, no delay)."),
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help=t("Show candidate counts and author-match details."),
        )
        parser.add_argument(
            "--config", type=str, help=t("Path to the configuration file")
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        started = time.monotonic()

        file_arg = getattr(args, "file", None)
        from_not_found = getattr(args, "from_not_found", None)
        if not file_arg and not from_not_found:
            fail(t("Provide --file or --from-not-found."), EXIT_USAGE)

        input_path = Path(str(file_arg or from_not_found))
        if not input_path.is_file():
            fail(
                t("Input file not found: {path}").format(path=str(input_path)),
                EXIT_USAGE,
            )

        interval = float(getattr(args, "interval", 0.0) or 0.0)
        if interval < 0:
            fail(
                t("--interval must be zero or positive (got {value}).").format(
                    value=interval
                ),
                EXIT_USAGE,
            )

        try:
            text = input_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as err:
            fail(
                t("Could not read {path}: {err}").format(path=str(input_path), err=err)
            )

        parsed = parse_batch_text(text)
        if not parsed.entries and not parsed.malformed:
            fail(
                t("No book entries found in {path}.").format(path=str(input_path)),
                EXIT_USAGE,
            )

        # The site recorded in a not-found file is a convenience default for
        # the documented "switch site and retry" workflow. "all" means the
        # previous run searched every site, i.e. there is no site to reuse.
        site: str | None = args.site
        if site is None and from_not_found:
            hint = parsed.site_hint
            site = hint if hint and hint != "all" else None
        if site:
            ui.info(t("Using site: {site}").format(site=site))

        _report_input_problems(parsed)

        config_path = Path(args.config) if args.config else None
        config_data = load_or_init_config(config_path)
        if config_data is None:
            # ``load_or_init_config`` already reported the reason on stderr.
            raise SystemExit(EXIT_FAILURE)

        out_dir: Path | None = None
        if args.out:
            out_dir = Path(args.out).expanduser().resolve()
            general = dict(config_data.get("general") or {})
            general["output_dir"] = str(out_dir)
            # ``--out`` is the user saying "put this run's data here", so the
            # raw chapter cache follows the exports instead of staying in the
            # configured ``general.raw_data_dir`` (usually ./raw_data).
            general["raw_data_dir"] = str(out_dir / "raw_data")
            config_data = {**config_data, "general": general}
            if args.download:
                try:
                    out_dir.mkdir(parents=True, exist_ok=True)
                except OSError as err:
                    fail(
                        t("Could not create {path}: {err}").format(
                            path=str(out_dir), err=err
                        )
                    )
            ui.info(t("Output directory: {path}").format(path=str(out_dir)))

        adapter = ConfigAdapter(config=config_data)
        ui.setup_logging(
            log_dir=adapter.get_log_dir(),
            console_level=adapter.get_log_level(),
        )

        plugins_cfg = adapter.get_plugins_config()
        if plugins_cfg.get("enable_local_plugins"):
            registrar.enable_local_plugins(
                plugins_cfg.get("local_plugins_path"),
                override=plugins_cfg.get("override_builtins", False),
            )

        strict = bool(args.strict_match)
        verbose = bool(args.verbose)
        outcomes = asyncio.run(
            _match_entries(
                parsed.entries,
                site=site,
                strict=strict,
                verbose=verbose,
                interval=interval,
            )
        )

        matched = [outcome for outcome in outcomes if outcome.result is not None]
        missed = [
            entry
            for entry, outcome in zip(parsed.entries, outcomes, strict=True)
            if outcome.result is None
        ]

        not_found_path = _not_found_path(args.not_found, site)

        # Write the not-found file *before* downloading. A download can run for
        # hours and may be interrupted (Ctrl+C, crash, network loss); the
        # unmatched entries are the result of the whole search pass and must
        # not be lost because the process died before the file was written.
        if missed or parsed.malformed:
            # ``--file`` input is always read-only: never truncate it, not
            # even with --force.
            if file_arg:
                _refuse_to_overwrite_input(not_found_path, input_path)
            # A different, pre-existing file is only overwritten when it is a
            # not-found file from a previous run (or the user said --force).
            # An in-place ``--from-not-found`` retry is the documented workflow
            # and is exempt, but only when the file really is a not-found
            # artifact (a plain book list must not be truncated either).
            in_place_retry = bool(
                from_not_found
                and _same_path(not_found_path, input_path)
                and _is_not_found_file(not_found_path)
            )
            if not in_place_retry and not args.force:
                _refuse_to_overwrite_foreign_file(not_found_path)
            try:
                _write_not_found(
                    not_found_path,
                    entries=missed,
                    malformed=parsed.malformed,
                    site=site,
                    matched=len(matched),
                    total=len(parsed.entries),
                )
            except OSError as err:
                fail(
                    t("Could not write {path}: {err}").format(
                        path=str(not_found_path), err=err
                    )
                )
            ui.info(
                t("Unmatched entries written to {path}").format(
                    path=str(not_found_path)
                )
            )
        elif verbose:
            ui.info(t("All books matched; no not-found file written."))

        report = DownloadReport()
        if args.download and matched:
            report = _download_matches(adapter, matched)
            if report.incomplete_books:
                # Distinct from a failed download: the book is on disk and was
                # exported, but some chapters are missing. It still makes the
                # batch non-zero because the run is not complete.
                ui.warn(
                    t("Downloaded with incomplete chapters: {count} book(s).").format(
                        count=report.incomplete_books
                    )
                )
        download_failed = not report.ok

        elapsed = time.monotonic() - started
        ui.info(
            t(
                "Total: {total} · matched: {matched} · not found: {not_found} "
                "· malformed: {malformed}"
            ).format(
                total=len(parsed.entries) + len(parsed.malformed),
                matched=len(matched),
                not_found=len(missed),
                malformed=len(parsed.malformed),
            )
        )
        ui.info(t("Elapsed: {seconds}s").format(seconds=f"{elapsed:.1f}"))

        if missed or parsed.malformed or download_failed:
            raise SystemExit(EXIT_FAILURE)


# --------------------------------------------------------------------------- #
# Input parsing
# --------------------------------------------------------------------------- #


def parse_batch_text(text: str) -> ParsedBatch:
    """
    Parse a batch/not-found file.

    A line containing a Tab is split at the **first** Tab: everything before it
    is the title (spaces kept, outer whitespace trimmed) and everything after
    it is the author. That is the explicit form for titles that contain
    spaces, e.g. ``斗破 苍穹<TAB>天蚕土豆``.

    Lines without a Tab keep the historical whitespace heuristic: they are
    split on any Unicode whitespace, the first token is the title and every
    remaining token (joined with a single space) is the author. A title-only
    line is valid and yields ``author=None``.

    :param text: File contents (a leading BOM is tolerated).
    :return: Parsed entries, malformed lines and the optional site hint.
    """
    parsed = ParsedBatch()
    for line_no, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if parsed.site_hint is None:
                match = _SITE_HEADER_RE.match(stripped)
                if match:
                    parsed.site_hint = match.group(1)
            continue

        if "\t" in raw:
            # The Tab is positional, so inspect the raw line: a trailing Tab
            # ("书名\t") means "title only", and spaces left of the first Tab
            # belong to the title.
            title_part, _, author_part = raw.partition("\t")
            title = title_part.strip()
            author = " ".join(author_part.split()) or None
        else:
            tokens = stripped.split()
            title = tokens[0]
            author = " ".join(tokens[1:]) or None
        problem = _entry_problem(title, author)
        if problem is not None:
            parsed.malformed.append(
                MalformedLine(
                    line_no=line_no,
                    text=_clip(stripped),
                    reason=problem,
                )
            )
            continue
        parsed.entries.append(BatchEntry(line_no=line_no, title=title, author=author))
    return parsed


def _entry_problem(title: str, author: str | None) -> str | None:
    """Return a localized reason the line is unusable, or ``None``."""
    if _has_control(title) or (author is not None and _has_control(author)):
        return t("contains control characters")
    if len(title) > _MAX_TITLE_LEN:
        return t("title is too long")
    if author is not None and len(author) > _MAX_AUTHOR_LEN:
        return t("author is too long")
    if not normalize_text(title):
        return t("title has no searchable characters")
    return None


def _has_control(value: str) -> bool:
    """True when ``value`` contains a control/surrogate character."""
    return any(unicodedata.category(char) in _CONTROL_CATEGORIES for char in value)


def _clip(value: str, limit: int = 80) -> str:
    """Collapse whitespace/control characters and truncate for display."""
    cleaned = "".join(
        " " if unicodedata.category(char) in _CONTROL_CATEGORIES else char
        for char in value
    )
    cleaned = " ".join(cleaned.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #


def select_match(
    entry: BatchEntry,
    results: Sequence[SearchResult],
    *,
    strict: bool = False,
) -> MatchOutcome:
    """
    Pick the result that corresponds to ``entry``.

    A candidate must match the title exactly after normalization (case,
    punctuation, spacing and separators are ignored). Without ``strict``, an
    exact author match wins; otherwise the title candidate whose author is
    most similar is chosen. With ``strict`` an author mismatch means no match.

    :param entry: Parsed input line.
    :param results: Ranked search results for ``entry.title``.
    :param strict: Require an exact normalized author match.
    :return: The chosen result (if any), the candidate count and whether the
        chosen author differs from the requested one.
    """
    title_key = normalize_text(entry.title)
    candidates = [
        result for result in results if normalize_text(result.get("title")) == title_key
    ]
    if not candidates:
        return MatchOutcome(result=None, candidates=0, author_mismatch=False)

    # Title-only entries have nothing to compare the author against; the
    # ranking already puts the best-corroborated result first.
    if entry.author is None:
        return MatchOutcome(
            result=candidates[0],
            candidates=len(candidates),
            author_mismatch=False,
        )

    author_key = normalize_text(entry.author)
    exact = [
        result
        for result in candidates
        if normalize_text(result.get("author")) == author_key
    ]
    if exact:
        return MatchOutcome(
            result=exact[0],
            candidates=len(candidates),
            author_mismatch=False,
        )
    if strict:
        return MatchOutcome(
            result=None,
            candidates=len(candidates),
            author_mismatch=True,
        )

    # ``max`` keeps the first of equally similar candidates, i.e. the one the
    # ranker preferred.
    best = max(
        candidates,
        key=lambda result: _author_similarity(
            author_key, normalize_text(result.get("author"))
        ),
    )
    return MatchOutcome(result=best, candidates=len(candidates), author_mismatch=True)


def _author_similarity(expected: str, actual: str) -> float:
    """Similarity of two normalized author strings in the 0..1 range."""
    if not expected or not actual:
        return 0.0
    if expected == actual:
        return 1.0
    if expected in actual or actual in expected:
        return 0.9
    return difflib.SequenceMatcher(None, expected, actual).ratio()


async def _match_entries(
    entries: Sequence[BatchEntry],
    *,
    site: str | None,
    strict: bool,
    verbose: bool,
    interval: float = 0.0,
) -> list[MatchOutcome]:
    """
    Search and match every entry, one search at a time.

    :param interval: Seconds to wait between two entries' searches. ``0``
        (the default) keeps the historical back-to-back behaviour.
    """
    outcomes: list[MatchOutcome] = []
    total = len(entries)
    for index, entry in enumerate(entries, 1):
        if interval > 0 and index > 1:
            await asyncio.sleep(interval)
        try:
            results = await _search_one(entry, site)
        except Exception as err:
            # A site may be down or a rule may be broken; that must not abort
            # the rest of the batch. The entry is reported as not found.
            ui.warn(
                t("Search failed for '{title}': {err}").format(
                    title=entry.title, err=err
                )
            )
            results = []

        outcome = select_match(entry, results, strict=strict)
        outcomes.append(outcome)
        _report_outcome(index, total, entry, outcome)
        if verbose:
            _report_detail(entry, outcome)
    return outcomes


async def _search_one(entry: BatchEntry, site: str | None) -> list[SearchResult]:
    """
    Search one title.

    Only the title is sent as the keyword: sites index that field, while the
    author is matched locally (and may be spelled differently per site).
    """
    from novel_downloader.plugins.search import search

    return await search(
        entry.title,
        sites=[site] if site else None,
        per_site_limit=_PER_SITE_LIMIT,
        timeout=_SEARCH_TIMEOUT,
    )


def _author_label(entry: BatchEntry) -> str:
    return entry.author or t("unknown")


def _report_outcome(
    index: int,
    total: int,
    entry: BatchEntry,
    outcome: MatchOutcome,
) -> None:
    """Print the ``[i/N] title (author) -> ...`` progress line."""
    context = {
        "index": index,
        "total": total,
        "title": entry.title,
        "author": _author_label(entry),
    }
    result = outcome.result
    if result is None:
        ui.warn(t("[{index}/{total}] {title} ({author}) → not found").format(**context))
        return
    site = str(result.get("site") or "")
    ui.success(
        t("[{index}/{total}] {title} ({author}) → matched {site}").format(
            site=site, **context
        )
    )


def _report_detail(entry: BatchEntry, outcome: MatchOutcome) -> None:
    """Print verbose matching details for one entry."""
    context = {
        "title": entry.title,
        "author": _author_label(entry),
        "count": outcome.candidates,
    }
    if outcome.candidates > 1:
        ui.info(t("{title} ({author}): {count} candidate(s)").format(**context))
    result = outcome.result
    if result is not None and outcome.author_mismatch:
        ui.warn(
            t(
                "Author mismatch for '{title}': expected '{expected}', got '{actual}'."
            ).format(
                title=entry.title,
                expected=entry.author or "",
                actual=str(result.get("author") or ""),
            )
        )


def _report_input_problems(parsed: ParsedBatch) -> None:
    """Warn about malformed lines and title-only entries before searching."""
    for item in parsed.malformed:
        ui.warn(
            t("Skipped malformed line {line}: {text} ({reason})").format(
                line=item.line_no,
                text=item.text,
                reason=item.reason,
            )
        )
    for entry in parsed.entries:
        if entry.author is None:
            ui.warn(
                t("No author for '{title}'; matching by title only.").format(
                    title=entry.title
                )
            )


# --------------------------------------------------------------------------- #
# Not-found file
# --------------------------------------------------------------------------- #


def _not_found_path(override: str | None, site: str | None) -> Path:
    """Resolve ``--not-found`` or the default ``logs/not_found_<site>.txt``."""
    if override:
        return Path(override).expanduser()
    # Both separators are stripped on every platform, not just the one the
    # running OS treats as a separator: a site key is untrusted input and a
    # name that is safe on POSIX today must not escape when the log directory
    # is later copied to Windows (or read by a Windows tool).
    safe_site = (site or "all").replace("/", "_").replace("\\", "_")
    stem = sanitize_filename(
        f"not_found_{safe_site}",
        max_length=120,
        collapse_whitespace=True,
        fallback="not_found",
    )
    return _NOT_FOUND_DIR / f"{stem}.txt"


def _same_path(left: Path, right: Path) -> bool:
    """True when two paths resolve to the same file."""
    try:
        return left.expanduser().resolve() == right.expanduser().resolve()
    except OSError:  # pragma: no cover - resolve() rarely fails on bad paths
        return False


def _refuse_to_overwrite_input(target: Path, source: Path) -> None:
    """
    Abort when the not-found target is the very file being read.

    ``--file books.txt --not-found books.txt`` (a plausible copy/paste slip)
    would otherwise truncate the user's book list down to the unmatched
    entries, silently destroying the input. Rewriting a *previous* not-found
    file in place stays allowed (through ``--from-not-found``): that is the
    normal "retry" workflow.

    :param target: Path the not-found file would be written to.
    :param source: Path the batch input was read from.
    """
    if _same_path(target, source):
        fail(
            t(
                "Refusing to overwrite the input file {path}; "
                "choose a different --not-found path."
            ).format(path=str(source)),
            EXIT_USAGE,
        )


def _is_not_found_file(path: Path) -> bool:
    """True when ``path`` exists and starts with this command's header."""
    if not path.is_file():
        return False
    try:
        with path.open("rb") as handle:
            first_line = handle.readline(200)
    except OSError:
        # Unreadable target: let the write path report the actual error.
        return True
    return first_line.decode("utf-8-sig", errors="replace").startswith("# site=")


def _refuse_to_overwrite_foreign_file(target: Path) -> None:
    """
    Abort before clobbering an existing file this command did not write.

    A not-found file always starts with a ``# site=`` header. Any other
    existing file at the target path is somebody's data (a book list, notes),
    so overwriting it needs an explicit ``--force``.

    :param target: Path the not-found file would be written to.
    """
    if not target.is_file():
        return
    if _is_not_found_file(target):
        return
    fail(
        t(
            "Refusing to overwrite {path}: it is not a not-found file written "
            "by this command (use --force to overwrite it)."
        ).format(path=str(target)),
        EXIT_USAGE,
    )


def _write_not_found(
    path: Path,
    *,
    entries: Sequence[BatchEntry],
    malformed: Sequence[MalformedLine],
    site: str | None,
    matched: int,
    total: int,
) -> None:
    """
    Write the not-found file.

    The body keeps the exact input format so the file is valid input for a
    later ``batch --from-not-found`` run. Malformed lines are preserved as
    comments: they cannot be searched, but silently losing what the user typed
    is worse than a comment they can fix.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    searched_at = datetime.now(UTC).isoformat(timespec="seconds")
    site_key = sanitize_filename(
        site or "all",
        max_length=64,
        collapse_whitespace=True,
        fallback="all",
    )

    lines = [f"# site={site_key} searched_at={searched_at} matches={matched}/{total}"]
    lines.extend(_format_entry(entry) for entry in entries)
    for item in malformed:
        lines.append(f"# malformed line {item.line_no}: {item.text}")
    lines.append(_END_MARKER)

    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def _format_entry(entry: BatchEntry) -> str:
    """
    Serialize one entry so :func:`parse_batch_text` reads it back unchanged.

    A title containing whitespace is only representable with the explicit Tab
    form; a title-only entry uses the same form with an empty author (the
    trailing Tab), otherwise the title would be split on its own spaces.
    """
    if any(char.isspace() for char in entry.title):
        if entry.author is None:
            return f"{entry.title}\t"
        return f"{entry.title}\t{entry.author}"
    if entry.author is None:
        return entry.title
    return f"{entry.title} {entry.author}"


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #


def _download_matches(
    adapter: ConfigAdapter,
    outcomes: Sequence[MatchOutcome],
) -> DownloadReport:
    """
    Download every matched book, grouped by site.

    Sites are processed one at a time (and books within a site one at a time),
    so a large batch never opens many sessions at once.

    :return: Aggregate report; ``ok`` is True only when every site and every
        book finished completely.
    """
    grouped: dict[str, list[str]] = {}
    for outcome in outcomes:
        result = outcome.result
        if result is None:
            continue
        site = str(result.get("site") or "")
        book_id = str(result.get("book_id") or "")
        if not site or not book_id:
            continue
        bucket = grouped.setdefault(site, [])
        if book_id not in bucket:
            bucket.append(book_id)

    if not grouped:
        ui.warn(t("No downloadable matches."))
        return DownloadReport(site_failures=1)

    site_failures = 0
    incomplete_books = 0
    total = len(grouped)
    for index, (site, book_ids) in enumerate(grouped.items(), 1):
        ui.info(
            t("Downloading from {site} ({index}/{total})").format(
                site=site, index=index, total=total
            )
        )
        report = _download_site(adapter, site, book_ids)
        site_failures += report.site_failures
        incomplete_books += report.incomplete_books
    return DownloadReport(
        site_failures=site_failures,
        incomplete_books=incomplete_books,
    )


def _download_site(
    adapter: ConfigAdapter,
    site: str,
    book_ids: Sequence[str],
) -> DownloadReport:
    """
    Download, process and export ``book_ids`` from one site.

    A book that fails must not abort the rest of the batch: each download,
    process and export step is attempted per book, the failure is reported for
    that book only, and the remaining books keep going. A book that downloads
    but reports missing chapters is counted separately from a hard failure.

    :param adapter: Resolved configuration adapter.
    :param site: Site key all ``book_ids`` belong to.
    :param book_ids: Book identifiers to download, in input order.
    :return: Per-site report (hard failure count and incomplete book count).
    """
    client = registrar.get_client(site, adapter.get_client_config(site))
    books = [BookConfig(book_id=book_id) for book_id in book_ids]
    login_ui = CLILoginUI()
    download_ui = CLIDownloadUI()
    login_failed = False
    failed_books: set[BookConfig] = set()
    incomplete_books = 0

    async def _run() -> None:
        nonlocal login_failed, incomplete_books
        async with client:
            if adapter.get_login_required(site):
                success = await client.login(
                    ui=login_ui,
                    login_cfg=adapter.get_login_config(site),
                )
                if not success:
                    login_failed = True
                    return
            for book in books:
                try:
                    summary = await client.download_book(book, ui=download_ui)
                except Exception as err:
                    # One broken book (bad ID, deleted page, parse error) must
                    # not cost the user the other books in the batch.
                    failed_books.add(book)
                    ui.error(
                        t("Download failed for book {book_id}: {err}").format(
                            book_id=book.book_id, err=err
                        )
                    )
                    continue
                failed_chapters = _failed_chapters(summary)
                if failed_chapters:
                    # The book exists and will be exported; only some
                    # chapters are missing. Keep it separate from a hard
                    # failure so the report can tell the two apart.
                    incomplete_books += 1
                    ui.warn(
                        t(
                            "Book {book_id} downloaded with {failed} "
                            "incomplete chapter(s)."
                        ).format(
                            book_id=book.book_id,
                            failed=failed_chapters,
                        )
                    )

    try:
        asyncio.run(_run())
    except ValueError as err:
        ui.warn(
            t("'{site}' is currently not supported: {err}").format(site=site, err=err)
        )
        return DownloadReport(site_failures=1)
    except Exception as err:
        ui.error(t("Site error ({site}): {err}").format(site=site, err=err))
        return DownloadReport(site_failures=1)

    if login_failed:
        ui.error(t("Login failed."))
        return DownloadReport(site_failures=1)
    if not download_ui.completed_books:
        ui.error(t("No books were downloaded from {site}.").format(site=site))
        return DownloadReport(site_failures=1)

    process_ui = CLIProcessUI()
    export_ui = CLIExportUI()
    all_ok = not failed_books
    for book in books:
        if book not in download_ui.completed_books:
            # Either ``download_book`` raised for this book (already counted in
            # ``failed_books``) or the client never reported it as complete.
            continue
        try:
            client.process_book(
                book,
                processors=adapter.get_processor_configs(site),
                ui=process_ui,
            )
            client.export_book(
                book,
                cfg=adapter.get_exporter_config(site),
                formats=adapter.get_export_fmt(site),
                ui=export_ui,
            )
        except Exception as err:
            # Same rule as the download loop: continue with the next book.
            ui.error(
                t("Process/export failed for book {book_id}: {err}").format(
                    book_id=book.book_id, err=err
                )
            )
            all_ok = False
    return DownloadReport(
        site_failures=0 if all_ok else 1,
        incomplete_books=incomplete_books,
    )


def _failed_chapters(summary: object) -> int:
    """
    Return the failed-chapter count from a ``DownloadSummary``-like value.

    Third-party clients may return ``None`` (or a non-dict), so the check is
    structural; the same approach the ``download`` command uses.
    """
    if not isinstance(summary, dict):
        return 0
    failed = summary.get("failed")
    return failed if isinstance(failed, int) and failed > 0 else 0


__all__ = [
    "BatchCmd",
    "BatchEntry",
    "DownloadReport",
    "MalformedLine",
    "MatchOutcome",
    "ParsedBatch",
    "parse_batch_text",
    "select_match",
]
