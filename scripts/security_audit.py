#!/usr/bin/env python3
"""
Offline dependency and configuration security audit.

The audit never touches the network. It performs four groups of checks:

``dependencies``
    Compares the requirements declared in ``pyproject.toml`` with the
    distributions installed in the current interpreter (``importlib.metadata``)
    and reports missing packages and unsatisfied version specifiers.

``configuration``
    Flags risky settings in ``settings.toml`` / ``settings.json`` such as
    ``verify_ssl = false`` and plaintext credentials, plus source-level
    ``verify=False`` TLS bypasses.

``api_docs``
    Finds documentation examples that bind the REST API to ``0.0.0.0`` without
    mentioning a token.

``credentials``
    Scans tracked text files for credential-shaped literals (cookie strings,
    API keys, tokens, private keys, ...) and looks for plaintext cookie/token
    files on disk. Only file names, line numbers and redacted evidence are
    reported -- matched values are never printed.

Usage examples::

    # Human-readable report
    python scripts/security_audit.py

    # Machine-readable report (pure JSON on stdout)
    python scripts/security_audit.py --json

    # Audit a specific config file as well
    python scripts/security_audit.py --config path/to/settings.toml

Exit codes
----------
``0``
    The audit completed and found no high-priority findings.
``1``
    The audit completed and found at least one high-priority finding.
``2``
    Usage error or the audit itself could not run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

try:  # ``packaging`` ships with pip/setuptools but guard anyway.
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.utils import canonicalize_name
except ModuleNotFoundError:  # pragma: no cover - minimal environments
    InvalidRequirement = ValueError  # type: ignore[assignment,misc]
    Requirement = None  # type: ignore[assignment]
    canonicalize_name = None  # type: ignore[assignment]

DEFAULT_ROOT = Path(__file__).resolve().parents[1]

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

#: Directories that never contain first-party source worth scanning.
SCAN_EXCLUDED_DIRS = {
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "site",
}

#: Directories skipped by the credential scan unless ``--scan-tests`` is used.
CREDENTIAL_SKIP_DIRS = {"tests"}

#: Extensions treated as text for the credential scan.
TEXT_SUFFIXES = {
    ".bat",
    ".cfg",
    ".css",
    ".env",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".txt",
    ".yaml",
    ".yml",
}

#: Config file names read from the audit root when ``--config`` is not given.
CONFIG_FILENAMES = ("settings.toml", "settings.json")

#: File names that should never be tracked by Git.
SENSITIVE_FILENAMES = {
    ".env",
    "platform_creds.json",
    "session.json",
    "settings.json",
    "settings.toml",
}

#: Config keys whose string values are credentials when non-empty.
CREDENTIAL_CONFIG_KEYS = {
    "api_key",
    "api_token",
    "client_secret",
    "cookie",
    "cookies",
    "password",
    "secret",
    "token",
}

#: Folder names that usually sync to a third-party cloud.
CLOUD_SYNC_MARKERS = (
    "box sync",
    "dropbox",
    "google drive",
    "googledrive",
    "icloud drive",
    "megasync",
    "nextcloud",
    "onedrive",
    "syncthing",
    "yandexdisk",
)

#: Matches obvious documentation placeholders so they do not become findings.
PLACEHOLDER_RE = re.compile(
    r"(?i)(?:"
    r"your|"
    r"change[-_ ]?me|"
    r"example|"
    r"placeholder|"
    r"dummy|"
    r"fake|"
    r"sample|"
    r"redacted|"
    r"xxx+|"
    r"test(?:ing)?[-_]?|"
    r"my[-_]?(?:secret|token)|"
    r"secret[-_]?token|"
    r"complete cookie|"
    r"完整的|"
    r"密钥|"
    r"yourpassword|"
    r"^<.*>$|"
    r"^\$\{.*\}$|"
    r"^\{.*\}$|"
    r"^\{\{.*\}\}$|"
    r"^%[sd]$"
    r")"
)

#: Code-level TLS verification bypasses (equivalent to ``verify_ssl = false``).
TLS_DISABLE_RE = re.compile(
    r"\bverify(?:_ssl)?\s*=\s*False\b"
    r"|\bcert_reqs\s*=\s*ssl\.CERT_NONE\b"
    r"|\bverify_mode\s*=\s*ssl\.CERT_NONE\b"
)


@dataclass
class Finding:
    """One audit finding. Values are never stored, only redacted evidence."""

    severity: str
    category: str
    check: str
    message: str
    location: str = ""
    line: int | None = None
    evidence: str = ""
    remediation: str = ""


@dataclass
class DependencyStatus:
    """Installation status of one declared requirement."""

    requirement: str
    distribution: str
    groups: list[str] = field(default_factory=list)
    installed: str | None = None
    status: str = "ok"
    detail: str = ""


def _canonical(name: str) -> str:
    """Return a PEP 503 normalized distribution name."""
    if canonicalize_name is not None:
        return canonicalize_name(name)
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_requirement(spec: str) -> Any:
    """Parse a PEP 508 requirement, returning ``None`` on failure."""
    if Requirement is not None:
        try:
            return Requirement(spec)
        except InvalidRequirement:
            return None
    match = re.match(r"^\s*([A-Za-z0-9_.-]+)", spec)
    if match is None:
        return None
    return match.group(1)


def _is_placeholder(value: str) -> bool:
    """Whether *value* looks like documentation text rather than a secret."""
    stripped = value.strip()
    if len(stripped) < 4:
        return True
    if PLACEHOLDER_RE.search(stripped):
        return True
    # Values that are only one repeated character carry no secret material.
    return len(set(stripped)) == 1


def _redact(key: str, value: str) -> str:
    """Describe a match without revealing the matched value."""
    return f"{key} = <redacted:{len(value)} chars>"


def _relative_to(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return str(path)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _cloud_marker(path: Path) -> str:
    """Return the name of the cloud-sync folder containing *path*, if any."""
    lowered = str(path.resolve()).lower()
    for marker in CLOUD_SYNC_MARKERS:
        if marker in lowered:
            return marker
    return ""


def _permission_problem(path: Path) -> str:
    """Describe unsafe POSIX permissions on *path* (empty when fine)."""
    if os.name == "nt":
        return ""
    try:
        mode = path.stat().st_mode
    except OSError:
        return ""
    problems: list[str] = []
    if path.is_dir():
        if mode & stat.S_IWOTH:
            problems.append("directory is world-writable")
        if mode & stat.S_IWGRP:
            problems.append("directory is group-writable")
    else:
        if mode & stat.S_IROTH:
            problems.append("file is world-readable")
        if mode & stat.S_IRGRP:
            problems.append("file is group-readable")
    return ", ".join(problems)


class Auditor:
    """Run all offline checks against a repository root."""

    def __init__(
        self,
        root: Path,
        *,
        config_paths: list[Path] | None = None,
        scan_tests: bool = False,
    ) -> None:
        self.root = root.resolve()
        self.config_paths = [p.resolve() for p in (config_paths or [])]
        self.scan_tests = scan_tests
        self.findings: list[Finding] = []
        self.dependencies: list[DependencyStatus] = []
        self.notes: list[str] = []
        self.checks: list[str] = []
        self._seen_findings: set[tuple[str, str, str, int | None, str]] = set()
        self._primary_config: dict[str, Any] | None = None
        self._git_files: list[Path] | None = None

    # ------------------------------------------------------------------ utils

    def add(self, finding: Finding) -> None:
        """Add a finding once."""
        key = (
            finding.severity,
            finding.check,
            finding.location,
            finding.line,
            finding.message,
        )
        if key in self._seen_findings:
            return
        self._seen_findings.add(key)
        self.findings.append(finding)

    def _read_text(self, path: Path) -> str | None:
        try:
            if path.stat().st_size > 2_000_000:
                return None
            data = path.read_bytes()
        except OSError:
            return None
        if b"\x00" in data[:4096]:
            return None
        return data.decode("utf-8", errors="replace")

    def _git(self, *args: str) -> subprocess.CompletedProcess[bytes] | None:
        try:
            return subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def _path_parts(self, path: Path) -> set[str]:
        """Return lowercase path components relative to the audit root."""
        try:
            relative = path.resolve().relative_to(self.root)
        except (OSError, ValueError):
            relative = path
        return {part.lower() for part in relative.parts}

    def _tracked_files(self) -> list[Path]:
        if self._git_files is not None:
            return self._git_files
        result = self._git("ls-files", "-z")
        if result is not None and result.returncode == 0:
            names = [part for part in result.stdout.split(b"\x00") if part]
            self._git_files = [
                self.root / name.decode("utf-8", errors="replace") for name in names
            ]
        else:
            self._git_files = [
                path
                for path in self.root.rglob("*")
                if path.is_file()
                and not any(
                    part in SCAN_EXCLUDED_DIRS for part in self._path_parts(path)
                )
            ]
        return self._git_files

    def _is_git_ignored(self, path: Path) -> bool:
        result = self._git("check-ignore", "-q", str(path))
        return result is not None and result.returncode == 0

    # ----------------------------------------------------------- dependencies

    def check_dependencies(self) -> None:
        """Compare pyproject requirements with installed distributions."""
        self.checks.append("dependencies")
        pyproject = self.root / "pyproject.toml"
        if not pyproject.is_file():
            self.add(
                Finding(
                    severity="medium",
                    category="dependencies",
                    check="pyproject-missing",
                    message="pyproject.toml 不存在，无法核对依赖声明。",
                    location=str(pyproject),
                )
            )
            return
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            self.add(
                Finding(
                    severity="medium",
                    category="dependencies",
                    check="pyproject-parse",
                    message=f"pyproject.toml 解析失败: {exc}",
                    location=str(pyproject),
                )
            )
            return

        project = data.get("project", {})
        requirements: list[tuple[str, str]] = [
            ("core", spec) for spec in project.get("dependencies", [])
        ]
        for group, specs in project.get("optional-dependencies", {}).items():
            requirements.extend((f"optional:{group}", spec) for spec in specs)

        merged: dict[tuple[str, str], DependencyStatus] = {}
        for group, spec in requirements:
            parsed = _parse_requirement(spec)
            if parsed is None:
                self.add(
                    Finding(
                        severity="low",
                        category="dependencies",
                        check="requirement-parse",
                        message=f"无法解析依赖声明: {spec!r}",
                        location="pyproject.toml",
                    )
                )
                continue
            name = getattr(parsed, "name", parsed)
            distribution = _canonical(name)
            extras = tuple(sorted(getattr(parsed, "extras", ())))
            specifier = getattr(parsed, "specifier", None)
            key = (distribution, extras)
            status = merged.get(key)
            if status is None:
                status = DependencyStatus(
                    requirement=spec,
                    distribution=distribution,
                    groups=[],
                )
                merged[key] = status
            if group not in status.groups:
                status.groups.append(group)

            try:
                installed = metadata.version(distribution)
            except metadata.PackageNotFoundError:
                installed = None

            if installed is None:
                status.status = "missing"
                status.detail = "not installed"
                continue
            status.installed = installed
            if specifier is not None and specifier:
                try:
                    satisfies = specifier.contains(installed, prereleases=True)
                except Exception:  # pragma: no cover - invalid installed version
                    satisfies = False
                if not satisfies:
                    status.status = "version-mismatch"
                    status.detail = (
                        f"installed {installed} does not satisfy {specifier}"
                    )

        self.dependencies = sorted(
            merged.values(), key=lambda item: (item.status == "ok", item.distribution)
        )
        for status in self.dependencies:
            if status.status == "ok":
                continue
            core = "core" in status.groups
            if status.status == "missing":
                severity = "high" if core else "low"
                detail = "未安装"
            else:
                severity = "high" if core else "medium"
                detail = status.detail
            self.add(
                Finding(
                    severity=severity,
                    category="dependencies",
                    check=f"dependency-{status.status}",
                    message=(
                        f"依赖 {status.distribution} 状态异常 ({detail}); "
                        f"声明组: {', '.join(status.groups)}"
                    ),
                    location="pyproject.toml",
                    evidence=f"requirement={status.requirement!r}",
                    remediation="运行 pip install -e '.[all]' 或安装对应可选组。",
                )
            )

    # ---------------------------------------------------------- configuration

    def _config_files(self) -> list[Path]:
        files = [p for p in self.config_paths if p.is_file()]
        for name in CONFIG_FILENAMES:
            candidate = self.root / name
            if candidate.is_file() and candidate not in files:
                files.append(candidate)
        return files

    def _load_config(self, path: Path) -> dict[str, Any] | None:
        try:
            if path.suffix.lower() == ".json":
                loaded = json.loads(path.read_text(encoding="utf-8"))
            else:
                loaded = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
            self.add(
                Finding(
                    severity="medium",
                    category="configuration",
                    check="config-parse",
                    message=f"配置文件解析失败: {exc}",
                    location=_relative_to(path, self.root),
                )
            )
            return None
        return loaded if isinstance(loaded, dict) else None

    @staticmethod
    def _walk_config(
        mapping: dict[str, Any], prefix: str = ""
    ) -> list[tuple[str, str, Any]]:
        items: list[tuple[str, str, Any]] = []
        for key, value in mapping.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                items.extend(Auditor._walk_config(value, dotted))
            else:
                items.append((dotted, str(key), value))
        return items

    def check_configuration(self) -> None:
        """Flag dangerous values in settings files."""
        self.checks.append("configuration")
        configs = self._config_files()
        if not configs:
            self.notes.append("未找到 settings.toml / settings.json，跳过配置检查。")

        for path in configs:
            rel = _relative_to(path, self.root)
            permission = _permission_problem(path)
            if permission:
                self.add(
                    Finding(
                        severity="medium",
                        category="configuration",
                        check="config-permissions",
                        message=f"配置文件权限过宽: {permission}",
                        location=rel,
                        remediation="执行 chmod 600 settings.toml。",
                    )
                )
            loaded = self._load_config(path)
            if loaded is None:
                continue
            if self._primary_config is None:
                self._primary_config = loaded

            for dotted, key, value in self._walk_config(loaded):
                lowered = key.lower()
                if lowered == "verify_ssl" and value is False:
                    self.add(
                        Finding(
                            severity="high",
                            category="configuration",
                            check="verify-ssl-disabled",
                            message=(
                                f"{dotted} = false 会关闭 TLS 证书校验，"
                                "易受中间人攻击。"
                            ),
                            location=rel,
                            remediation=(
                                "恢复 verify_ssl = true；仅在可控代理排障时临时关闭。"
                            ),
                        )
                    )
                if lowered in CREDENTIAL_CONFIG_KEYS and isinstance(value, str):
                    stripped = value.strip()
                    if not stripped:
                        continue
                    if _is_placeholder(stripped):
                        self.add(
                            Finding(
                                severity="info",
                                category="configuration",
                                check="config-placeholder-credential",
                                message=(
                                    f"{dotted} 看起来是占位符，请确认未提交真实凭据。"
                                ),
                                location=rel,
                                evidence=f"{dotted} = <placeholder>",
                            )
                        )
                    else:
                        self.add(
                            Finding(
                                severity="high",
                                category="configuration",
                                check="config-plaintext-credential",
                                message=(
                                    f"{dotted} 在配置文件中以明文保存凭据；"
                                    "文件若被同步或分享会直接泄露。"
                                ),
                                location=rel,
                                evidence=_redact(dotted, stripped),
                                remediation=(
                                    "改用环境变量或登录命令写入的 cookie 文件，"
                                    "并确保配置文件权限为 600。"
                                ),
                            )
                        )

    # --------------------------------------------------------------- API docs

    def check_code_tls(self) -> None:
        """Flag code that disables TLS certificate verification."""
        self.checks.append("code_tls")
        for path in self._tracked_files():
            if path.suffix.lower() != ".py" or not self._should_scan(path):
                continue
            text = self._read_text(path)
            if text is None:
                continue
            lines = [
                index
                for index, line in enumerate(text.splitlines(), start=1)
                if TLS_DISABLE_RE.search(line)
            ]
            if not lines:
                continue
            parts = self._path_parts(path)
            severity = "medium" if "_vendor" in parts else "high"
            self.add(
                Finding(
                    severity=severity,
                    category="configuration",
                    check="tls-verify-disabled-in-code",
                    message=(
                        f"{_relative_to(path, self.root)} 中硬编码禁用 TLS "
                        f"证书校验 ({len(lines)} 处)"
                    ),
                    location=_relative_to(path, self.root),
                    line=lines[0],
                    evidence="verify=False at lines "
                    + ", ".join(str(number) for number in lines),
                    remediation=(
                        "删除 verify=False，改用系统 CA；若站点证书确实异常，"
                        "应在配置层显式说明并记录风险。"
                    ),
                )
            )

    def check_api_docs(self) -> None:
        """Find 0.0.0.0 examples that do not mention a token."""
        self.checks.append("api_docs")
        roots = [
            self.root / "docs",
            self.root / "android",
            self.root / "README.md",
            self.root / "README.en.md",
        ]
        files: list[Path] = []
        for item in roots:
            if item.is_file():
                files.append(item)
            elif item.is_dir():
                files.extend(item.rglob("*.md"))

        checked = 0
        with_token = 0
        for path in files:
            text = self._read_text(path)
            if text is None:
                continue
            lines = text.splitlines()
            for index, line in enumerate(lines):
                if "--host" not in line or "0.0.0.0" not in line:
                    continue
                checked += 1
                context = "\n".join(lines[max(0, index - 2) : index + 3]).lower()
                has_token = (
                    "--token" in line.lower()
                    or "novel_api_token" in context
                    or "token" in context
                )
                if has_token:
                    with_token += 1
                    continue
                self.add(
                    Finding(
                        severity="medium",
                        category="api_docs",
                        check="insecure-bind-example",
                        message=(
                            "文档示例绑定 0.0.0.0 但未提示设置 token；"
                            "同网段任何人都能访问下载目录。"
                        ),
                        location=_relative_to(path, self.root),
                        line=index + 1,
                        evidence="--host 0.0.0.0 (no token in surrounding lines)",
                        remediation="在示例中加入 --token 或 NOVEL_API_TOKEN。",
                    )
                )
                if "--cors" in context:
                    self.add(
                        Finding(
                            severity="medium",
                            category="api_docs",
                            check="cors-with-public-bind",
                            message="文档示例在 0.0.0.0 上同时启用 --cors。",
                            location=_relative_to(path, self.root),
                            line=index + 1,
                            evidence="--host 0.0.0.0 + --cors",
                        )
                    )
        self.notes.append(
            f"API 文档检查: {checked} 个 0.0.0.0 示例，"
            f"{with_token} 个在上下文中提到 token。"
        )
        api_dir = self.root / "src" / "novel_downloader" / "apps" / "api"
        if api_dir.is_dir():
            for path in sorted(api_dir.glob("*.py")):
                text = self._read_text(path)
                if text and "StaticFiles" in text and '"/downloads"' in text:
                    self.notes.append(
                        "API 的 /downloads 由 StaticFiles 挂载，token 仅保护 "
                        "/api/v1/*；绑定 0.0.0.0 时导出文件仍可被直接访问。"
                    )
                    break

    # -------------------------------------------------------------- storage

    def _cache_dir(self) -> Path:
        override = os.environ.get("NOVEL_CACHE_DIR", "").strip()
        if override:
            return Path(override).expanduser().resolve()
        general = {}
        if isinstance(self._primary_config, dict):
            value = self._primary_config.get("general")
            if isinstance(value, dict):
                general = value
        raw = str(general.get("cache_dir") or "./novel_cache")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.root / path
        return path.resolve()

    def _music_data_dir(self) -> Path:
        override = os.environ.get("NOVEL_MUSIC_HOME", "").strip()
        if override:
            return Path(override).expanduser().resolve()
        try:
            from platformdirs import user_data_dir
        except ModuleNotFoundError:  # pragma: no cover - optional runtime dep
            return self.root / "music"
        base = Path(user_data_dir("novel_downloader", appauthor=False))
        return (base / "music").resolve()

    def _record_plaintext_file(self, path: Path, label: str) -> None:
        if not path.is_file():
            return
        reasons: list[str] = []
        severity = "low"
        cloud = _cloud_marker(path)
        if cloud:
            reasons.append(f"位于云同步目录 ({cloud})")
            severity = "high"
        permission = _permission_problem(path)
        if permission:
            reasons.append(permission)
            severity = "high"
        parent_permission = _permission_problem(path.parent)
        if parent_permission:
            reasons.append(f"父目录 {parent_permission}")
            severity = "high"
        if _is_under(path, self.root):
            reasons.append(
                "位于项目工作目录内（已被 .gitignore 忽略，"
                "但打包/备份/压缩时仍可能泄露）"
            )
            severity = "medium" if severity == "low" else severity

        if not reasons:
            reasons.append("明文保存，未检测到额外权限问题")
        self.add(
            Finding(
                severity=severity,
                category="storage",
                check="plaintext-credential-file",
                message=f"发现明文{label}文件: {_relative_to(path, self.root)}",
                location=_relative_to(path, self.root),
                evidence="; ".join(reasons),
                remediation=(
                    "确认该目录不参与云同步/共享，限制为当前用户可读；"
                    "不再需要时删除对应 cookie 文件。"
                ),
            )
        )

    def check_plaintext_storage(self) -> None:
        """Locate cookie/token stores and assess how exposed they are."""
        self.checks.append("storage")
        cache_dir = self._cache_dir()
        if cache_dir.exists():
            for path in sorted(cache_dir.rglob("*.cookies")):
                self._record_plaintext_file(path, " Cookie")
            for name in ("session.json", "platform_creds.json"):
                self._record_plaintext_file(cache_dir / name, "凭据")

        for name in ("platform_creds.json", "session.json"):
            self._record_plaintext_file(self.root / name, "凭据")
            self._record_plaintext_file(self._music_data_dir() / name, "音乐平台凭据")

        if os.name == "nt":
            self.notes.append(
                "Windows 下未校验 ACL：请确认凭据目录仅当前用户可读 (icacls 检查)。"
            )

    # ---------------------------------------------------------- credentials

    @staticmethod
    def _credential_patterns() -> list[tuple[str, re.Pattern[str]]]:
        return [
            (
                "MUSIC_U cookie",
                re.compile(r"\bMUSIC_U\s*=\s*(?P<value>[^\s\"';&<]{8,})"),
            ),
            (
                "cookie literal",
                re.compile(
                    r"(?i)\bcookies?\s*=\s*(?P<q>[\"'])"
                    r"(?P<value>[^\"'\r\n]{12,})(?P=q)"
                ),
            ),
            (
                "Cookie header",
                re.compile(
                    r"(?i)[\"']?cookie[\"']?\s*:\s*(?P<q>[\"'])"
                    r"(?P<value>[^\"'\r\n]{12,})(?P=q)"
                ),
            ),
            (
                "token literal",
                re.compile(
                    r"(?i)\b(?:access_token|api_token|auth_token|refresh_token|"
                    r"token)\s*=\s*(?P<q>[\"'])(?P<value>[^\"'\r\n]{8,})(?P=q)"
                ),
            ),
            (
                "API key literal",
                re.compile(
                    r"(?i)\b(?:api[_-]?key|x-api-key)\b[\"']?\s*[:=]\s*"
                    r"(?P<q>[\"'])(?P<value>[^\"'\r\n]{12,})(?P=q)"
                ),
            ),
            (
                "client secret literal",
                re.compile(
                    r"(?i)\b(?:app_secret|client[_-]?secret|secret[_-]?key)\b"
                    r"[\"']?\s*[:=]\s*(?P<q>[\"'])"
                    r"(?P<value>[^\"'\r\n]{12,})(?P=q)"
                ),
            ),
            (
                "password literal",
                re.compile(
                    r"(?i)\bpassword\s*=\s*(?P<q>[\"'])"
                    r"(?P<value>[^\"'\r\n]{8,})(?P=q)"
                ),
            ),
            (
                "Bearer token",
                re.compile(r"(?i)\bBearer\s+(?P<value>[A-Za-z0-9._~+/=-]{16,})"),
            ),
            (
                "AWS access key",
                re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
            ),
            (
                "GitHub token",
                re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
            ),
            (
                "Slack token",
                re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
            ),
            (
                "JWT",
                re.compile(
                    r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\."
                    r"[A-Za-z0-9_-]{10,}\b"
                ),
            ),
            (
                "private key block",
                re.compile(r"-----BEGIN (?:DSA |EC |OPENSSH |RSA )?PRIVATE KEY-----"),
            ),
        ]

    def _credential_severity(self, path: Path) -> str:
        parts = self._path_parts(path)
        name = path.name.lower()
        if "tests" in parts or name.startswith("test_") or name == "conftest.py":
            return "info"
        if "_vendor" in parts:
            return "medium"
        if path.suffix.lower() == ".md" or "docs" in parts:
            return "medium"
        if "example" in name or "sample" in name:
            return "low"
        return "high"

    def _should_scan(self, path: Path) -> bool:
        if not path.is_file():
            return False
        parts = self._path_parts(path)
        if any(part in SCAN_EXCLUDED_DIRS for part in parts):
            return False
        if not self.scan_tests and (parts & CREDENTIAL_SKIP_DIRS):
            return False
        name = path.name.lower()
        if name.startswith(".env") or name in {".npmrc", ".pypirc"}:
            return True
        return path.suffix.lower() in TEXT_SUFFIXES

    def _scan_file(
        self, path: Path, patterns: list[tuple[str, re.Pattern[str]]]
    ) -> None:
        text = self._read_text(path)
        if text is None:
            return
        severity_base = self._credential_severity(path)
        rel = _relative_to(path, self.root)
        for line_no, line in enumerate(text.splitlines(), start=1):
            for label, pattern in patterns:
                match = pattern.search(line)
                if match is None:
                    continue
                value = match.groupdict().get("value", "")
                if value and _is_placeholder(value):
                    continue
                if value:
                    evidence = _redact(label, value)
                else:
                    evidence = f"{label} (literal marker)"
                self.add(
                    Finding(
                        severity=severity_base,
                        category="credentials",
                        check="credential-literal",
                        message=f"检测到疑似硬编码凭据: {label}",
                        location=rel,
                        line=line_no,
                        evidence=evidence,
                        remediation=(
                            "将凭据移出源码（环境变量/本地凭据文件）；"
                            "如已提交，请立即轮换。"
                        ),
                    )
                )

    def _check_tracked_sensitive_files(self) -> None:
        for path in self._tracked_files():
            name = path.name.lower()
            if name in SENSITIVE_FILENAMES or name.endswith(
                (".cookies", ".pem", ".key")
            ):
                self.add(
                    Finding(
                        severity="high",
                        category="credentials",
                        check="sensitive-file-tracked",
                        message=f"敏感文件被 Git 跟踪: {_relative_to(path, self.root)}",
                        location=_relative_to(path, self.root),
                        remediation=(
                            "git rm --cached 该文件并加入 .gitignore，轮换其中凭据。"
                        ),
                    )
                )

    def _check_untracked_sensitive_files(self) -> None:
        inside_worktree = self._git("rev-parse", "--is-inside-work-tree")
        if inside_worktree is None or inside_worktree.returncode != 0:
            return
        candidates = [self.root / name for name in SENSITIVE_FILENAMES]
        candidates.extend(
            [self.root / ".env", self.root / ".npmrc", self.root / ".pypirc"]
        )
        for path in candidates:
            if not path.is_file():
                continue
            if _relative_to(path, self.root) != path.name:
                continue
            if self._is_git_ignored(path):
                continue
            result = self._git("ls-files", "--error-unmatch", path.name)
            if result is not None and result.returncode == 0:
                continue
            self.add(
                Finding(
                    severity="high",
                    category="credentials",
                    check="sensitive-file-not-ignored",
                    message=(
                        f"本地敏感文件未被 .gitignore 覆盖: "
                        f"{_relative_to(path, self.root)}"
                    ),
                    location=_relative_to(path, self.root),
                    remediation="把该文件加入 .gitignore，并检查是否已进入历史。",
                )
            )

    def check_credentials(self) -> None:
        """Scan tracked text files and local configs for credential literals."""
        self.checks.append("credentials")
        self._check_tracked_sensitive_files()
        self._check_untracked_sensitive_files()

        patterns = self._credential_patterns()
        seen: set[Path] = set()
        for path in self._tracked_files():
            if path in seen or not self._should_scan(path):
                continue
            seen.add(path)
            self._scan_file(path, patterns)

        # Local (usually gitignored) configs are still worth scanning for
        # accidentally pasted secrets, but only at the repository root.
        for name in (*CONFIG_FILENAMES, ".env"):
            path = self.root / name
            if path.is_file() and path not in seen:
                seen.add(path)
                self._scan_file(path, patterns)

    # ---------------------------------------------------------------- report

    def run(self) -> dict[str, Any]:
        """Run every check and return the report as a JSON-ready dict."""
        self.check_dependencies()
        self.check_configuration()
        self.check_code_tls()
        self.check_api_docs()
        self.check_plaintext_storage()
        self.check_credentials()

        self.findings.sort(
            key=lambda item: (
                SEVERITY_ORDER.get(item.severity, 9),
                item.category,
                item.location,
                item.line or 0,
            )
        )
        counts = {
            severity: sum(1 for item in self.findings if item.severity == severity)
            for severity in SEVERITY_ORDER
        }
        missing = sum(1 for d in self.dependencies if d.status == "missing")
        mismatched = sum(1 for d in self.dependencies if d.status == "version-mismatch")
        exit_code = 1 if counts["high"] else 0
        return {
            "root": str(self.root),
            "generated_at": datetime.now(UTC).isoformat(),
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "checks": self.checks,
            "summary": {
                **counts,
                "dependencies_checked": len(self.dependencies),
                "dependencies_missing": missing,
                "dependencies_mismatched": mismatched,
                "exit_code": exit_code,
            },
            "dependencies": [
                {
                    "requirement": item.requirement,
                    "distribution": item.distribution,
                    "groups": item.groups,
                    "installed": item.installed,
                    "status": item.status,
                    "detail": item.detail,
                }
                for item in self.dependencies
            ],
            "findings": [
                {
                    "severity": item.severity,
                    "category": item.category,
                    "check": item.check,
                    "message": item.message,
                    "location": item.location,
                    "line": item.line,
                    "evidence": item.evidence,
                    "remediation": item.remediation,
                }
                for item in self.findings
            ],
            "notes": self.notes,
        }


def _print_human(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print(f"Security audit: {report['root']}")
    print(f"Generated: {report['generated_at']}")
    print(f"Python: {report['python']} ({report['platform']})")
    print()

    dep = (
        f"{summary['dependencies_checked']} checked, "
        f"{summary['dependencies_missing']} missing, "
        f"{summary['dependencies_mismatched']} mismatched"
    )
    print(f"Dependencies: {dep}")
    print()

    findings = report["findings"]
    if not findings:
        print("Findings: none")
    else:
        print(f"Findings ({len(findings)}):")
        for item in findings:
            location = item["location"] or "-"
            if item["line"]:
                location = f"{location}:{item['line']}"
            print(f"[{item['severity'].upper()}] {item['message']}")
            print(f"  location: {location}")
            if item["evidence"]:
                print(f"  evidence: {item['evidence']}")
            if item["remediation"]:
                print(f"  fix: {item['remediation']}")
            print()

    if report["notes"]:
        print("Notes:")
        for note in report["notes"]:
            print(f"  - {note}")
        print()

    print(
        "Summary: "
        f"{summary['high']} high, {summary['medium']} medium, "
        f"{summary['low']} low, {summary['info']} info"
    )
    if summary["exit_code"]:
        print("Result: FAIL (high-priority findings)")
    else:
        print("Result: PASS (no high-priority findings)")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline security audit for novel-downloader.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help="Repository root to audit.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        action="append",
        default=None,
        help="Extra settings file to audit (repeatable).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON report to stdout.",
    )
    parser.add_argument(
        "--scan-tests",
        action="store_true",
        help="Include tests/ in the credential scan (fake fixtures usually match).",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Run the audit and exit with 0 (clean) or 1 (high findings)."""
    args = _build_parser().parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir():
        print(f"error: root is not a directory: {root}", file=sys.stderr)
        raise SystemExit(2)

    try:
        auditor = Auditor(
            root,
            config_paths=list(args.config or []),
            scan_tests=args.scan_tests,
        )
        report = auditor.run()
    except Exception as exc:  # pragma: no cover - defensive
        print(f"error: audit failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        _print_human(report)
    raise SystemExit(report["summary"]["exit_code"])


if __name__ == "__main__":
    main()
