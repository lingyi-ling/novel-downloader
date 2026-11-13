"""Supply-chain invariants for the GitHub Actions workflows.

The files under ``.github/workflows/`` are treated here as a *specification*:
they are parsed as plain text (the project has no PyYAML dependency, so the
structural checks work everywhere) and the security-relevant properties are
asserted directly.  A careless edit then fails a test instead of quietly
widening the attack surface.  When PyYAML happens to be installed -- CI always
installs it for this reason -- the files are additionally parsed as real YAML
so a syntax error is reported with a line number instead of surfacing on the
next push.

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
EXPECTED_WORKFLOWS = frozenset({"build.yml", "ci.yml", "release.yml", "security.yml"})

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


def iter_action_steps(
    path: Path, action: str
) -> list[tuple[int, str, list[tuple[int, str]]]]:
    """Return ``(line, reference, body)`` for every step that uses *action*.

    ``action`` is the ``owner/repo`` part of ``uses:`` (no ``@ref``): the
    comparison ignores the pinned SHA so a version bump does not invalidate
    the contract.
    """
    lines = read_lines(path)
    prefix = f"{action}@"
    steps: list[tuple[int, str, list[tuple[int, str]]]] = []
    index = 0
    while index < len(lines):
        match = USES_LINE.match(lines[index])
        if not match or not match.group("ref").startswith(prefix):
            index += 1
            continue
        step_indent = len(match.group("indent"))
        body: list[tuple[int, str]] = []
        cursor = index + 1
        while cursor < len(lines):
            line = lines[cursor]
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            # A step may be written as `- name: ...` with `uses:` on the
            # following line; `with:`/`env:` then sit at the same indent as
            # `uses:` and still belong to the step.  The next `- ` item (or a
            # less-indented key, i.e. a job key) ends it.
            if stripped.startswith("- ") and indent <= step_indent:
                break
            if stripped and indent < step_indent:
                break
            body.append((cursor + 1, line))
            cursor += 1
        steps.append((index + 1, match.group("ref"), body))
        index = cursor
    return steps


def iter_checkout_steps(path: Path) -> list[tuple[int, str, list[tuple[int, str]]]]:
    """Return ``(line, reference, body)`` for each ``actions/checkout`` step."""
    return iter_action_steps(path, "actions/checkout")


@dataclass(frozen=True)
class Job:
    """One entry under the top-level ``jobs:`` mapping."""

    name: str
    start: int  # 1-based line of the job key
    end: int  # 1-based line of the job's last line
    body: list[str]

    def runs_on(self) -> str:
        """The ``runs-on:`` value (empty when the job inherits a default)."""
        for line in self.body:
            match = re.match(r"^\s{4}runs-on:\s*(\S+)", line)
            if match:
                return match.group(1).strip("\"'")
        return ""


def iter_jobs(path: Path) -> list[Job]:
    """Split a workflow into job blocks.

    Job keys sit at exactly two spaces under ``jobs:``; their bodies are more
    indented, and block scalars are deeper still, so this is unambiguous for
    the shapes used in this repository.
    """
    lines = read_lines(path)
    header = next(
        (index for index, line in enumerate(lines) if line.rstrip() == "jobs:"),
        None,
    )
    if header is None:
        return []
    keys: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if index <= header:
            continue
        match = re.match(r"^  ([\w.-]+):\s*(?:#.*)?$", line)
        if match:
            keys.append((index, match.group(1)))
    jobs: list[Job] = []
    for position, (index, name) in enumerate(keys):
        end = keys[position + 1][0] if position + 1 < len(keys) else len(lines)
        jobs.append(Job(name=name, start=index + 1, end=end, body=lines[index:end]))
    return jobs


def find_job(path: Path, name: str) -> Job:
    """Return the job called *name*, failing with the available ones."""
    jobs = {job.name: job for job in iter_jobs(path)}
    assert name in jobs, (
        f"{path.relative_to(ROOT).as_posix()}: no job named {name!r}; "
        f"jobs are {sorted(jobs)}"
    )
    return jobs[name]


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


class TestWorkflowSyntax:
    """The files must be valid YAML, not just regex-friendly text."""

    def test_no_tabs_in_indentation(self) -> None:
        violations: list[Violation] = []
        for path in workflow_files():
            for lineno, line in enumerate(read_lines(path), 1):
                indent = line[: len(line) - len(line.lstrip())]
                if "\t" in indent:
                    violations.append(
                        Violation(
                            path,
                            lineno,
                            f"tab character in indentation: {line.strip()!r}",
                            "YAML forbids tabs for indentation; replace them "
                            "with spaces (the file may look fine in an editor "
                            "but fail to parse on GitHub)",
                        )
                    )
        assert_clean(violations)

    def test_workflows_parse_as_yaml(self) -> None:
        # PyYAML is not a runtime dependency, but CI installs it explicitly so
        # this contract cannot silently disappear there; locally it is skipped
        # rather than failing an otherwise complete environment.
        yaml = pytest.importorskip("yaml")
        violations: list[Violation] = []
        for path in workflow_files():
            try:
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                mark = getattr(exc, "problem_mark", None)
                lineno = mark.line + 1 if mark is not None else 1
                violations.append(
                    Violation(
                        path,
                        lineno,
                        f"invalid YAML: {exc}",
                        "fix the syntax (unquoted colon, bad indent or a "
                        "misplaced block scalar); GitHub rejects the workflow "
                        "before any job runs",
                    )
                )
                continue
            if not isinstance(document, dict):
                violations.append(
                    Violation(
                        path,
                        1,
                        "the document is not a mapping",
                        "a workflow is a mapping with `name`, `on` and `jobs`",
                    )
                )
                continue
            if not isinstance(document.get("jobs"), dict) or not document["jobs"]:
                violations.append(
                    Violation(
                        path,
                        1,
                        "no non-empty `jobs:` mapping",
                        "declare at least one job; an empty workflow is a silent no-op",
                    )
                )
            # PyYAML parses the bare `on` key as boolean True (YAML 1.1).
            if not document.get("on") and not document.get(True):
                violations.append(
                    Violation(
                        path,
                        1,
                        "no `on:` trigger",
                        "declare the triggers; a workflow without `on` is invalid",
                    )
                )
        assert_clean(violations)


class TestWorkflowEnvironment:
    """``$NAME`` references in run blocks must resolve to a declared env var."""

    #: Shell/GitHub variables that exist without an ``env:`` entry.
    BUILTIN_ENV = frozenset(
        {
            "CI",
            "HOME",
            "PATH",
            "RUNNER_OS",
            "RUNNER_TEMP",
            "GITHUB_ENV",
            "GITHUB_OUTPUT",
            "GITHUB_PATH",
            "GITHUB_STEP_SUMMARY",
            "GITHUB_WORKSPACE",
            "GITHUB_SHA",
            "GITHUB_REF",
            "GITHUB_TOKEN",
        }
    )
    ENV_REF = re.compile(r"\$(?:\{([A-Z][A-Z0-9_]*)\}|([A-Z][A-Z0-9_]*))")

    @staticmethod
    def declared_env(path: Path) -> set[str]:
        # Every indented `UPPER_CASE:` key in the file: workflow-level `env:`
        # entries and step-level `env:` entries alike.  Over-approximating is
        # safe here; the goal is catching a reference to a renamed/removed var.
        return set(
            re.findall(
                r"^\s+([A-Z][A-Z0-9_]*):", "\n".join(read_lines(path)), re.MULTILINE
            )
        )

    def test_run_blocks_only_reference_declared_env(self) -> None:
        violations: list[Violation] = []
        for path in workflow_files():
            declared = self.declared_env(path)
            for lineno, line in iter_run_scripts(path):
                for braced, bare in self.ENV_REF.findall(line):
                    name = braced or bare
                    if name in self.BUILTIN_ENV or name in declared:
                        continue
                    violations.append(
                        Violation(
                            path,
                            lineno,
                            f"`run:` reads ${name}, which no `env:` block declares",
                            f"declare `{name}` under the workflow/job/step "
                            "`env:` block, or drop the reference",
                        )
                    )
        assert_clean(violations)

    def test_pip_cache_is_keyed_on_an_existing_manifest(self) -> None:
        violations: list[Violation] = []
        for path in workflow_files():
            for lineno, reference, body in iter_action_steps(
                path, "actions/setup-python"
            ):
                text = "\n".join(line for _, line in body)
                if not re.search(r"^\s*cache:\s*pip\s*$", text, re.MULTILINE):
                    continue
                dependency = re.search(
                    r"^\s*cache-dependency-path:\s*(\S+)", text, re.MULTILINE
                )
                if dependency is None:
                    violations.append(
                        Violation(
                            path,
                            lineno,
                            f"`uses: {reference}` enables the pip cache "
                            "without `cache-dependency-path`",
                            "set `cache-dependency-path: pyproject.toml`; the "
                            "default lookup finds no requirements file and "
                            "re-downloads every run",
                        )
                    )
                    continue
                manifest = ROOT / dependency.group(1).strip("\"'")
                if not manifest.is_file():
                    violations.append(
                        Violation(
                            path,
                            lineno,
                            f"`cache-dependency-path` points at missing file "
                            f"{dependency.group(1)!r}",
                            "point it at pyproject.toml (the only dependency "
                            "manifest), or a lock file that exists",
                        )
                    )
        assert_clean(violations)


class TestPythonRuntimeContract:
    """One interpreter version across workflows, Chaquopy and pyproject."""

    @staticmethod
    def pyproject_floor() -> tuple[int, int]:
        text = PYPROJECT.read_text(encoding="utf-8")
        spec = re.search(r'^requires-python\s*=\s*"([^"]+)"', text, re.MULTILINE)
        assert spec is not None, "pyproject.toml has no requires-python"
        floor = re.search(r">=\s*(\d+)\.(\d+)", spec.group(1))
        assert floor is not None, f"requires-python {spec.group(1)!r} has no floor"
        return int(floor.group(1)), int(floor.group(2))

    def test_python_version_matches_chaquopy_and_floor(self) -> None:
        gradle = (ROOT / "android" / "app" / "build.gradle.kts").read_text(
            encoding="utf-8"
        )
        chaquopy_block = gradle.find("chaquopy {")
        chaquopy = (
            re.search(r'version\s*=\s*"(\d+\.\d+)"', gradle[chaquopy_block:])
            if chaquopy_block >= 0
            else None
        )
        assert chaquopy is not None, (
            'android/app/build.gradle.kts: no Chaquopy `version = "X.Y"`; the '
            "workflows pin PYTHON_VERSION to it"
        )
        chaquopy_version = parse_version(chaquopy.group(1))
        floor = self.pyproject_floor()

        violations: list[Violation] = []
        seen: dict[str, tuple[Path, int]] = {}
        for path in workflow_files():
            found = top_level_value(path, "env")
            if found is None:
                violations.append(
                    Violation(
                        path,
                        1,
                        "no top-level `env:` block",
                        "declare PYTHON_VERSION once per workflow so the "
                        "interpreter is not the runner default",
                    )
                )
                continue
            lineno, _, block = found
            for line in block:
                match = re.match(r"^\s+PYTHON_VERSION:\s*(\S+)", line)
                if match:
                    seen[str(path)] = (path, lineno)
                    value = parse_version(match.group(1))
                    if value != chaquopy_version:
                        violations.append(
                            Violation(
                                path,
                                lineno,
                                f"PYTHON_VERSION {match.group(1)} differs from "
                                "the Chaquopy version "
                                f"{chaquopy.group(1)}",
                                "keep the workflow interpreter and "
                                "android/app/build.gradle.kts in sync",
                            )
                        )
                    if value < floor:
                        violations.append(
                            Violation(
                                path,
                                lineno,
                                f"PYTHON_VERSION {match.group(1)} is below "
                                f"requires-python >={floor[0]}.{floor[1]}",
                                "raise the pin or lower the pyproject floor",
                            )
                        )
        assert len(seen) == len(workflow_files()), (
            "every workflow must pin PYTHON_VERSION; missing in "
            + ", ".join(sorted(p.name for p in workflow_files() if str(p) not in seen))
        )
        assert_clean(violations)


class TestArtifactContract:
    """Uploaded paths must be the directories ``build_release.py`` archives."""

    @staticmethod
    def release_bundles() -> set[str]:
        text = (ROOT / "scripts" / "build_release.py").read_text(encoding="utf-8")
        bundles: set[str] = set()
        for raw in re.findall(r"\bbundle=([^,\n)]+)", text):
            value = raw.strip()
            if value.startswith(('"', "'")):
                bundles.add(value.strip("\"'"))
            elif value == "build_desktop.APP_NAME":
                desktop = (ROOT / "scripts" / "build_desktop.py").read_text(
                    encoding="utf-8"
                )
                name = re.search(r'^APP_NAME\s*=\s*"([^"]+)"', desktop, re.MULTILINE)
                if name:
                    bundles.add(name.group(1))
        assert bundles, (
            "scripts/build_release.py: no `bundle=...` declarations; the "
            "workflow upload paths cannot be derived"
        )
        return bundles

    def test_upload_paths_match_build_release_bundles(self) -> None:
        bundles = self.release_bundles()
        path = WORKFLOW_DIR / "build.yml"
        uploads: dict[str, str] = {}
        for _, _reference, body in iter_action_steps(path, "actions/upload-artifact"):
            text = "\n".join(line for _, line in body)
            name = re.search(r"^\s+name:\s*(\S+)", text, re.MULTILINE)
            target = re.search(r"^\s+path:\s*(\S+)", text, re.MULTILINE)
            if name and target:
                uploads[name.group(1)] = target.group(1)
        expected = {f"dist/{bundle}" for bundle in bundles}
        actual = {target for target in uploads.values() if target.startswith("dist/")}
        assert actual == expected, (
            f"{path.relative_to(ROOT).as_posix()}: uploads {sorted(actual)}, but "
            f"scripts/build_release.py archives {sorted(expected)}"
        )
        # The debug APK is a diagnostic artifact, not part of a release.
        apk = [target for target in uploads.values() if not target.startswith("dist/")]
        assert len(apk) == 1 and apk[0].endswith("*.apk"), (
            "the only non-release upload should be the debug APK glob"
        )


class TestSecretsAreNotPrinted:
    """Tokens reach ``release.py`` through ``env:`` and never through a shell."""

    TOKEN_REF = re.compile(r"\bsecrets\.([A-Za-z_]\w*)")
    LEAK = re.compile(
        r"\b(?:echo|printf|cat|tee|set\s+-x|set\s+-o\s+xtrace)\b[^\n]*"
        r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY)\b",
        re.IGNORECASE,
    )

    def test_only_the_workflow_token_is_referenced(self) -> None:
        violations: list[Violation] = []
        for path in workflow_files():
            for lineno, line in enumerate(read_lines(path), 1):
                for name in self.TOKEN_REF.findall(line):
                    if name == "GITHUB_TOKEN":
                        continue
                    violations.append(
                        Violation(
                            path,
                            lineno,
                            f"references `secrets.{name}`",
                            "publishing uses the built-in GITHUB_TOKEN; a new "
                            "secret needs an explicit audit (add it to "
                            "tests/test_workflow_contract.py)",
                        )
                    )
        assert_clean(violations)

    def test_run_blocks_never_echo_credentials(self) -> None:
        violations: list[Violation] = []
        for path in workflow_files():
            for lineno, line in iter_run_scripts(path):
                if line.lstrip().startswith("#"):
                    continue
                if self.LEAK.search(line):
                    violations.append(
                        Violation(
                            path,
                            lineno,
                            f"`run:` may print a credential: {line.strip()!r}",
                            "remove the echo/trace; GitHub masks the value but "
                            "the log is still public to anyone with access",
                        )
                    )
        assert_clean(violations)


class TestWindowsRunnerCompatibility:
    """The Windows jobs run under PowerShell, not bash."""

    POSIX_ONLY = (
        ("export ", "PowerShell has no `export`; use `$env:NAME = ...`"),
        ("chmod ", "PowerShell has no `chmod`; checkout already preserves mode"),
        ("sudo ", "the Windows runner has no sudo"),
        ("apt-get ", "no apt-get on Windows"),
        ("sh -c", "`sh` is not guaranteed on the Windows runner"),
        ("mkdir -p", "PowerShell has no `-p`; use New-Item -ItemType Directory"),
        ("rm -rf", "PowerShell's `rm` rejects `-rf`; use Remove-Item -Recurse"),
    )

    def test_windows_jobs_do_not_use_posix_shell_syntax(self) -> None:
        violations: list[Violation] = []
        checked = 0
        for path in workflow_files():
            scripts = iter_run_scripts(path)
            for job in iter_jobs(path):
                if not job.runs_on().startswith("windows"):
                    continue
                checked += 1
                for lineno, line in scripts:
                    if not (job.start <= lineno <= job.end):
                        continue
                    command = line.lstrip()
                    if command.startswith("#"):
                        continue
                    for token, hint in self.POSIX_ONLY:
                        if token in command:
                            violations.append(
                                Violation(
                                    path,
                                    lineno,
                                    f"Windows job {job.name!r} runs POSIX-only "
                                    f"syntax {token.strip()!r}",
                                    hint,
                                )
                            )
        assert checked, "no Windows job found; the runner split moved"
        assert_clean(violations)


class TestConcurrencyPolicy:
    """Publishing must never be cancelled or started twice concurrently."""

    @staticmethod
    def concurrency(path: Path) -> tuple[int, dict[str, str]]:
        found = top_level_value(path, "concurrency")
        assert found is not None, (
            f"{path.relative_to(ROOT).as_posix()}: no top-level `concurrency:`; "
            "fix: serialise or cancel superseded runs explicitly"
        )
        lineno, inline, block = found
        if inline:
            return lineno, {"group": inline}
        entries: dict[str, str] = {}
        for line in block:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # The group may contain a `${{ ... }}` expression with spaces.
            match = re.match(r"^([\w-]+):\s*(.+?)\s*$", stripped)
            if match:
                entries[match.group(1)] = match.group(2)
        return lineno, entries

    def test_every_workflow_declares_cancel_in_progress(self) -> None:
        violations: list[Violation] = []
        for path in workflow_files():
            lineno, entries = self.concurrency(path)
            value = entries.get("cancel-in-progress")
            if value not in {"true", "false"}:
                violations.append(
                    Violation(
                        path,
                        lineno,
                        "`concurrency` does not set an explicit boolean "
                        f"`cancel-in-progress` (found {value!r})",
                        "set `cancel-in-progress: true` for test/lint jobs and "
                        "`false` for build/publish jobs",
                    )
                )
            if not entries.get("group"):
                violations.append(
                    Violation(
                        path,
                        lineno,
                        "`concurrency` has no `group`",
                        "name the group; without it the block has no effect",
                    )
                )
        assert_clean(violations)

    def test_only_ci_cancels_superseded_runs(self) -> None:
        violations: list[Violation] = []
        expected = {
            "ci.yml": "true",
            "build.yml": "false",
            "release.yml": "false",
        }
        for name, want in expected.items():
            path = WORKFLOW_DIR / name
            if not path.is_file():
                continue
            lineno, entries = self.concurrency(path)
            if entries.get("cancel-in-progress") != want:
                violations.append(
                    Violation(
                        path,
                        lineno,
                        f"cancel-in-progress is {entries.get('cancel-in-progress')!r}, "
                        f"expected {want!r}",
                        "cancelling a publish/build mid-flight leaves orphaned "
                        "assets; only PR/push CI may cancel",
                    )
                )
        assert_clean(violations)

    def test_release_concurrency_group_is_not_ref_scoped(self) -> None:
        # ``workflow_dispatch`` from two branches yields two different
        # ``github.ref`` values; a ref-scoped group would then allow two
        # publishing runs to race on the same release assets.
        path = WORKFLOW_DIR / "release.yml"
        if not path.is_file():
            return
        lineno, entries = self.concurrency(path)
        group = entries.get("group", "")
        if "github." in group:
            assert_clean(
                [
                    Violation(
                        path,
                        lineno,
                        f"the release concurrency group is ref-scoped: {group!r}",
                        "use a constant group (for example `release`) so every "
                        "publish serialises regardless of the dispatched ref",
                    )
                ]
            )


class TestScheduledSecurityScan:
    """``security.yml`` is read-only and reports findings without leaking them."""

    @staticmethod
    def path() -> Path:
        path = WORKFLOW_DIR / "security.yml"
        assert path.is_file(), f"missing {path.relative_to(ROOT).as_posix()}"
        return path

    def test_triggers_are_weekly_schedule_and_manual_dispatch(self) -> None:
        path = self.path()
        found = top_level_value(path, "on")
        assert found is not None, (
            f"{path.relative_to(ROOT).as_posix()}: no top-level `on:` block; "
            "fix: declare `schedule` and `workflow_dispatch`"
        )
        lineno, inline, block = found
        trigger = "\n".join([inline, *block])
        violations: list[Violation] = []
        for forbidden in ("push", "pull_request", "pull_request_target"):
            if re.search(rf"^\s*{forbidden}\s*:", trigger, re.MULTILINE):
                violations.append(
                    Violation(
                        path,
                        lineno,
                        f"the security scan is triggered by `{forbidden}`",
                        "keep it on `schedule` + `workflow_dispatch` only; a "
                        "weekly audit does not need to run on every push",
                    )
                )
        if not re.search(r"^\s*workflow_dispatch\s*:", trigger, re.MULTILINE):
            violations.append(
                Violation(
                    path,
                    lineno,
                    "no `workflow_dispatch` trigger: the scan cannot be run on demand",
                    "add `workflow_dispatch:` next to the `schedule:` block",
                )
            )
        crons = [
            next(value for value in match if value)
            for match in re.findall(
                r"^\s*-\s*cron:\s*(?:\"([^\"]*)\"|'([^']*)'|(\S+))",
                trigger,
                re.MULTILINE,
            )
        ]
        if len(crons) != 1:
            violations.append(
                Violation(
                    path,
                    lineno,
                    f"expected exactly one `cron:` entry, found {crons or 'none'}",
                    "declare one weekly cron expression, for example "
                    '`- cron: "17 3 * * 1"`',
                )
            )
        else:
            fields = crons[0].split()
            if len(fields) != 5:
                violations.append(
                    Violation(
                        path,
                        lineno,
                        f"cron {crons[0]!r} is not a 5-field expression",
                        "use `minute hour day-of-month month day-of-week`",
                    )
                )
            else:
                _, _, day_of_month, month, day_of_week = fields
                if day_of_month != "*" or month != "*" or day_of_week in {"*", "?"}:
                    violations.append(
                        Violation(
                            path,
                            lineno,
                            f"cron {crons[0]!r} does not pin a weekday, so it is "
                            "not a weekly schedule",
                            'pin the day of week, for example "17 3 * * 1" (Mondays)',
                        )
                    )
        assert_clean(violations)

    def test_declares_read_only_contents_permission(self) -> None:
        path = self.path()
        lineno, entries, inline = top_level_permissions(path)
        violations: list[Violation] = []
        if inline or entries.get("contents") != "read":
            violations.append(
                Violation(
                    path,
                    lineno,
                    "the scan must declare exactly `contents: read`; found "
                    f"{inline or entries or 'no scope'}",
                    "replace the top-level `permissions:` with `contents: read`",
                )
            )
        for scope, access in sorted(entries.items()):
            if access == "write":
                violations.append(
                    Violation(
                        path,
                        lineno,
                        f"the scan requests `{scope}: write`",
                        "a read-only audit needs no write scope; remove it",
                    )
                )
        assert_clean(violations)

    def test_job_is_bounded_to_ten_minutes(self) -> None:
        path = self.path()
        job = find_job(path, "scan")
        match = re.search(
            r"^\s{4}timeout-minutes:\s*(\d+)\s*$",
            "\n".join(job.body),
            re.MULTILINE,
        )
        assert match is not None, (
            f"{path.relative_to(ROOT).as_posix()}: job `scan` has no "
            "`timeout-minutes`; a stuck history walk would burn runner time"
        )
        minutes = int(match.group(1))
        assert 0 < minutes <= 10, (
            f"{path.relative_to(ROOT).as_posix()}: job `scan` allows "
            f"{minutes} minutes; the security scan must stay at or below 10"
        )

    def test_history_checkout_is_complete(self) -> None:
        path = self.path()
        checkouts = iter_checkout_steps(path)
        assert checkouts, (
            f"{path.relative_to(ROOT).as_posix()}: no `actions/checkout` step; "
            "the scan cannot run without the repository"
        )
        missing: list[str] = []
        for lineno, reference, body in checkouts:
            text = "\n".join(line for _, line in body)
            if not re.search(r"^\s+fetch-depth:\s*0\s*$", text, re.MULTILINE):
                missing.append(f"{path.name}:{lineno} ({reference})")
        assert not missing, (
            f"{path.relative_to(ROOT).as_posix()}: checkout is shallow in "
            f"{', '.join(missing)}; `--history` would silently see no commits\n"
            "    fix: set `fetch-depth: 0` under the step's `with:`"
        )

    def test_runs_both_scans_and_keeps_json_artifacts(self) -> None:
        path = self.path()
        commands = [line for _, line in iter_run_scripts(path)]
        workspace = [
            line
            for line in commands
            if "scripts/scan_secrets.py" in line and "--history" not in line
        ]
        history = [
            line
            for line in commands
            if "scripts/scan_secrets.py" in line and "--history" in line
        ]
        assert workspace, (
            f"{path.relative_to(ROOT).as_posix()}: no working-tree scan; "
            "fix: run `python scripts/scan_secrets.py --json`"
        )
        assert history, (
            f"{path.relative_to(ROOT).as_posix()}: no history scan; "
            "fix: run `python scripts/scan_secrets.py --history --json`"
        )
        redirected = [
            line
            for line in workspace + history
            if re.search(r"--json\s*>\s*\S+\.json", line)
        ]
        assert len(redirected) == len(workspace) + len(history), (
            f"{path.relative_to(ROOT).as_posix()}: every `--json` report must "
            "be redirected to a file; a report on stdout ends up in the log"
        )

        uploads = iter_action_steps(path, "actions/upload-artifact")
        assert uploads, (
            f"{path.relative_to(ROOT).as_posix()}: the JSON reports are never "
            "uploaded; fix: add `actions/upload-artifact`"
        )
        body = "\n".join(line for _, _, lines in uploads for _, line in lines)
        for name in ("security-workspace.json", "security-history.json"):
            assert name in body, (
                f"{path.relative_to(ROOT).as_posix()}: artifact does not include {name}"
            )
        assert re.search(r"^\s+retention-days:\s*30\s*$", body, re.MULTILINE), (
            f"{path.relative_to(ROOT).as_posix()}: reports must be retained for 30 days"
        )
        assert re.search(r"^\s+if-no-files-found:\s*error\s*$", body, re.MULTILINE), (
            f"{path.relative_to(ROOT).as_posix()}: a missing report must fail "
            "the upload, not pass silently"
        )
        job = find_job(path, "scan")
        assert "if: always()" in "\n".join(job.body), (
            f"{path.relative_to(ROOT).as_posix()}: the upload must run with "
            "`if: always()`; otherwise a failing scan skips its own report"
        )

    def test_findings_fail_the_job(self) -> None:
        path = self.path()
        job = find_job(path, "scan")
        job_text = "\n".join(job.body)
        assert "continue-on-error" not in job_text, (
            f"{path.relative_to(ROOT).as_posix()}: a scan step sets "
            "`continue-on-error`, so findings would pass as a green run"
        )
        commands = "\n".join(line for _, line in iter_run_scripts(path))
        assert commands.count("|| status=1") >= 2, (
            f"{path.relative_to(ROOT).as_posix()}: both scans must record a "
            "non-zero exit instead of discarding it (`|| status=1`)"
        )
        assert 'exit "$status"' in commands, (
            f"{path.relative_to(ROOT).as_posix()}: the combined run must exit "
            "with the recorded status, so findings block the job"
        )


class TestTranslationsFreshness:
    """A newly generated catalog must fail the job, not slip through."""

    def test_staleness_check_covers_untracked_catalogs(self) -> None:
        path = WORKFLOW_DIR / "ci.yml"
        job = find_job(path, "translations")
        commands = "\n".join(
            text
            for lineno, text in iter_run_scripts(path)
            if job.start <= lineno <= job.end
        )
        assert "scripts/update_pot.py" in commands
        assert "scripts/update_po.py" in commands
        missing: list[str] = []
        if "git diff --exit-code -- src/novel_downloader/locales" not in commands:
            missing.append("git diff --exit-code (modified catalogs)")
        if "git status --porcelain -- src/novel_downloader/locales" not in commands:
            missing.append(
                "git status --porcelain (untracked catalogs: `git diff` ignores them)"
            )
        assert not missing, (
            f"{path.relative_to(ROOT).as_posix()}: the translations job no "
            f"longer checks {', '.join(missing)}"
        )


class TestPosixWrapper:
    """The Linux/macOS wrapper is exercised by CI and never locally."""

    def test_gradlew_does_not_embed_quotes_in_jvm_options(self) -> None:
        # ``DEFAULT_JVM_OPTS`` is expanded unquoted, so ``'"-Xmx64m"'`` reaches
        # java as a literal argument and the launcher looks for a main class
        # named "-Xmx64m" (exit 1 on Linux; the .bat wrapper is unaffected).
        script = (ROOT / "android" / "gradlew").read_text(encoding="utf-8")

        assert "'\"-" not in script
        assert 'DEFAULT_JVM_OPTS="-Xmx64m -Xms64m"' in script
