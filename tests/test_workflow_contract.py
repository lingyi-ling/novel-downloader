"""Supply-chain invariants for the GitHub Actions workflows.

The files under ``.github/workflows/`` are treated here as a *specification*:
they are parsed as plain text (the project has no PyYAML dependency) and the
security-relevant properties are asserted directly.  A careless edit then
fails a test instead of quietly widening the attack surface.

Every failure names the offending ``file:line``, quotes the matched text and
suggests one concrete fix.  Assertions describe behaviour ("no checkout keeps
credentials"), never brittle counts ("there are exactly three of X").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
PYPROJECT = ROOT / "pyproject.toml"

# The complete set of workflow entry points.  A new file means a new trigger,
# a new token scope and possibly new secrets, so it must be audited first.
EXPECTED_WORKFLOWS = frozenset({"build.yml", "ci.yml", "release.yml"})

# ``actions/checkout`` steps allowed to keep the workflow token in
# ``.git/config``.  Intentionally empty: ``release.yml`` publishes through
# ``scripts/release.py``, whose ``push_tag`` uses an explicit token URL (and is
# a no-op when the tag is already on the remote), so no workflow needs
# persisted git credentials.  Add ``{"<workflow name>": "<reason>"}`` only for a
# checkout that genuinely has to push.
CHECKOUT_CREDENTIAL_EXCEPTIONS: dict[str, str] = {}

FULL_COMMIT_SHA = re.compile(r"@[0-9a-f]{40}$")
DOCKER_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
USES_LINE = re.compile(r"^(?P<indent>\s*)(?:-\s+)?uses:\s*(?P<ref>\S+)\s*(?:#.*)?$")
RUN_BLOCK = re.compile(r"^(?P<indent>\s*)(?:-\s+)?run:\s*[|>][+-]?\d*\s*$")
RUN_INLINE = re.compile(r"^(?P<indent>\s*)(?:-\s+)?run:\s*(?P<value>[^|>].*?)\s*$")
GH_RELEASE_UPLOAD = re.compile(r"\bgh\s+release\s+upload\b")
CURL_PIPE_SHELL = re.compile(
    r"\bcurl\b[^\n]*\|\s*(?:sudo\s+)?(?:bash|sh|zsh|ksh|dash|python3?)\b"
)
FORCE_PUSH = re.compile(
    r"\bgit\s+push\b[^\n]*(?<!\S)(?:--force(?!-with-lease)|-f)(?=\s|$)"
)
FORCE_WITH_LEASE = re.compile(r"\bgit\s+push\b[^\n]*(?<!\S)--force-with-lease\b")


@dataclass(frozen=True)
class Violation:
    """One broken invariant, rendered as ``file:line`` + fix hint."""

    path: Path
    line: int
    detail: str
    fix: str

    def render(self) -> str:
        where = f"{self.path.relative_to(ROOT).as_posix()}:{self.line}"
        return f"{where}: {self.detail}\n    fix: {self.fix}"


def assert_clean(violations: list[Violation]) -> None:
    """Fail with every violation rendered, so one run fixes them all."""
    if violations:
        rendered = "\n".join(violation.render() for violation in violations)
        pytest.fail(f"{len(violations)} supply-chain violation(s):\n{rendered}")


def workflow_files() -> list[Path]:
    assert WORKFLOW_DIR.is_dir(), f"missing workflow directory: {WORKFLOW_DIR}"
    files = sorted(
        path for path in WORKFLOW_DIR.iterdir() if path.suffix in {".yml", ".yaml"}
    )
    assert files, f"no workflow files found in {WORKFLOW_DIR}"
    return files


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def iter_uses(path: Path) -> list[tuple[int, str]]:
    """Return ``(line number, reference)`` for every ``uses:`` in *path*."""
    references: list[tuple[int, str]] = []
    for lineno, line in enumerate(read_lines(path), 1):
        match = USES_LINE.match(line)
        if match:
            references.append((lineno, match.group("ref")))
    return references


def iter_run_scripts(path: Path) -> list[tuple[int, str]]:
    """Every physical line that belongs to a ``run:`` script.

    Handles block scalars (``run: |`` / ``run: >-``, body delimited by
    indentation) as well as the single-line form ``run: command``.
    """
    lines = read_lines(path)
    scripts: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        block = RUN_BLOCK.match(lines[index])
        if block:
            base_indent = len(block.group("indent"))
            index += 1
            while index < len(lines):
                line = lines[index]
                indented = len(line) - len(line.lstrip())
                if line.strip() and indented <= base_indent:
                    break
                scripts.append((index + 1, line))
                index += 1
            continue
        inline = RUN_INLINE.match(lines[index])
        if inline:
            scripts.append((index + 1, inline.group("value")))
        index += 1
    return scripts


def top_level_value(path: Path, key: str) -> tuple[int, str, list[str]] | None:
    """Locate a top-level ``key:`` and return (line, inline value, block)."""
    lines = read_lines(path)
    prefix = f"{key}:"
    for index, line in enumerate(lines):
        if not line.startswith(prefix):
            continue
        inline = line[len(prefix) :].split("#", 1)[0].strip()
        block: list[str] = []
        for candidate in lines[index + 1 :]:
            if candidate.strip() and not candidate.startswith((" ", "\t")):
                break
            block.append(candidate)
        return index + 1, inline, block
    return None


def top_level_permissions(path: Path) -> tuple[int, dict[str, str], str]:
    """Parse the top-level ``permissions:`` mapping (or its scalar form)."""
    found = top_level_value(path, "permissions")
    assert found is not None, (
        f"{path.relative_to(ROOT).as_posix()}: no top-level `permissions:` "
        "block; declare the least privilege the workflow needs"
    )
    lineno, inline, block = found
    if inline:
        return lineno, {}, inline
    entries: dict[str, str] = {}
    for line in block:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^([\w-]+):\s*(\S+)$", stripped)
        if match:
            entries[match.group(1)] = match.group(2)
    return lineno, entries, ""


def iter_checkout_steps(path: Path) -> list[tuple[int, str, list[tuple[int, str]]]]:
    """Return ``(line, reference, body)`` for each ``actions/checkout`` step."""
    lines = read_lines(path)
    steps: list[tuple[int, str, list[tuple[int, str]]]] = []
    index = 0
    while index < len(lines):
        match = USES_LINE.match(lines[index])
        if not match or not match.group("ref").startswith("actions/checkout@"):
            index += 1
            continue
        step_indent = len(match.group("indent"))
        body: list[tuple[int, str]] = []
        cursor = index + 1
        while cursor < len(lines):
            line = lines[cursor]
            if line.strip() and len(line) - len(line.lstrip()) <= step_indent:
                break
            body.append((cursor + 1, line))
            cursor += 1
        steps.append((index + 1, match.group("ref"), body))
        index = cursor
    return steps


def parse_version(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)", text.strip().strip("\"'"))
    assert match, f"unparsable Python version: {text!r}"
    return int(match.group(1)), int(match.group(2))


# ---------------------------------------------------------------------------
# 1. Actions are pinned to an immutable commit
# ---------------------------------------------------------------------------


def test_actions_are_pinned_to_commit_shas() -> None:
    violations: list[Violation] = []
    for path in workflow_files():
        for lineno, reference in iter_uses(path):
            if reference.startswith("./"):
                continue
            if reference.startswith("docker://"):
                if not DOCKER_DIGEST.search(reference):
                    violations.append(
                        Violation(
                            path,
                            lineno,
                            f"`uses: {reference}` is not pinned to an image digest",
                            "pin the audited image with `@sha256:<64 hex>`",
                        )
                    )
                continue
            if FULL_COMMIT_SHA.search(reference):
                continue
            repo, _, tag = reference.partition("@")
            if tag:
                fix = (
                    f"pin it: `uses: {repo}@<40-char-commit-sha> # {tag}` "
                    f"(resolve with `gh api repos/{repo}/commits/{tag} "
                    "--jq .sha`)"
                )
            else:
                fix = (
                    f"pin it: `uses: {repo}@<40-char-commit-sha> # <tag>` "
                    f"(resolve with `gh api repos/{repo}/commits/<tag> "
                    "--jq .sha`)"
                )
            violations.append(
                Violation(
                    path,
                    lineno,
                    f"`uses: {reference}` is not pinned to a 40-char commit "
                    "SHA; a tag can be moved to different code",
                    fix,
                )
            )
    assert_clean(violations)


# ---------------------------------------------------------------------------
# 2. Fork pull requests never run with the base repository's secrets
# ---------------------------------------------------------------------------


def test_pull_request_target_is_forbidden() -> None:
    violations: list[Violation] = []
    for path in workflow_files():
        for lineno, line in enumerate(read_lines(path), 1):
            if "pull_request_target" in line and not line.lstrip().startswith("#"):
                violations.append(
                    Violation(
                        path,
                        lineno,
                        f"`pull_request_target` is used: {line.strip()!r}",
                        "use `pull_request` instead; `pull_request_target` gives "
                        "fork PRs the base repository's secrets and token",
                    )
                )
    assert_clean(violations)


# ---------------------------------------------------------------------------
# 3. Shell scripts never interpolate expressions
# ---------------------------------------------------------------------------


def test_run_blocks_do_not_interpolate_expressions() -> None:
    violations: list[Violation] = []
    for path in workflow_files():
        for lineno, line in iter_run_scripts(path):
            if "${{" in line:
                violations.append(
                    Violation(
                        path,
                        lineno,
                        f"`run:` script interpolates an expression: {line.strip()!r}",
                        "put the value in the step's `env:` mapping and read it "
                        "as `$NAME` in the script (direct interpolation allows "
                        "shell injection from branch names, inputs or PR titles)",
                    )
                )
    assert_clean(violations)


# ---------------------------------------------------------------------------
# 4. Token permissions are declared and minimal
# ---------------------------------------------------------------------------


def test_ci_declares_read_only_contents_permission() -> None:
    path = WORKFLOW_DIR / "ci.yml"
    assert path.is_file(), f"missing {path.relative_to(ROOT).as_posix()}"
    lineno, entries, inline = top_level_permissions(path)
    mutations = sorted(scope for scope, access in entries.items() if access == "write")
    if inline or entries.get("contents") != "read" or mutations:
        declared = inline or entries or "no scope"
        assert_clean(
            [
                Violation(
                    path,
                    lineno,
                    f"CI must declare exactly `contents: read`; found {declared}",
                    "replace the top-level `permissions:` with `contents: read`",
                )
            ]
        )


def test_no_workflow_requests_write_all() -> None:
    violations: list[Violation] = []
    for path in workflow_files():
        for lineno, line in enumerate(read_lines(path), 1):
            match = re.match(r"^\s*permissions:\s*(\S+)", line)
            if match and match.group(1) == "write-all":
                violations.append(
                    Violation(
                        path,
                        lineno,
                        "`permissions: write-all` grants every scope to every step",
                        "name the scopes you need (for example `contents: read`)",
                    )
                )
            stripped = line.strip()
            if stripped == "write-all":
                violations.append(
                    Violation(
                        path,
                        lineno,
                        "the permissions mapping grants `write-all`",
                        "grant only the scopes this workflow uses",
                    )
                )
    assert_clean(violations)


def test_release_declares_contents_write_permission() -> None:
    path = WORKFLOW_DIR / "release.yml"
    assert path.is_file(), f"missing {path.relative_to(ROOT).as_posix()}"
    lineno, entries, inline = top_level_permissions(path)
    if inline:
        if inline == "read-all":
            assert_clean(
                [
                    Violation(
                        path,
                        lineno,
                        "release publishing cannot run with `permissions: read-all`",
                        "replace it with the explicit `contents: write` the "
                        "publisher needs",
                    )
                ]
            )
        return
    if entries.get("contents") != "write":
        assert_clean(
            [
                Violation(
                    path,
                    lineno,
                    "the release workflow must declare `contents: write` to "
                    f"create releases and upload assets; found {entries or 'nothing'}",
                    "add `contents: write` under the top-level `permissions:`",
                )
            ]
        )


# ---------------------------------------------------------------------------
# 5. Publishing is never reachable from a pull request
# ---------------------------------------------------------------------------


def test_release_is_not_triggered_by_pull_request() -> None:
    path = WORKFLOW_DIR / "release.yml"
    assert path.is_file(), f"missing {path.relative_to(ROOT).as_posix()}"
    found = top_level_value(path, "on")
    assert found is not None, (
        f"{path.relative_to(ROOT).as_posix()}: no top-level `on:` trigger block; "
        "fix: declare the allowed triggers (for example `workflow_dispatch:`)"
    )
    lineno, inline, block = found
    trigger = "\n".join([inline, *block])
    if "pull_request" in trigger:
        matched = inline or block[0].strip()
        assert_clean(
            [
                Violation(
                    path,
                    lineno,
                    "the release workflow is reachable from a pull request "
                    f"({matched!r})",
                    "trigger publishing only from `workflow_dispatch` (or a "
                    "tag); remove `pull_request` so PR code cannot publish",
                )
            ]
        )


# ---------------------------------------------------------------------------
# 6. Publishing goes through scripts/release.py, not ad-hoc shell downloads
# ---------------------------------------------------------------------------


def test_no_gh_release_upload_or_curl_pipe_shell() -> None:
    violations: list[Violation] = []
    for path in workflow_files():
        for lineno, line in iter_run_scripts(path):
            if GH_RELEASE_UPLOAD.search(line):
                violations.append(
                    Violation(
                        path,
                        lineno,
                        f"`gh release upload` bypasses the audited publisher: "
                        f"{line.strip()!r}",
                        "publish through `scripts/release.py` so checksums and "
                        "asset names stay consistent",
                    )
                )
            if CURL_PIPE_SHELL.search(line):
                violations.append(
                    Violation(
                        path,
                        lineno,
                        f"downloading and piping straight into a shell: "
                        f"{line.strip()!r}",
                        "download the file, verify a pinned SHA-256, then run it "
                        "(or use a pinned action)",
                    )
                )
    assert_clean(violations)


# ---------------------------------------------------------------------------
# 7. No history rewriting from CI
# ---------------------------------------------------------------------------


def test_no_force_push_to_protected_branches() -> None:
    violations: list[Violation] = []
    for path in workflow_files():
        for lineno, line in iter_run_scripts(path):
            if FORCE_PUSH.search(line) or FORCE_WITH_LEASE.search(line):
                violations.append(
                    Violation(
                        path,
                        lineno,
                        f"force-pushing from CI rewrites shared history: "
                        f"{line.strip()!r}",
                        "drop the force flag; append a commit or open a PR "
                        "instead of overwriting the protected branch",
                    )
                )
    assert_clean(violations)


# ---------------------------------------------------------------------------
# 8. Checkouts do not leave the workflow token in .git/config
# ---------------------------------------------------------------------------


def test_checkout_steps_do_not_persist_credentials() -> None:
    violations: list[Violation] = []
    for path in workflow_files():
        exception = CHECKOUT_CREDENTIAL_EXCEPTIONS.get(path.name)
        for lineno, reference, body in iter_checkout_steps(path):
            if any("persist-credentials: false" in line for _, line in body):
                continue
            if exception:
                continue
            violations.append(
                Violation(
                    path,
                    lineno,
                    f"`uses: {reference}` keeps the workflow token in "
                    ".git/config for every later step",
                    "add `persist-credentials: false` under the step's `with:`; "
                    "a checkout that must push is an explicit exception and "
                    "needs a documented reason",
                )
            )
    assert_clean(violations)


# ---------------------------------------------------------------------------
# 9. The set of publishing entry points is fixed
# ---------------------------------------------------------------------------


def test_workflow_file_set_is_fixed() -> None:
    actual = {path.name for path in workflow_files()}
    violations = [
        Violation(
            WORKFLOW_DIR / name,
            1,
            f"unexpected workflow `{name}` adds a new publishing entry point",
            "audit it first (triggers, permissions, secrets), then add it to "
            "EXPECTED_WORKFLOWS in tests/test_workflow_contract.py",
        )
        for name in sorted(actual - EXPECTED_WORKFLOWS)
    ]
    violations.extend(
        Violation(
            WORKFLOW_DIR / name,
            1,
            f"expected workflow `{name}` is missing",
            "restore it, or update EXPECTED_WORKFLOWS once the invariant it "
            "protected is covered elsewhere",
        )
        for name in sorted(EXPECTED_WORKFLOWS - actual)
    )
    assert_clean(violations)


# ---------------------------------------------------------------------------
# 10. The CI matrix stays inside the supported Python range
# ---------------------------------------------------------------------------


def test_ci_python_matrix_matches_pyproject() -> None:
    ci = WORKFLOW_DIR / "ci.yml"
    assert ci.is_file(), f"missing {ci.relative_to(ROOT).as_posix()}"
    matrix: list[tuple[int, int]] = []
    matrix_line = 0
    for lineno, line in enumerate(read_lines(ci), 1):
        match = re.search(r"python-version:\s*\[([^\]]*)\]", line)
        if match:
            matrix_line = lineno
            matrix = [parse_version(item) for item in match.group(1).split(",") if item]
            break
    assert matrix_line, (
        f"{ci.relative_to(ROOT).as_posix()}: no `python-version: [...]` matrix "
        "found; fix: list the supported runtimes explicitly, e.g. "
        '`python-version: ["3.11", "3.12"]`'
    )

    text = PYPROJECT.read_text(encoding="utf-8")
    spec_match = re.search(r'^requires-python\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert spec_match is not None, (
        f"{PYPROJECT.relative_to(ROOT).as_posix()}: missing `requires-python`; "
        'fix: add `requires-python = ">=X.Y"` under `[project]`'
    )
    floor_match = re.search(r">=\s*(\d+)\.(\d+)", spec_match.group(1))
    assert floor_match is not None, (
        f"{PYPROJECT.relative_to(ROOT).as_posix()}: `requires-python = "
        f"{spec_match.group(1)!r}` has no `>=X.Y` lower bound; fix: state the "
        "minimum version as `>=X.Y`"
    )
    floor = (int(floor_match.group(1)), int(floor_match.group(2)))
    floor_line = text.count("\n", 0, spec_match.start()) + 1

    classifier_matches = list(
        re.finditer(r'"Programming Language :: Python :: (\d+)\.(\d+)"', text)
    )
    assert classifier_matches, (
        f"{PYPROJECT.relative_to(ROOT).as_posix()}: no supported-Python "
        "classifiers; fix: add `Programming Language :: Python :: X.Y` for "
        "every runtime CI tests"
    )
    classifiers: dict[tuple[int, int], int] = {}
    for match in classifier_matches:
        version = (int(match.group(1)), int(match.group(2)))
        classifiers[version] = text.count("\n", 0, match.start()) + 1

    violations: list[Violation] = []
    for version in matrix:
        label = f"{version[0]}.{version[1]}"
        if version < floor:
            violations.append(
                Violation(
                    ci,
                    matrix_line,
                    f"CI tests Python {label}, but `requires-python` only "
                    f"supports >={floor[0]}.{floor[1]}",
                    "drop it from the matrix or lower `requires-python` in "
                    "pyproject.toml",
                )
            )
        if version not in classifiers:
            violations.append(
                Violation(
                    ci,
                    matrix_line,
                    f"CI tests Python {label}, which pyproject.toml does not "
                    "advertise in its classifiers",
                    f"add `Programming Language :: Python :: {label}` to the "
                    "classifiers, or drop it from the matrix",
                )
            )
    for version, lineno in classifiers.items():
        if version < floor:
            violations.append(
                Violation(
                    PYPROJECT,
                    lineno,
                    f"classifier advertises Python "
                    f"{version[0]}.{version[1]}, below the "
                    f">={floor[0]}.{floor[1]} floor at line {floor_line}",
                    "remove the classifier or lower `requires-python`",
                )
            )
    assert_clean(violations)


class TestPosixWrapper:
    """The Linux/macOS wrapper is exercised by CI and never locally."""

    def test_gradlew_does_not_embed_quotes_in_jvm_options(self) -> None:
        # ``DEFAULT_JVM_OPTS`` is expanded unquoted, so ``'"-Xmx64m"'`` reaches
        # java as a literal argument and the launcher looks for a main class
        # named "-Xmx64m" (exit 1 on Linux; the .bat wrapper is unaffected).
        script = (ROOT / "android" / "gradlew").read_text(encoding="utf-8")

        assert "'\"-" not in script
        assert 'DEFAULT_JVM_OPTS="-Xmx64m -Xms64m"' in script
