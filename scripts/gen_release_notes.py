#!/usr/bin/env python3
"""Generate release notes from Conventional Commits.

The commit range is read with ``git log <from>..<to>``.  When ``--from`` is
omitted, the range starts at the most recent tag reachable from ``--to`` (the
usual release workflow); repositories without tags fall back to the first
commit, so nothing is silently skipped.  Messages are grouped by Conventional
Commits type, and both ``!`` markers and ``BREAKING CHANGE:`` footers are
reported in a dedicated section.

Usage:
    python scripts/gen_release_notes.py --version v1.1.0
    python scripts/gen_release_notes.py --from v1.0.0 --to HEAD
    python scripts/gen_release_notes.py --from v1.0.0 --output RELEASE.md
    python scripts/gen_release_notes.py --json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Order in which groups appear in the rendered notes.
TYPE_TITLES: dict[str, str] = {
    "feat": "Features",
    "fix": "Bug Fixes",
    "perf": "Performance",
    "docs": "Documentation",
    "test": "Tests",
    "build": "Build System",
    "refactor": "Refactoring",
    "chore": "Chores",
}
OTHER_TYPE = "other"
OTHER_TITLE = "Other Changes"
GROUP_ORDER = (*TYPE_TITLES, OTHER_TYPE)

HEADER_RE = re.compile(
    r"^(?P<type>[A-Za-z][\w+/-]*)(?:\((?P<scope>[^)]*)\))?"
    r"(?P<bang>!)?:\s*(?P<description>.+)$"
)
BREAKING_RE = re.compile(r"^BREAKING[ -]CHANGE:\s*(?P<text>.*)$", re.IGNORECASE)
FOOTER_RE = re.compile(r"^[A-Z][A-Z0-9 _-]+:\s")

RECORD_SEP = "\x1e"
FIELD_SEP = "\x1f"


class ReleaseNotesError(RuntimeError):
    """A git command failed or the repository cannot be read."""


@dataclass(slots=True)
class Commit:
    """One parsed commit message."""

    hash: str
    short_hash: str
    author: str
    email: str
    date: str
    subject: str
    type: str
    description: str
    scope: str | None = None
    breaking: bool = False
    breaking_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "hash": self.hash,
            "short_hash": self.short_hash,
            "author": self.author,
            "email": self.email,
            "date": self.date,
            "subject": self.subject,
            "type": self.type,
            "scope": self.scope,
            "description": self.description,
            "breaking": self.breaking,
            "breaking_notes": self.breaking_notes,
        }


def run_git(
    args: list[str], repo: Path, *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a git command inside *repo*."""

    command = ["git", "-c", "i18n.logOutputEncoding=UTF-8", *args]
    proc = subprocess.run(
        command,
        cwd=repo,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown git error"
        raise ReleaseNotesError(f"git {' '.join(args)} failed: {detail}")
    return proc


def extract_breaking_notes(body: str) -> list[str]:
    """Return the values of all ``BREAKING CHANGE`` footers in *body*."""

    lines = body.splitlines()
    notes: list[str] = []
    index = 0
    while index < len(lines):
        match = BREAKING_RE.match(lines[index].strip())
        if match is None:
            index += 1
            continue

        parts = [match.group("text").strip()]
        index += 1
        while index < len(lines):
            continuation = lines[index]
            if not continuation.strip() or FOOTER_RE.match(continuation.strip()):
                break
            if not continuation.startswith((" ", "\t")):
                break
            parts.append(continuation.strip())
            index += 1

        note = " ".join(part for part in parts if part)
        if note:
            notes.append(note)
    return notes


def parse_commit(
    *,
    commit_hash: str,
    short_hash: str,
    author: str,
    email: str,
    date_: str,
    message: str,
) -> Commit:
    """Parse a raw commit message into a :class:`Commit`."""

    lines = message.splitlines()
    header = lines[0].strip() if lines else ""
    body = "\n".join(lines[1:])
    notes = extract_breaking_notes(body)

    scope: str | None = None
    breaking = bool(notes)
    match = HEADER_RE.match(header)
    if match is None:
        commit_type = OTHER_TYPE
        description = header or "(no commit message)"
    else:
        # Earlier in this repository some commits combine types (``fix+perf:``).
        # Group them under the first recognised type instead of dropping them.
        raw_tokens = [
            token for token in re.split(r"[+/]", match["type"].lower()) if token
        ]
        commit_type = next(
            (token for token in raw_tokens if token in TYPE_TITLES), OTHER_TYPE
        )
        scope = (match["scope"] or "").strip() or None
        description = match["description"].strip()
        breaking = breaking or bool(match["bang"])

    return Commit(
        hash=commit_hash,
        short_hash=short_hash,
        author=author,
        email=email,
        date=date_,
        subject=header,
        type=commit_type,
        description=description,
        scope=scope,
        breaking=breaking,
        breaking_notes=notes,
    )


def latest_tag(repo: Path, to_ref: str) -> str | None:
    """Return the most recent tag reachable from *to_ref*, if any."""

    proc = run_git(["describe", "--tags", "--abbrev=0", to_ref], repo, check=False)
    tag = proc.stdout.strip()
    return tag or None


def detect_version(repo: Path, to_ref: str) -> str:
    """Return the latest tag reachable from *to_ref*, or ``Unreleased``."""

    return latest_tag(repo, to_ref) or "Unreleased"


def resolve_range(
    repo: Path, from_ref: str | None, to_ref: str
) -> tuple[str, str, str | None]:
    """Return ``(git_range, human_label, effective_from)``.

    Without an explicit start revision the latest reachable tag is used.  When
    the repository has no tags, every commit up to *to_ref* is included (the
    root commit is deliberately kept).
    """

    effective_from = from_ref or latest_tag(repo, to_ref)
    if effective_from:
        label = f"{effective_from}..{to_ref}"
        return label, label, effective_from

    return to_ref, f"first commit..{to_ref}", None


def collect_commits(repo: Path, from_ref: str | None, to_ref: str) -> list[Commit]:
    """Collect non-merge commits in the requested range."""

    log_range, _, _ = resolve_range(repo, from_ref, to_ref)
    fmt = (
        f"--format=%H{FIELD_SEP}%h{FIELD_SEP}%ad{FIELD_SEP}%an{FIELD_SEP}%ae"
        f"{FIELD_SEP}%B{RECORD_SEP}"
    )
    output = run_git(
        ["log", "--no-merges", "--date=short", fmt, log_range, "--"], repo
    ).stdout

    commits: list[Commit] = []
    for record in output.split(RECORD_SEP):
        record = record.strip("\r\n")
        if not record:
            continue
        fields = record.split(FIELD_SEP, 5)
        if len(fields) != 6:
            continue
        commit_hash, short_hash, date_, author, email, message = fields
        commits.append(
            parse_commit(
                commit_hash=commit_hash,
                short_hash=short_hash,
                author=author,
                email=email,
                date_=date_,
                message=message,
            )
        )
    return commits


def collect_contributors(
    repo: Path, from_ref: str | None, to_ref: str
) -> list[dict[str, object]]:
    """Collect authors with their commit counts using ``git shortlog``."""

    log_range, _, _ = resolve_range(repo, from_ref, to_ref)
    output = run_git(
        ["shortlog", "-s", "-n", "-e", "--no-merges", log_range], repo
    ).stdout

    contributors: list[dict[str, object]] = []
    for line in output.splitlines():
        match = re.match(r"^\s*(\d+)\s+(.+?)\s*$", line)
        if match is None:
            continue
        count = int(match.group(1))
        identity = match.group(2)
        identity_match = re.match(r"^(?P<name>.*?)\s*<(?P<email>[^>]+)>$", identity)
        if identity_match is None:
            name, email = identity, ""
        else:
            name = identity_match.group("name") or identity_match.group("email")
            email = identity_match.group("email")
        contributors.append({"name": name, "email": email, "commits": count})
    return contributors


def build_release_notes(
    repo: Path,
    *,
    from_ref: str | None = None,
    to_ref: str = "HEAD",
    version: str = "Unreleased",
    release_date: str | None = None,
) -> dict[str, object]:
    """Build the release-note data structure (also used for ``--json``)."""

    _, label, effective_from = resolve_range(repo, from_ref, to_ref)
    commits = collect_commits(repo, from_ref, to_ref)
    contributors = collect_contributors(repo, from_ref, to_ref)
    grouped: list[dict[str, object]] = []
    for commit_type in GROUP_ORDER:
        members = [commit for commit in commits if commit.type == commit_type]
        if not members:
            continue
        grouped.append(
            {
                "type": commit_type,
                "title": TYPE_TITLES.get(commit_type, OTHER_TITLE),
                "commits": [commit.to_dict() for commit in members],
            }
        )

    breaking = [commit.to_dict() for commit in commits if commit.breaking]
    empty = not commits
    return {
        "version": version,
        "date": release_date or date.today().isoformat(),
        "range": {"from": effective_from, "to": to_ref, "label": label},
        "empty": empty,
        "message": f"No commits found in range {label}." if empty else None,
        "stats": {
            "commits": len(commits),
            "groups": len(grouped),
            "breaking_changes": len(breaking),
            "contributors": len(contributors),
        },
        "groups": grouped,
        "breaking_changes": breaking,
        "contributors": contributors,
    }


def _commit_line(commit: dict[str, object]) -> str:
    scope = commit.get("scope")
    prefix = f"**{scope}:** " if scope else ""
    return f"- {prefix}{commit['description']} (`{commit['short_hash']}`)"


def render_markdown(data: dict[str, object]) -> str:
    """Render the release-note data as Markdown."""

    range_info = data["range"]
    assert isinstance(range_info, dict)
    label = range_info["label"]
    lines = [f"# Release {data['version']}", "", f"**Date:** {data['date']}", ""]

    if data["empty"]:
        lines.extend([f"_No commits found in range `{label}`._", ""])
        return "\n".join(lines)

    stats = data["stats"]
    assert isinstance(stats, dict)
    lines.extend([f"**Range:** `{label}` · {stats['commits']} commit(s)", ""])

    breaking = data["breaking_changes"]
    assert isinstance(breaking, list)
    if breaking:
        lines.extend(["## Breaking Changes", ""])
        for commit in breaking:
            lines.append(_commit_line(commit))
            for note in commit["breaking_notes"]:
                lines.append(f"  - {note}")
        lines.append("")

    groups = data["groups"]
    assert isinstance(groups, list)
    for group in groups:
        lines.extend([f"## {group['title']}", ""])
        for commit in group["commits"]:
            lines.append(_commit_line(commit))
        lines.append("")

    contributors = data["contributors"]
    assert isinstance(contributors, list)
    if contributors:
        lines.extend(["## Contributors", ""])
        for contributor in contributors:
            lines.append(f"- {contributor['name']} ({contributor['commits']})")
        lines.append("")

    return "\n".join(lines)


def render_json(data: dict[str, object]) -> str:
    """Render the release-note data as JSON."""

    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate release notes from Conventional Commits."
    )
    parser.add_argument(
        "--from",
        dest="from_ref",
        metavar="REV",
        help="start revision (exclusive); defaults to the latest tag, or the "
        "first commit when no tags exist",
    )
    parser.add_argument(
        "--to", dest="to_ref", default="HEAD", metavar="REV", help="end revision"
    )
    parser.add_argument(
        "--version", help="release title/version (defaults to the latest tag)"
    )
    parser.add_argument(
        "--output",
        type=Path,
        metavar="PATH",
        help="write notes to PATH instead of stdout",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit JSON instead of Markdown"
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=PROJECT_ROOT,
        metavar="PATH",
        help="repository root (defaults to the project root)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = args.repo.resolve()
    try:
        version = args.version or detect_version(repo, args.to_ref)
        data = build_release_notes(
            repo,
            from_ref=args.from_ref,
            to_ref=args.to_ref,
            version=version,
        )
    except ReleaseNotesError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    rendered = render_json(data) if args.json else render_markdown(data)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
