#!/usr/bin/env python3
"""通过 GitHub Git Data API 推送本地提交（不依赖 github.com:443）。

本机/部分网络环境到 ``github.com:443`` 的 git 链路会被拦截，但
``api.github.com`` 仍可访问。这个脚本只使用 Git Data API：

1. 读取远端分支当前指向的提交，比较提交的 tree 与本地 ``main`` 的历史，
   找出最新的"已推送边界"（tree 相同即视为内容已同步）；
2. 把边界之后的提交按从旧到新的顺序 replay：上传缺失的 blob，重建完整
   tree，创建 commit（保留原始 message、author、committer 与日期）；
3. 最后用非 force 的方式更新分支 ref。远端若已有本地不包含的新提交，
   脚本直接报错退出，绝不覆盖。

凭据：优先读 ``GITHUB_TOKEN`` / ``GH_TOKEN``，否则执行
``git credential fill``（protocol=https, host=github.com）。token 只放在
HTTP 头里，绝不打印、绝不写入磁盘。

用法::

    python scripts/push_via_api.py --dry-run
    python scripts/push_via_api.py --repo owner/name --base main
    python scripts/push_via_api.py --repo owner/name --base release/1.0

默认仓库从 ``git remote get-url origin`` 解析，默认基准/目标分支为 ``main``。
只依赖标准库。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

GITHUB_HOST = "github.com"
GITHUB_API = "https://api.github.com"
TOKEN_ENV_VARS = ("GITHUB_TOKEN", "GH_TOKEN")
HTTP_TIMEOUT = 30.0
CREDENTIAL_TIMEOUT = 15.0
HTTP_ATTEMPTS = 4
RETRY_BASE_DELAY = 1.0
MAX_RETRY_DELAY = 30.0
MAX_TREE_BYTES = 7 * 1024 * 1024
MAX_TREE_ENTRIES = 100_000
USER_AGENT = "novel-downloader-push-script"

_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_SLUG_PART_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SCP_REMOTE_RE = re.compile(r"^[^@/\s]+@[^:/\s]+:(?P<path>.+)$")

_COMMIT_FORMAT = "%H%x00%T%x00%P%x00%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%B"


class PushError(RuntimeError):
    """A local check or API reply makes this push unsafe to continue."""


class TransientAPIError(PushError):
    """A retryable transport or server-side fault."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    """The author or committer fields GitHub expects."""

    name: str
    email: str
    date: str


@dataclass(frozen=True)
class LocalCommit:
    """One local commit as replayed through the API."""

    sha: str
    tree: str
    parents: tuple[str, ...]
    author: Identity
    committer: Identity
    message: str


@dataclass(frozen=True)
class TreeFile:
    """A file entry from ``git ls-tree -r``."""

    path: str
    mode: str
    sha: str


@dataclass(frozen=True)
class RemoteRef:
    """The target branch ref and the commit it points at."""

    ref: str
    sha: str


@dataclass(frozen=True)
class RemoteCommit:
    """The subset of a remote commit this script needs."""

    sha: str
    tree: str


@dataclass(frozen=True)
class Base:
    """The local commit treated as already pushed, and its remote counterpart."""

    local_sha: str
    remote_sha: str


@dataclass(frozen=True)
class PlannedCommit:
    """A local commit plus the file list needed to rebuild its tree."""

    commit: LocalCommit
    files: tuple[TreeFile, ...]


@dataclass(frozen=True)
class PushPlan:
    """Everything the replay needs, computed before any write happens."""

    branch: str
    base: Base | None
    remote_ref_sha: str | None
    remote_tree: str | None
    commits: tuple[PlannedCommit, ...]

    @property
    def file_count(self) -> int:
        """Total file entries across every commit that will be replayed."""
        return sum(len(planned.files) for planned in self.commits)


@dataclass(frozen=True)
class Reply:
    """An HTTP reply with its decoded JSON payload (``None`` when not JSON)."""

    status: int
    headers: Mapping[str, str]
    payload: Any


# ---------------------------------------------------------------------------
# Git plumbing (tests replace ``_git_text`` / ``_git_bytes``)
# ---------------------------------------------------------------------------


def _run_git_raw(
    args: Sequence[str],
    root: Path,
    *,
    text: bool,
    input_text: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[Any]:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
    }
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        input=input_text,
        capture_output=True,
        text=text,
        encoding="utf-8" if text else None,
        errors="replace" if text else None,
        timeout=timeout,
        check=False,
        env=env,
    )


def _git_text(args: Sequence[str], root: Path) -> str:
    """Run a read-only git command and return its UTF-8 stdout."""
    try:
        proc = _run_git_raw(args, root, text=True)
    except FileNotFoundError as exc:
        raise PushError("找不到 git 可执行文件") from exc
    except subprocess.TimeoutExpired as exc:
        raise PushError(f"git {' '.join(args)} 超时") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "无输出").strip().splitlines()
        raise PushError(
            f"git {' '.join(args)} 失败（退出码 {proc.returncode}）："
            + " | ".join(detail[-5:])
        )
    return str(proc.stdout)


def _git_bytes(args: Sequence[str], root: Path) -> bytes:
    """Run a read-only git command and return raw stdout bytes."""
    try:
        proc = _run_git_raw(args, root, text=False)
    except FileNotFoundError as exc:
        raise PushError("找不到 git 可执行文件") from exc
    except subprocess.TimeoutExpired as exc:
        raise PushError(f"git {' '.join(args)} 超时") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or b"no output").decode("utf-8", "replace")
        raise PushError(
            f"git {' '.join(args)} 失败（退出码 {proc.returncode}）：{detail.strip()}"
        )
    raw = proc.stdout
    if isinstance(raw, str):  # pragma: no cover - text=False always yields bytes
        return raw.encode("utf-8")
    return bytes(raw)


def local_history(root: Path) -> list[tuple[str, str]]:
    """Every first-parent commit as ``(sha, tree_sha)``, newest first."""
    raw = _git_text(["log", "--first-parent", "--format=%H %T", "HEAD"], root)
    history: list[tuple[str, str]] = []
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) != 2:
            raise PushError(f"无法解析 git log 输出：{line!r}")
        history.append((fields[0], fields[1]))
    if not history:
        raise PushError("本地仓库没有提交（HEAD 为空）")
    return history


def _parse_commit(sha: str, raw: str) -> LocalCommit:
    """Parse one ``%H%x00...%B`` record."""
    fields = raw.split("\x00", 9)
    if len(fields) != 10:
        raise PushError(f"无法解析提交 {sha[:10]} 的元数据")
    actual, tree, parents, an, ae, ad, cn, ce, cd, message = fields
    if actual.strip() != sha:
        raise PushError(f"git 返回了错误的提交：期望 {sha}，得到 {actual.strip()}")
    return LocalCommit(
        sha=sha,
        tree=tree,
        parents=tuple(part for part in parents.split() if part),
        author=Identity(name=an, email=ae, date=ad),
        committer=Identity(name=cn, email=ce, date=cd),
        # Git stores a trailing newline in the raw message; GitHub's API does
        # not need it and adding one per replay would grow the text.
        message=message.rstrip("\n"),
    )


def read_commit(root: Path, sha: str) -> LocalCommit:
    """Read one commit's message, parents, author and committer."""
    raw = _git_text(["show", "-s", f"--format={_COMMIT_FORMAT}", sha], root)
    return _parse_commit(sha, raw)


def read_commit_tree(root: Path, sha: str) -> tuple[TreeFile, ...]:
    """Every blob in *sha*'s tree, recursively (``git ls-tree -r``)."""
    raw = _git_text(["ls-tree", "-r", "-z", sha], root)
    files: list[TreeFile] = []
    for record in raw.split("\x00"):
        if not record:
            continue
        meta, tab, path = record.partition("\t")
        if not tab:
            raise PushError(f"无法解析 ls-tree 条目：{record!r}")
        parts = meta.split()
        if len(parts) != 3:
            raise PushError(f"无法解析 ls-tree 元数据：{meta!r}")
        mode, kind, blob = parts
        if kind != "blob":
            raise PushError(
                f"提交 {sha[:10]} 包含 {kind} 条目 {path!r}；"
                "Git Data API 推送暂不支持子模块"
            )
        files.append(TreeFile(path=path, mode=mode, sha=blob))
    return tuple(files)


def read_blob(root: Path, sha: str) -> bytes:
    """The raw bytes of a git blob."""
    return _git_bytes(["cat-file", "blob", sha], root)


def commits_between(root: Path, base_sha: str | None) -> tuple[LocalCommit, ...]:
    """Commits after *base_sha* (oldest first); every commit when it is ``None``."""
    revision = f"{base_sha}..HEAD" if base_sha else "HEAD"
    raw = _git_text(
        ["rev-list", "--first-parent", "--reverse", revision],
        root,
    )
    return tuple(read_commit(root, sha) for sha in raw.split())


# ---------------------------------------------------------------------------
# Remote names and credentials
# ---------------------------------------------------------------------------


def parse_remote_slug(remote_url: str) -> str:
    """Extract ``OWNER/NAME`` from an HTTPS or SSH remote URL."""
    text = remote_url.strip()
    if not text:
        raise PushError("origin 的 URL 为空")
    if "://" in text:
        path = urllib.parse.urlsplit(text).path
    else:
        match = _SCP_REMOTE_RE.match(text)
        if match is None:
            raise PushError(f"无法从 origin URL 解析仓库：{remote_url!r}")
        path = match.group("path")
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        raise PushError(
            f"无法从 origin URL 解析 OWNER/NAME：{remote_url!r}；"
            "请用 --repo owner/name 指定"
        )
    owner, name = parts[-2], parts[-1]
    _validate_slug(owner, name)
    return f"{owner}/{name}"


def _validate_slug(owner: str, name: str) -> None:
    if not _SLUG_PART_RE.fullmatch(owner) or not _SLUG_PART_RE.fullmatch(name):
        raise PushError(f"非法的仓库名：{owner}/{name}")


def normalize_slug(value: str) -> str:
    """Validate and normalize an explicit ``--repo`` value."""
    text = value.strip().strip("/")
    if text.endswith(".git"):
        text = text[: -len(".git")]
    parts = [part for part in text.split("/") if part]
    if len(parts) != 2:
        raise PushError(f"--repo 必须是 owner/name，得到 {value!r}")
    _validate_slug(parts[0], parts[1])
    return f"{parts[0]}/{parts[1]}"


def origin_slug(root: Path) -> str:
    """``OWNER/NAME`` from the ``origin`` remote."""
    try:
        url = _git_text(["remote", "get-url", "origin"], root).strip()
    except PushError as exc:
        raise PushError(f"{exc}（请用 --repo owner/name 指定仓库）") from exc
    return parse_remote_slug(url)


def github_token(root: Path) -> str | None:
    """
    Return a GitHub token without storing or printing it.

    Order: ``GITHUB_TOKEN`` / ``GH_TOKEN`` (how CI provides the token), then
    ``git credential fill`` for ``protocol=https`` / ``host=github.com``.
    """
    for variable in TOKEN_ENV_VARS:
        token = os.environ.get(variable)
        if token:
            return token
    try:
        proc = _run_git_raw(
            ["credential", "fill"],
            root,
            text=True,
            input_text=f"protocol=https\nhost={GITHUB_HOST}\n\n",
            timeout=CREDENTIAL_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired, PushError):
        return None
    if proc.returncode != 0:
        return None
    for line in str(proc.stdout).splitlines():
        if line.startswith("password="):
            secret = line[len("password=") :].strip()
            return secret or None
    return None


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


def _parse_json(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None


def _header_map(headers: Any) -> dict[str, str]:
    if headers is None:
        return {}
    try:
        items = headers.items()
    except AttributeError:  # pragma: no cover - defensive for odd doubles
        return {}
    return {str(key).lower(): str(value) for key, value in items}


def _retry_delay(headers: Mapping[str, str], attempt: int) -> float:
    """Seconds to wait before the next attempt (429 honours ``Retry-After``)."""
    retry_after = headers.get("retry-after")
    if retry_after:
        try:
            return float(min(max(float(retry_after), 0.0), MAX_RETRY_DELAY))
        except ValueError:
            pass
    return float(min(RETRY_BASE_DELAY * (2 ** (attempt - 1)), MAX_RETRY_DELAY))


class GitHubClient:
    """The Git Data API calls this script needs, with retry/backoff."""

    def __init__(
        self,
        slug: str,
        token: str,
        api_base: str = GITHUB_API,
        *,
        attempts: int = HTTP_ATTEMPTS,
    ) -> None:
        self._slug = slug.rstrip("/")
        self._token = token
        self._api = api_base.rstrip("/")
        self._attempts = max(1, attempts)

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
        timeout: float | None = None,
    ) -> Reply:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        body: bytes | None = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        for attempt in range(1, self._attempts + 1):
            status = 0
            raw = b""
            reply_headers: dict[str, str] = {}
            try:
                with urllib.request.urlopen(
                    request, timeout=HTTP_TIMEOUT if timeout is None else timeout
                ) as response:
                    status = int(response.status)
                    raw = response.read()
                    reply_headers = _header_map(getattr(response, "headers", None))
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                raw = exc.read()
                reply_headers = _header_map(exc.headers)
            except (
                urllib.error.URLError,
                ssl.SSLError,
                TimeoutError,
                ConnectionError,
            ) as exc:
                reason = getattr(exc, "reason", exc)
                if attempt < self._attempts:
                    delay = _retry_delay(reply_headers, attempt)
                    print(
                        f"  网络错误：{reason}；{delay:.1f}s 后重试 "
                        f"({attempt}/{self._attempts - 1})",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                raise TransientAPIError(
                    f"{method} {url} 网络失败（共尝试 {self._attempts} 次）：{reason}"
                ) from exc

            payload = _parse_json(raw)
            if status == 429 or status >= 500:
                message = (
                    payload.get("message") if isinstance(payload, dict) else None
                ) or raw[:200].decode("utf-8", "replace")
                if attempt < self._attempts:
                    delay = _retry_delay(reply_headers, attempt)
                    print(
                        f"  HTTP {status}：{message}；{delay:.1f}s 后重试 "
                        f"({attempt}/{self._attempts - 1})",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                raise TransientAPIError(
                    f"{method} {url} 返回 HTTP {status}（共尝试 "
                    f"{self._attempts} 次）：{message}"
                )
            if status not in expected:
                message = (
                    payload.get("message") if isinstance(payload, dict) else None
                ) or raw[:200].decode("utf-8", "replace")
                raise PushError(
                    f"GitHub API {method} {url} 返回 HTTP {status}：{message}"
                )
            return Reply(status=status, headers=reply_headers, payload=payload)
        raise AssertionError("unreachable: retry loop always returns or raises")

    # -- reads -------------------------------------------------------------

    def get_ref(self, branch: str) -> RemoteRef | None:
        """The branch ref, or ``None`` when the remote branch does not exist."""
        url = (
            f"{self._api}/repos/{self._slug}/git/ref/heads/"
            f"{urllib.parse.quote(branch, safe='/')}"
        )
        reply = self._request("GET", url, expected=(200, 404))
        if reply.status == 404:
            return None
        payload = self._as_dict(reply, f"读取分支 {branch}")
        target = payload.get("object")
        if not isinstance(target, dict) or not isinstance(target.get("sha"), str):
            raise PushError(f"分支 {branch} 的 API 回复缺少 object.sha")
        ref = payload.get("ref")
        return RemoteRef(
            ref=ref if isinstance(ref, str) else f"refs/heads/{branch}",
            sha=str(target["sha"]),
        )

    def get_commit(self, sha: str) -> RemoteCommit:
        """A remote commit's tree; missing commits are a hard error."""
        url = f"{self._api}/repos/{self._slug}/git/commits/{sha}"
        reply = self._request("GET", url)
        payload = self._as_dict(reply, f"读取远端提交 {sha[:10]}")
        tree = payload.get("tree")
        if not isinstance(tree, dict) or not isinstance(tree.get("sha"), str):
            raise PushError(f"远端提交 {sha[:10]} 的回复缺少 tree.sha")
        return RemoteCommit(sha=sha, tree=str(tree["sha"]))

    def get_commit_optional(self, sha: str) -> RemoteCommit | None:
        """Like :meth:`get_commit` but ``None`` when the object is absent."""
        url = f"{self._api}/repos/{self._slug}/git/commits/{sha}"
        reply = self._request("GET", url, expected=(200, 404))
        if reply.status == 404:
            return None
        payload = self._as_dict(reply, f"读取远端提交 {sha[:10]}")
        tree = payload.get("tree")
        if not isinstance(tree, dict) or not isinstance(tree.get("sha"), str):
            raise PushError(f"远端提交 {sha[:10]} 的回复缺少 tree.sha")
        return RemoteCommit(sha=sha, tree=str(tree["sha"]))

    def get_tree_blobs(self, tree_sha: str) -> set[str]:
        """Every blob SHA reachable from a remote tree (already on the server)."""
        url = f"{self._api}/repos/{self._slug}/git/trees/{tree_sha}?recursive=1"
        reply = self._request("GET", url)
        payload = self._as_dict(reply, f"读取远端 tree {tree_sha[:10]}")
        if payload.get("truncated"):
            raise PushError(
                "远端 tree 过大，GitHub 的 recursive=1 回复被截断；"
                "请先把基准分支推到一个较小的提交"
            )
        entries = payload.get("tree")
        if not isinstance(entries, list):
            raise PushError(f"远端 tree {tree_sha[:10]} 的回复缺少 tree 数组")
        blobs: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") == "blob" and isinstance(entry.get("sha"), str):
                blobs.add(str(entry["sha"]))
        return blobs

    # -- writes ------------------------------------------------------------

    def create_blob(self, content: bytes) -> str:
        """Upload one blob; GitHub returns the content-addressed SHA."""
        url = f"{self._api}/repos/{self._slug}/git/blobs"
        reply = self._request(
            "POST",
            url,
            json_body={
                "content": base64.b64encode(content).decode("ascii"),
                "encoding": "base64",
            },
            expected=(201,),
        )
        payload = self._as_dict(reply, "创建 blob")
        sha = payload.get("sha")
        if not isinstance(sha, str):
            raise PushError("创建 blob 的 API 回复缺少 sha")
        return sha

    def create_tree(self, entries: list[dict[str, str]]) -> str:
        """Create a tree from explicit entries; returns its SHA."""
        if len(entries) > MAX_TREE_ENTRIES:
            raise PushError(
                f"tree 有 {len(entries)} 个条目，超过 GitHub 的 {MAX_TREE_ENTRIES} 上限"
            )
        body = json.dumps({"tree": entries}).encode("utf-8")
        if len(body) > MAX_TREE_BYTES:
            raise PushError(
                f"tree 请求体 {len(body)} 字节，超过 GitHub 的 "
                f"{MAX_TREE_BYTES} 字节上限"
            )
        url = f"{self._api}/repos/{self._slug}/git/trees"
        reply = self._request("POST", url, json_body={"tree": entries}, expected=(201,))
        payload = self._as_dict(reply, "创建 tree")
        sha = payload.get("sha")
        if not isinstance(sha, str):
            raise PushError("创建 tree 的 API 回复缺少 sha")
        return sha

    def create_commit(
        self,
        *,
        message: str,
        tree: str,
        parents: Sequence[str],
        author: Identity,
        committer: Identity,
    ) -> str:
        """Create a commit preserving message, author, committer and dates."""
        url = f"{self._api}/repos/{self._slug}/git/commits"
        reply = self._request(
            "POST",
            url,
            json_body={
                "message": message,
                "tree": tree,
                "parents": list(parents),
                "author": _identity_body(author),
                "committer": _identity_body(committer),
            },
            expected=(201,),
        )
        payload = self._as_dict(reply, "创建 commit")
        sha = payload.get("sha")
        if not isinstance(sha, str):
            raise PushError("创建 commit 的 API 回复缺少 sha")
        return sha

    def create_ref(self, branch: str, sha: str) -> None:
        """Create ``refs/heads/<branch>``; an identical existing ref is fine."""
        url = f"{self._api}/repos/{self._slug}/git/refs"
        reply = self._request(
            "POST",
            url,
            json_body={"ref": f"refs/heads/{branch}", "sha": sha},
            expected=(201, 422),
        )
        if reply.status == 201:
            return
        existing = self.get_ref(branch)
        if existing is not None and existing.sha == sha:
            return
        current = existing.sha[:10] if existing is not None else "未知"
        raise PushError(
            f"远端分支 {branch} 已存在且指向 {current}，与本次推送的 "
            f"{sha[:10]} 不同；拒绝 force 覆盖"
        )

    def update_ref(self, branch: str, sha: str) -> None:
        """Fast-forward ``refs/heads/<branch>``; never force."""
        url = (
            f"{self._api}/repos/{self._slug}/git/refs/heads/"
            f"{urllib.parse.quote(branch, safe='/')}"
        )
        reply = self._request(
            "PATCH",
            url,
            json_body={"sha": sha, "force": False},
            expected=(200, 409, 422),
        )
        if reply.status == 200:
            return
        # A retry may have already applied the update; that is idempotent.
        existing = self.get_ref(branch)
        if existing is not None and existing.sha == sha:
            return
        current = existing.sha[:10] if existing is not None else "未知"
        raise PushError(
            f"更新远端分支 {branch} 被拒绝（当前 {current}，目标 {sha[:10]}）；"
            "远端可能已有新提交，本脚本不会 force 覆盖，请先 fetch 并 rebase"
        )

    @staticmethod
    def _as_dict(reply: Reply, context: str) -> dict[str, Any]:
        if not isinstance(reply.payload, dict):
            raise PushError(f"{context}：API 回复不是 JSON 对象")
        return reply.payload


def _identity_body(identity: Identity) -> dict[str, str]:
    return {
        "name": identity.name,
        "email": identity.email,
        "date": identity.date,
    }


# ---------------------------------------------------------------------------
# Planning: find the already-pushed boundary with a tree comparison
# ---------------------------------------------------------------------------


def normalize_branch(value: str) -> str:
    """Accept ``main``, ``heads/main`` or ``refs/heads/main``."""
    text = value.strip()
    for prefix in ("refs/heads/", "heads/"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    if text.startswith("refs/"):
        raise PushError(f"--base 只支持分支，不支持 {value!r}")
    if not text or not _REF_RE.fullmatch(text) or ".." in text:
        raise PushError(f"非法的分支名：{value!r}")
    return text


def find_base(
    history: Sequence[tuple[str, str]],
    remote_sha: str,
    remote_tree: str,
) -> Base | None:
    """
    The local commit whose content is already on the remote branch.

    An exact SHA match is preferred: even an empty commit after it must still
    be replayed.  Only when the remote SHA is not in local history does a tree
    comparison decide — a rewritten/cherry-picked history with identical
    content is safe to continue from.  Anything else means the remote moved
    independently and must not be overwritten.
    """
    for sha, _tree in history:
        if sha == remote_sha:
            return Base(local_sha=sha, remote_sha=remote_sha)
    for sha, tree in history:
        if tree == remote_tree:
            return Base(local_sha=sha, remote_sha=remote_sha)
    return None


def build_plan(client: GitHubClient, root: Path, branch: str) -> PushPlan:
    """Read remote state and prepare every commit without writing anything."""
    history = local_history(root)
    remote_ref = client.get_ref(branch)
    base: Base | None = None
    remote_tree: str | None = None
    if remote_ref is None:
        base_sha: str | None = None
    else:
        remote_commit = client.get_commit(remote_ref.sha)
        remote_tree = remote_commit.tree
        base = find_base(history, remote_ref.sha, remote_commit.tree)
        if base is None:
            raise PushError(
                f"远端分支 {branch} 指向 {remote_ref.sha[:10]}，"
                "其 tree 与本地 main 的任何提交都不同：远端可能领先或历史"
                "已分叉。本脚本不会 force 覆盖；请先 fetch 并 rebase，"
                "或用 --base 指定正确的远端基准分支"
            )
        base_sha = base.local_sha

    commits = tuple(
        PlannedCommit(commit=commit, files=read_commit_tree(root, commit.sha))
        for commit in commits_between(root, base_sha)
    )
    _validate_chain(commits, base)
    return PushPlan(
        branch=branch,
        base=base,
        remote_ref_sha=None if remote_ref is None else remote_ref.sha,
        remote_tree=remote_tree,
        commits=commits,
    )


def _validate_chain(commits: Sequence[PlannedCommit], base: Base | None) -> None:
    """Every first parent must be the base or a commit earlier in the replay."""
    known = {base.local_sha} if base is not None else set()
    for planned in commits:
        parents = planned.commit.parents
        if parents and parents[0] not in known:
            raise PushError(
                f"提交 {planned.commit.sha[:10]} 的父提交 {parents[0][:10]} "
                "不在本地基准之后的 first-parent 链上；请先 rebase"
            )
        known.add(planned.commit.sha)


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def create_nested_tree(
    client: GitHubClient, entries: Sequence[tuple[str, str, str]]
) -> str:
    """
    Turn ``(path, mode, blob_sha)`` triples into a nested GitHub tree.

    Directories are created deepest first; each directory lists its files and
    its immediate child trees.
    """
    file_dirs: dict[str, list[tuple[str, str, str]]] = {}
    dirs: set[str] = {""}
    for path, mode, sha in entries:
        if not path or path.startswith("/") or "//" in path:
            raise PushError(f"非法的 tree 路径：{path!r}")
        parent, _, name = path.rpartition("/")
        file_dirs.setdefault(parent, []).append((name, mode, sha))
        while parent:
            dirs.add(parent)
            parent = parent.rpartition("/")[0]

    children: dict[str, set[str]] = {directory: set() for directory in dirs}
    for directory in dirs:
        if not directory:
            continue
        parent, _, name = directory.rpartition("/")
        children.setdefault(parent, set()).add(name)

    shas: dict[str, str] = {}
    for directory in sorted(
        dirs, key=lambda value: len(value.split("/")) if value else 0, reverse=True
    ):
        payload: list[dict[str, str]] = [
            {"path": name, "mode": mode, "type": "blob", "sha": sha}
            for name, mode, sha in file_dirs.get(directory, [])
        ]
        payload.extend(
            {
                "path": child,
                "mode": "040000",
                "type": "tree",
                "sha": shas[child],
            }
            for child in sorted(children.get(directory, set()))
        )
        payload.sort(key=lambda entry: entry["path"])
        shas[directory] = client.create_tree(payload)
    return shas[""]


def push_plan(
    client: GitHubClient,
    root: Path,
    plan: PushPlan,
    *,
    dry_run: bool,
) -> None:
    """Print the plan and, unless *dry_run*, replay it through the API."""
    if dry_run:
        _print_plan(plan)
        return
    if not plan.commits:
        target = plan.remote_ref_sha or "远端"
        print(f"已同步：{plan.branch} 与远端 {target[:10]} 一致，无需推送。")
        return

    blob_map: dict[str, str] = {}
    if plan.remote_tree is not None:
        blob_map = {sha: sha for sha in client.get_tree_blobs(plan.remote_tree)}

    local_to_remote: dict[str, str] = {}
    if plan.base is not None:
        local_to_remote[plan.base.local_sha] = plan.base.remote_sha

    total = len(plan.commits)
    remote_tip = plan.remote_ref_sha
    for index, planned in enumerate(plan.commits, 1):
        commit = planned.commit
        tree_sha = build_remote_tree(client, root, planned.files, blob_map)
        parents: list[str] = []
        for parent in commit.parents:
            mapped = local_to_remote.get(parent)
            if mapped is None:
                remote = client.get_commit_optional(parent)
                if remote is None:
                    raise PushError(
                        f"提交 {commit.sha[:10]} 的父提交 {parent[:10]} 不在远端，"
                        "也不是本次 replay 的一部分；请先 rebase 去掉合并提交"
                    )
                mapped = remote.sha
                local_to_remote[parent] = mapped
            parents.append(mapped)
        remote_sha = client.create_commit(
            message=commit.message,
            tree=tree_sha,
            parents=parents,
            author=commit.author,
            committer=commit.committer,
        )
        local_to_remote[commit.sha] = remote_sha
        remote_tip = remote_sha
        print(
            f"[{index}/{total}] {commit.sha[:10]} {_subject(commit.message)} "
            f"({len(planned.files)} 个文件) -> {remote_sha[:10]}"
        )

    assert remote_tip is not None
    if plan.remote_ref_sha is None:
        client.create_ref(plan.branch, remote_tip)
        print(f"已创建远端分支 {plan.branch} -> {remote_tip[:10]}")
    else:
        client.update_ref(plan.branch, remote_tip)
        print(
            f"已更新远端分支 {plan.branch}: "
            f"{plan.remote_ref_sha[:10]} -> {remote_tip[:10]}"
        )


def build_remote_tree(
    client: GitHubClient,
    root: Path,
    files: Sequence[TreeFile],
    blob_map: dict[str, str],
) -> str:
    """Upload missing blobs, then create the commit's complete tree."""
    triples: list[tuple[str, str, str]] = []
    for file in files:
        remote_blob = blob_map.get(file.sha)
        if remote_blob is None:
            remote_blob = client.create_blob(read_blob(root, file.sha))
            blob_map[file.sha] = remote_blob
        triples.append((file.path, file.mode, remote_blob))
    return create_nested_tree(client, triples)


def _subject(message: str) -> str:
    first = message.splitlines()[0] if message.splitlines() else ""
    return first or "(无提交信息)"


def _print_plan(plan: PushPlan) -> None:
    if not plan.commits:
        target = plan.remote_ref_sha or "远端"
        print(f"已同步：{plan.branch} 与远端 {target[:10]} 一致，无需推送。")
        return
    if plan.base is None:
        print(f"远端分支 {plan.branch} 不存在：将创建并推送全部本地提交。")
    else:
        print(
            f"远端基准：{plan.branch} @ {plan.base.remote_sha[:10]}"
            f"（本地 {plan.base.local_sha[:10]}）"
        )
    print(f"将推送 {len(plan.commits)} 个提交（dry-run，不会写入）：")
    for planned in plan.commits:
        print(
            f"  {planned.commit.sha[:10]} {_subject(planned.commit.message)} "
            f"({len(planned.files)} 个文件)"
        )
    print(f"合计 {len(plan.commits)} 个提交、{plan.file_count} 个文件条目。")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The command-line parser (Chinese help, standard library only)."""
    parser = argparse.ArgumentParser(
        prog="push_via_api.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    parser.add_argument(
        "-h",
        "--help",
        action="help",
        help="显示本帮助并退出",
    )
    parser.add_argument(
        "--repo",
        metavar="OWNER/NAME",
        help="目标仓库（默认从 git remote get-url origin 解析）",
    )
    parser.add_argument(
        "--base",
        default="main",
        metavar="BRANCH",
        help="远端基准分支，同时作为推送目标（默认 main）",
    )
    parser.add_argument(
        "--api-base",
        default=os.environ.get("GITHUB_API_URL", GITHUB_API),
        metavar="URL",
        help=f"GitHub API 基地址（默认 {GITHUB_API}）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只读取远端状态并打印将推送的提交与文件数，不写任何 API",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the push; return 0 on success (including an already-synced branch)."""
    args = build_parser().parse_args(argv)
    try:
        branch = normalize_branch(args.base)
        slug = normalize_slug(args.repo) if args.repo else origin_slug(ROOT)
        token = github_token(ROOT)
        if not token:
            raise PushError(
                "未找到 GitHub token：请设置 GITHUB_TOKEN/GH_TOKEN，或让 "
                "git credential fill 能返回 github.com 的密码"
            )
        client = GitHubClient(slug, token, args.api_base)
        plan = build_plan(client, ROOT, branch)
        push_plan(client, ROOT, plan, dry_run=bool(args.dry_run))
    except PushError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
