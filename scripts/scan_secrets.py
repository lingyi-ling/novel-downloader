#!/usr/bin/env python3
"""Offline secret scan for files that Git tracks or has staged.

The scan is deliberately narrow and fully offline: it looks for the credential
shapes that are cheap to recognise with high confidence, and it never prints a
matched value.  A finding is reported as ``path:line: rule`` only, so a CI log
cannot become the leak the check is meant to prevent.

Rules
-----
``private-key``
    PEM private key headers (RSA/EC/OPENSSH/DSA/PGP), including the block
    marker only -- the key material itself is never read out.

``aws-access-key``
    ``AKIA``/``ASIA``-prefixed AWS access key ids.

``github-token``
    Classic (``ghp_``, ``gho_``, ``ghu_``, ``ghs_``, ``ghr_``) and
    fine-grained (``github_pat_``) personal access tokens.

``slack-token``
    ``xoxa-``/``xoxb-``/``xoxp-``/``xoxr-``/``xoxs-`` bot, user and app
    tokens.

``bearer-token``
    A literal ``Bearer`` credential longer than a header placeholder.

``generic-secret``
    A quoted or literal value assigned to a credential-shaped key
    (``password``, ``secret``, ``token``, ``api_key``, ...).  Values that
    name an environment variable, a config attribute or a documented
    placeholder are ignored.

``sensitive-file``
    A tracked keystore or SSH private key file name (``*.jks``, ``*.keystore``,
    ``*.p12``, ``*.pfx``, ``*.bks``, ``id_rsa``, ...).

Scope
-----
Only files known to Git are read (``git ls-files``), so ignored build output
and local credential files are out of scope.  Binary files, files without a
known text extension and files above :data:`MAX_FILE_BYTES` are skipped;
keystore file names are still reported because the name alone is the finding.

History
-------
``--history`` walks ``git log -p`` and scans only the lines each commit added,
so a credential that has since been deleted from the working tree is still
reported.  The same rules and path allowlist apply.  The scan is read-only: it
never checks anything out and never rewrites history.  A commit whose patch is
larger than :data:`MAX_COMMIT_PATCH_BYTES` is skipped as a whole, the run stops
after :data:`MAX_TOTAL_PATCH_BYTES`, and ``--max-commits`` bounds the walk on a
large repository.  Merges contribute no patch by default and are therefore
harmless to include.

Usage::

    python scripts/scan_secrets.py
    python scripts/scan_secrets.py --json
    python scripts/scan_secrets.py --staged
    python scripts/scan_secrets.py --history
    python scripts/scan_secrets.py --history --since v1.20.0 --max-commits 200

Exit codes
----------
``0``
    No findings.
``1``
    At least one finding, or the scan could not run (the error goes to
    stderr and never includes file content).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

EXIT_OK = 0
EXIT_FINDINGS = 1

DEFAULT_ROOT = Path(__file__).resolve().parents[1]

#: Files larger than this are skipped.  Credentials live in small source and
#: configuration files; a large generated blob is not worth the scan time.
MAX_FILE_BYTES = 2 * 1024 * 1024

#: Bytes inspected for a NUL byte before a file is treated as binary.
BINARY_PROBE_BYTES = 8192

#: Patch size above which a single historical commit is skipped as a whole.
#: The scan must stay cheap on repositories that once checked in a large blob.
MAX_COMMIT_PATCH_BYTES = 8 * 1024 * 1024

#: Total patch bytes read before a history scan stops early.
MAX_TOTAL_PATCH_BYTES = 256 * 1024 * 1024

#: Extensions that are expected to hold text.  The list is explicit rather
#: than extension-less so a stray ``.apk`` inside the tree is never decoded.
TEXT_SUFFIXES = frozenset(
    {
        ".bat",
        ".cfg",
        ".cmake",
        ".cmd",
        ".conf",
        ".css",
        ".dockerfile",
        ".editorconfig",
        ".env",
        ".gitattributes",
        ".gitignore",
        ".gradle",
        ".htaccess",
        ".html",
        ".htm",
        ".ini",
        ".ipynb",
        ".iss",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".key",
        ".kt",
        ".kts",
        ".md",
        ".mk",
        ".pem",
        ".po",
        ".pot",
        ".properties",
        ".pro",
        ".ps1",
        ".py",
        ".pyi",
        ".rst",
        ".sh",
        ".spec",
        ".sql",
        ".tf",
        ".tfvars",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".typed",
        ".xml",
        ".yaml",
        ".yml",
    }
)

#: Extension-less files that are known text and often hold configuration.
TEXT_NAMES = frozenset(
    {
        ".dockerignore",
        ".editorconfig",
        ".env",
        ".gitattributes",
        ".gitconfig",
        ".gitignore",
        ".npmrc",
        ".pypirc",
        "Dockerfile",
        "Makefile",
        "gradlew",
    }
)

#: Matching keystore/key file names are findings on their own: these formats
#: are effectively always private material (a debug keystore is public, but it
#: still must not be committed by policy).
SENSITIVE_FILENAME_RE = re.compile(
    r"(?i)(?:^|/)"
    r"(?:"
    r"[^/]+\.(?:jks|keystore|p12|pfx|bks|pkcs12)"
    r"|keystore\.properties"
    r"|id_(?:rsa|dsa|ecdsa|ed25519)"
    r")$"
)

#: Documented placeholders and templates.  These appear in README files and
#: test fixtures by design, so they are not findings.
PLACEHOLDER_RE = re.compile(
    r"(?i)(?:"
    r"your|"
    r"change[-_ ]?me|"
    r"change[-_ ]?this|"
    r"replace[-_ ]?me|"
    r"example|"
    r"placeholder|"
    r"dummy|"
    r"fake|"
    r"sample|"
    r"redacted|"
    r"test(?:ing)?[-_]?|"
    r"xxx+|"
    r"not[-_ ]?a[-_ ]?real|"
    r"my[-_ ]?(?:secret|token|key|password)|"
    r"secret[-_ ]?token|"
    r"(?:^|[-_ ])(?:secret|token|key|password)$|"
    r"<[^>]*>|"
    r"\{\{.*?\}\}|"
    r"\$\{.*?\}|"
    r"%\([^)]*\)s|"
    r"\.\.\.|"
    r"^(?:password|passwd|pwd|secret|token|apikey|api_key|none|null|"
    r"undefined|true|false|changeme)$"
    r")"
)

#: Values that are references rather than literals.  A credential assigned
#: from the environment or from settings is exactly what the scanner wants to
#: encourage.
REFERENCE_PREFIXES = (
    "$",
    "{{",
    "os.",
    "env.",
    "environ",
    "getenv",
    "self.",
    "cls.",
    "settings.",
    "config.",
    "process.env",
    "import.meta",
    "None",
    "null",
    "undefined",
    "http://",
    "https://",
    "/",
    "./",
    "../",
    "~/",
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][\w.]*$")
_CALL_OR_SUBSCRIPT_RE = re.compile(r"^[A-Za-z_][\w.]*[\(\[].*$")
_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True)
class Rule:
    """One pattern with the name that is safe to print."""

    name: str
    pattern: re.Pattern[str]


RULES = (
    Rule(
        "private-key",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"),
    ),
    Rule(
        "aws-access-key",
        re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])"),
    ),
    Rule(
        "github-token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    ),
    Rule(
        "github-token",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
    ),
    Rule(
        "slack-token",
        re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    ),
    Rule(
        "bearer-token",
        re.compile(r"(?i)\bBearer\s+(?P<value>[A-Za-z0-9._~+/=-]{20,})"),
    ),
    Rule(
        "generic-secret",
        re.compile(
            r"(?i)\b(?P<key>"
            r"password|passwd|pwd|"
            r"secret|client[_-]?secret|secret[_-]?key|"
            r"token|access[_-]?token|auth[_-]?token|refresh[_-]?token|"
            r"api[_-]?key|apikey|x[_-]?api[_-]?key|"
            r"access[_-]?key(?:[_-]?id)?|"
            r"aws[_-]?secret[_-]?access[_-]?key|"
            r"private[_-]?key"
            r")\b[\"']?\s*[:=]\s*(?P<quote>[\"']?)"
            r"(?P<value>[^\s\"'#;,)\]}{]{8,})(?P=quote)"
        ),
    ),
)

#: Path allowlist.  Each entry is ``(posix glob, rules, reason)``; a rule not
#: named here is still enforced in that path, and ``*`` would disable all
#: rules -- which no entry does.  Keep this list tiny and explain every entry:
#: a broad path exemption is how a real secret gets missed.
ALLOWLIST: tuple[tuple[str, frozenset[str], str], ...] = (
    (
        # Fixtures deliberately contain fake credentials ("secret-token",
        # "Bearer ...") to exercise redaction and auth code.  Only the two
        # heuristic rules are relaxed: a private key, AWS/GitHub/Slack token
        # or keystore inside tests/ is still reported.
        "tests/*",
        frozenset({"generic-secret", "bearer-token"}),
        "test fixtures contain synthetic credentials",
    ),
    (
        "android/app/src/test/*",
        frozenset({"generic-secret"}),
        "unit-test fixtures contain synthetic credentials",
    ),
    (
        "android/app/src/androidTest/*",
        frozenset({"generic-secret"}),
        "instrumentation fixtures contain synthetic credentials",
    ),
    (
        # Upstream musicdl publishes these third-party app ids/secrets in its
        # source; they are not this project's credentials.  Only the generic
        # assignment rule is relaxed here -- private keys and provider tokens
        # are still reported inside the vendored tree.
        "src/novel_downloader/apps/music/providers/_vendor/musicdl/*",
        frozenset({"generic-secret"}),
        "vendored upstream public client constants",
    ),
    (
        # The release-asset auditor is tested against a synthetic PEM header;
        # the fixture contains no key material.
        "tests/test_audit_release_assets.py",
        frozenset({"private-key"}),
        "synthetic PEM header fixture",
    ),
)


@dataclass(frozen=True)
class Finding:
    """A single hit, carrying only the location and the rule name."""

    path: str
    line: int
    rule: str

    def as_dict(self) -> dict[str, str | int]:
        return {"path": self.path, "line": self.line, "rule": self.rule}


@dataclass(frozen=True)
class HistoryFinding:
    """A hit in a historical commit; the commit is part of the location."""

    commit: str
    path: str
    line: int
    rule: str

    def as_dict(self) -> dict[str, str | int]:
        return {
            "commit": self.commit,
            "path": self.path,
            "line": self.line,
            "rule": self.rule,
        }


@dataclass
class HistoryReport:
    """Findings plus what the walk had to skip."""

    findings: list[HistoryFinding]
    commits_scanned: int
    skipped_commits: int
    truncated: bool


def _run_git(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(detail or f"git {' '.join(args)} failed")
    return result.stdout


def iter_tracked_files(root: Path) -> list[str]:
    """Return every path in the index, as POSIX-style relative strings."""
    output = _run_git(root, "ls-files", "-z")
    names = output.decode("utf-8", "surrogateescape").split("\0")
    return sorted(name for name in names if name)


def iter_staged_files(root: Path) -> list[str]:
    """Return added/copied/modified/renamed paths in the index."""
    output = _run_git(
        root,
        "diff",
        "--cached",
        "--name-only",
        "--diff-filter=ACMR",
        "-z",
    )
    names = output.decode("utf-8", "surrogateescape").split("\0")
    return sorted(name for name in names if name)


def _is_allowlisted(rel_path: str, rule: str) -> bool:
    lowered = rel_path.lower()
    for pattern, rules, _reason in ALLOWLIST:
        if rule in rules and fnmatch.fnmatchcase(lowered, pattern.lower()):
            return True
    return False


def _is_text_candidate(rel_path: str) -> bool:
    path = Path(rel_path)
    name = path.name
    if name in TEXT_NAMES or name.startswith(".env."):
        return True
    return path.suffix.lower() in TEXT_SUFFIXES


def _decode(data: bytes) -> str | None:
    """Decode a text file, or return ``None`` for binary/oversized content."""
    if len(data) > MAX_FILE_BYTES:
        return None
    if b"\x00" in data[:BINARY_PROBE_BYTES]:
        return None
    return data.decode("utf-8", "replace")


def _read_worktree(root: Path, rel_path: str) -> bytes | None:
    path = root / rel_path
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _read_index(root: Path, rel_path: str) -> bytes | None:
    """Read the staged blob, not the working tree, so ``--staged`` is exact."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "show", f":{rel_path}"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0 or len(result.stdout) > MAX_FILE_BYTES:
        return None
    return result.stdout


def _is_benign_value(value: str, quoted: bool) -> bool:
    """Decide whether an assignment value is a placeholder or a reference."""
    if PLACEHOLDER_RE.search(value):
        return True
    if value.startswith(REFERENCE_PREFIXES):
        return True
    if _WINDOWS_PATH_RE.match(value):
        return True
    if quoted:
        return False
    if any(char in value for char in "&=?()+[]{}"):
        # Unquoted query strings, arithmetic and subscript expressions are
        # code, not credentials; quoted base64 values are still scanned.
        return True
    return bool(_IDENTIFIER_RE.match(value) or _CALL_OR_SUBSCRIPT_RE.match(value))


def _matches(rule: Rule, line: str) -> bool:
    match = rule.pattern.search(line)
    if match is None:
        return False
    if rule.name == "generic-secret":
        return not _is_benign_value(
            match.group("value"), quoted=bool(match.group("quote"))
        )
    if rule.name == "bearer-token":
        value = match.group("value")
        return not PLACEHOLDER_RE.search(value)
    return True


def scan_text(rel_path: str, text: str) -> list[Finding]:
    """Scan one file's text; returns at most one hit per rule per line."""
    findings: list[Finding] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for rule in RULES:
            if _is_allowlisted(rel_path, rule.name):
                continue
            if _matches(rule, line):
                findings.append(Finding(rel_path, line_no, rule.name))
    return findings


def _filename_findings(rel_path: str) -> list[Finding]:
    if _is_allowlisted(rel_path, "sensitive-file"):
        return []
    if SENSITIVE_FILENAME_RE.search(rel_path):
        # Line 0 marks a whole-file finding.
        return [Finding(rel_path, 0, "sensitive-file")]
    return []


def scan_paths(root: Path, paths: list[str], *, staged: bool = False) -> list[Finding]:
    """Scan *paths* relative to *root*, reading the index when *staged*."""
    findings: list[Finding] = []
    for rel_path in paths:
        findings.extend(_filename_findings(rel_path))
        if not _is_text_candidate(rel_path):
            continue
        data = _read_index(root, rel_path) if staged else _read_worktree(root, rel_path)
        if data is None:
            continue
        text = _decode(data)
        if text is not None:
            findings.extend(scan_text(rel_path, text))
    return findings


def scan_repository(root: Path, *, staged: bool = False) -> list[Finding]:
    """Scan tracked files (or the staged subset) under *root*."""
    paths = iter_staged_files(root) if staged else iter_tracked_files(root)
    findings = scan_paths(root, paths, staged=staged)
    return sorted(findings, key=lambda item: (item.path, item.line, item.rule))


_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

#: Format marker: a NUL byte cannot appear in a commit message or a text patch,
#: so it delimits commit records without buffering the whole log.
_COMMIT_MARKER = b"\x00"


def _decode_git_path(raw: str) -> str | None:
    """Turn a diff header path into a repository-relative path.

    ``/dev/null`` (a whole-file deletion) maps to ``None``.  Git quotes paths
    that contain special characters with C escapes; those are decoded back to
    text, with octal escapes carrying UTF-8 bytes.
    """
    raw = raw.strip()
    if not raw or raw == "/dev/null":
        return None
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        inner = raw[1:-1]
        try:
            unquoted = inner.encode("ascii").decode("unicode_escape")
            raw = unquoted.encode("latin-1").decode("utf-8", "replace")
        except (UnicodeDecodeError, UnicodeEncodeError):
            raw = inner
    if raw.startswith(("a/", "b/")):
        return raw[2:]
    return raw


def _diff_b_path(line: str) -> str | None:
    """Best-effort new-side path from a ``diff --git`` header.

    The header is unambiguous only for paths without spaces; this is used for
    the filename rule (binary files have no ``+++`` header), so a miss is a
    missed keystore name, never a wrong line hit.
    """
    _, marker, tail = line.rpartition(" b/")
    if not marker or not tail:
        return None
    return _decode_git_path("b/" + tail)


class _HistoryScanner:
    """Incremental parser for ``git log -p`` output.

    Lines are fed one at a time so an oversized commit can be abandoned
    without buffering its patch.  Only added lines (``+``) are scanned; the
    diff line numbers come from the hunk header of the new side.
    """

    def __init__(self) -> None:
        self.findings: list[HistoryFinding] = []
        self.commits_scanned = 0
        self.skipped_commits = 0
        self.truncated = False
        self._total_bytes = 0
        self._commit: str | None = None
        self._commit_bytes = 0
        self._skipped = False
        self._path: str | None = None
        self._scan_path = False
        self._line = 0
        self._in_hunk = False
        self._sensitive_paths: set[str] = set()

    def feed(self, raw: bytes) -> bool:
        """Consume one output line; return ``False`` once the total limit hits."""
        if raw.startswith(_COMMIT_MARKER):
            self._begin(raw[1:])
            return True
        if self._commit is None:
            return True
        self._total_bytes += len(raw)
        if self._total_bytes > MAX_TOTAL_PATCH_BYTES:
            self.truncated = True
            return False
        self._commit_bytes += len(raw)
        if self._skipped:
            return True
        if self._commit_bytes > MAX_COMMIT_PATCH_BYTES:
            self._skipped = True
            self.skipped_commits += 1
            self._path = None
            self._scan_path = False
            self._in_hunk = False
            return True
        self._feed_line(raw)
        return True

    def _begin(self, raw: bytes) -> None:
        commit = raw.decode("ascii", "replace").strip()
        if not commit:
            return
        self.commits_scanned += 1
        self._commit = commit
        self._commit_bytes = 0
        self._skipped = False
        self._path = None
        self._scan_path = False
        self._line = 0
        self._in_hunk = False
        self._sensitive_paths = set()

    def _feed_line(self, raw: bytes) -> None:
        text = raw.decode("utf-8", "replace").rstrip("\r\n")
        # ``diff --git`` is the only structural line that is never confused
        # with hunk content, because every content line is prefixed.
        if text.startswith("diff --git "):
            self._path = None
            self._scan_path = False
            self._in_hunk = False
            self._note_sensitive_path(_diff_b_path(text))
            return
        if self._in_hunk:
            self._feed_hunk_line(text)
            return
        if text.startswith("+++ "):
            self._path = _decode_git_path(text[4:])
            self._scan_path = bool(
                self._path is not None and _is_text_candidate(self._path)
            )
            self._note_sensitive_path(self._path)
            return
        if text.startswith("@@"):
            match = _HUNK_HEADER_RE.match(text)
            if match is not None and self._path is not None:
                self._line = int(match.group(1))
                self._in_hunk = True

    def _feed_hunk_line(self, text: str) -> None:
        if text.startswith("\\"):
            # "\ No newline at end of file" belongs to neither side.
            return
        if text.startswith("-"):
            return
        if not text.startswith("+"):
            # Context line; -U0 normally emits none, but be correct if set.
            self._line += 1
            return
        content = text[1:]
        path = self._path
        commit = self._commit
        if commit is not None and path is not None and self._scan_path:
            for rule in RULES:
                if _is_allowlisted(path, rule.name):
                    continue
                if _matches(rule, content):
                    self.findings.append(
                        HistoryFinding(commit, path, self._line, rule.name)
                    )
        self._line += 1

    def _note_sensitive_path(self, path: str | None) -> None:
        commit = self._commit
        if commit is None or path is None or path in self._sensitive_paths:
            return
        if _filename_findings(path):
            self._sensitive_paths.add(path)
            self.findings.append(HistoryFinding(commit, path, 0, "sensitive-file"))


def scan_history(
    root: Path,
    *,
    since: str | None = None,
    max_commits: int | None = None,
) -> HistoryReport:
    """Scan the added lines of every commit reachable from ``HEAD``.

    ``since`` restricts the walk to ``REV..HEAD``; ``max_commits`` caps how
    many of the newest commits are read.  Nothing is checked out or rewritten.
    """
    scanner = _HistoryScanner()
    command = [
        "git",
        "-C",
        str(root),
        "-c",
        "core.quotepath=false",
        "log",
        "-p",
        "-U0",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--format=%x00%H",
    ]
    if max_commits is not None:
        command.append(f"--max-count={max_commits}")
    if since:
        command.append(f"{since}..HEAD")

    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=errors,
        )
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                if not scanner.feed(raw):
                    break
        finally:
            proc.stdout.close()
            if proc.poll() is None:
                proc.kill()
            returncode = proc.wait()
        errors.seek(0)
        detail = errors.read().decode("utf-8", "replace").strip()

    if returncode != 0 and not scanner.truncated:
        raise RuntimeError(detail or f"git log failed with exit code {returncode}")

    return HistoryReport(
        findings=scanner.findings,
        commits_scanned=scanner.commits_scanned,
        skipped_commits=scanner.skipped_commits,
        truncated=scanner.truncated,
    )


def _render_history(report: HistoryReport) -> str:
    lines = [
        f"{item.commit[:12]}:{item.path}:{item.line}: {item.rule}"
        for item in report.findings
    ]
    if report.findings:
        commits = len({item.commit for item in report.findings})
        summary = (
            f"scan-secrets: {len(report.findings)} finding(s) in {commits} commit(s)"
        )
    else:
        summary = "scan-secrets: no findings in history"
    summary += f" ({report.commits_scanned} commit(s) scanned)"
    if report.skipped_commits:
        summary += f", {report.skipped_commits} skipped (patch too large)"
    if report.truncated:
        summary += ", stopped at size limit"
    lines.append(summary)
    return "\n".join(lines)


def _render_text(findings: list[Finding]) -> str:
    lines = [f"{item.path}:{item.line}: {item.rule}" for item in findings]
    if findings:
        files = len({item.path for item in findings})
        lines.append(f"scan-secrets: {len(findings)} finding(s) in {files} file(s)")
    else:
        lines.append("scan-secrets: no findings")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Scan files tracked by Git for credentials. "
            "Findings never include the matched value."
        )
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print a JSON report instead of one line per finding",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="scan only added/copied/modified/renamed index entries",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="read-only scan of lines added by past commits (git log -p)",
    )
    parser.add_argument(
        "--since",
        metavar="REV",
        help="with --history: scan only commits after REV (REV..HEAD)",
    )
    parser.add_argument(
        "--max-commits",
        type=int,
        metavar="N",
        help="with --history: scan at most the N newest commits",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help="repository root (defaults to the repository containing this script)",
    )
    args = parser.parse_args(argv)

    if args.staged and args.history:
        print(
            "scan-secrets: error: --staged and --history are mutually exclusive",
            file=sys.stderr,
        )
        return EXIT_FINDINGS
    if not args.history and (args.since is not None or args.max_commits is not None):
        print(
            "scan-secrets: error: --since and --max-commits require --history",
            file=sys.stderr,
        )
        return EXIT_FINDINGS
    if args.max_commits is not None and args.max_commits < 1:
        print(
            "scan-secrets: error: --max-commits must be at least 1",
            file=sys.stderr,
        )
        return EXIT_FINDINGS

    if args.history:
        try:
            report = scan_history(
                args.root, since=args.since, max_commits=args.max_commits
            )
        except (OSError, RuntimeError) as exc:
            print(f"scan-secrets: error: {exc}", file=sys.stderr)
            return EXIT_FINDINGS

        if args.json:
            payload = {
                "count": len(report.findings),
                "findings": [item.as_dict() for item in report.findings],
                "commits_scanned": report.commits_scanned,
                "skipped_commits": report.skipped_commits,
                "truncated": report.truncated,
            }
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print(_render_history(report))
        return EXIT_FINDINGS if report.findings else EXIT_OK

    try:
        findings = scan_repository(args.root, staged=args.staged)
    except (OSError, RuntimeError) as exc:
        print(f"scan-secrets: error: {exc}", file=sys.stderr)
        return EXIT_FINDINGS

    if args.json:
        payload = {
            "count": len(findings),
            "findings": [item.as_dict() for item in findings],
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(_render_text(findings))

    return EXIT_FINDINGS if findings else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
