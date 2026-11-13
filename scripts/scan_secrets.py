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

Usage::

    python scripts/scan_secrets.py
    python scripts/scan_secrets.py --json
    python scripts/scan_secrets.py --staged

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
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help="repository root (defaults to the repository containing this script)",
    )
    args = parser.parse_args(argv)

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
