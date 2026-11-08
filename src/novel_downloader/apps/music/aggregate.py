"""跨平台聚合搜索。

单平台搜索要求调用方先选平台：每个平台的曲目 id、凭据和直链解析都是
独立的。 但这只是实现细节，不该成为界面的约束——同一首歌在网易云、
QQ、酷狗上往往都能找到，一起搜再合并成一行才符合用户直觉。

本模块做三件事：

* **并发**：每个平台一个线程，先到先收；某个平台挂掉只影响它自己。
* **合并**：同名同歌手的曲目折叠成一行，并记录 ``sources``
  （命中的平台，最佳来源在前）。会员曲目排在后面，避免合并后只留下
  一个下载不了的来源。
* **排序**：关键词相关度优先，其次非会员、来源数量、平台优先级。

下载时仍然需要原始 provider 对象（musicdl 的直链解析数据不是
JSON 可序列化的），所以搜索结果要配合 :mod:`trackcache` 使用。
"""

from __future__ import annotations

import contextlib
import logging
import multiprocessing
import os
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from .providers import available, create
from .providers.base import Track

logger = logging.getLogger(__name__)

__all__ = [
    "ALL_PLATFORM_KEY",
    "AggregateResult",
    "PlatformOutcome",
    "is_all_platforms",
    "merge_tracks",
    "normalize_text",
    "search_all",
    "search_all_isolated",
]

#: ``platform`` 参数的「全部平台」取值。
ALL_PLATFORM_KEY = "all"

#: 聚合搜索默认每平台取多少条。20+ 个平台各取 8 条已经足够挑歌，再多
#: 只会让合并后的列表变长、也更慢。
DEFAULT_PER_PLATFORM_LIMIT = 8

#: 聚合搜索默认墙钟预算（秒）。超时的平台按失败上报，已回来的结果照常合并。
DEFAULT_TIMEOUT = 12.0

#: 同时在跑的平台数量上限：每个平台内部还有自己的请求线程池。
_MAX_WORKERS = 10

#: 平台侧用来表示「未知」的占位值。
_UNKNOWN = frozenset({"", "未知歌曲", "未知歌手", "未知专辑", "unknown", "n/a", "-"})

_NON_WORD_RE = re.compile(r"[\W_]+", re.UNICODE)


def is_all_platforms(platform: object) -> bool:
    """``platform`` 是否表示「全部平台」（空值也算）。"""
    value = str(platform or "").strip().lower()
    return value in {"", ALL_PLATFORM_KEY}


def normalize_text(value: object) -> str:
    """用于比较的规范化：全半角统一、去标点、去空格、小写。"""
    text = unicodedata.normalize("NFKC", str(value if value is not None else ""))
    return _NON_WORD_RE.sub("", text.casefold())


@dataclass(frozen=True)
class PlatformOutcome:
    """一个平台在聚合搜索中的结果；失败不抛异常，只记录原因。"""

    platform: str
    ok: bool
    count: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "ok": self.ok,
            "count": self.count,
            "error": self.error,
        }


@dataclass
class AggregateResult:
    """聚合搜索的完整结果。"""

    tracks: list[Track] = field(default_factory=list)
    platforms: list[PlatformOutcome] = field(default_factory=list)
    #: 合并后的曲目总数（``tracks`` 被 ``limit`` 截断前）。
    total: int = 0

    @property
    def ok_count(self) -> int:
        return sum(1 for item in self.platforms if item.ok)

    def platform_map(self) -> dict[str, dict[str, object]]:
        return {item.platform: item.as_dict() for item in self.platforms}


def _platform_order() -> list[str]:
    return [key for key, _label in available()]


def _platform_position(key: str) -> int:
    order = _platform_order()
    try:
        return order.index(key)
    except ValueError:
        return len(order)


def _relevance(track: Track, needle: str) -> float:
    """曲名 / 歌手与关键词的 0..1 相关度（曲名优先）。"""
    if not needle:
        return 0.5

    def score(text: str, *, exact: float, prefix: float, contains: float) -> float:
        value = normalize_text(text)
        if not value:
            return 0.0
        if value == needle:
            return exact
        if value.startswith(needle):
            return prefix
        if needle in value:
            return contains
        coverage = sum(1 for ch in needle if ch in set(value)) / len(needle)
        ordered = SequenceMatcher(None, needle, value).ratio()
        return 0.5 * (0.5 * coverage + 0.5 * ordered)

    name = score(track.name, exact=1.0, prefix=0.85, contains=0.72)
    artist = score(track.artists, exact=0.9, prefix=0.7, contains=0.7)
    return max(name, artist * 0.9)


def _merge_key(track: Track) -> tuple[str, str]:
    """同名同歌手视为同一首；歌手未知时退回专辑，避免误合并翻唱。"""
    name = normalize_text(track.name)
    artist = normalize_text(track.artists)
    if artist in _UNKNOWN:
        artist = "album:" + normalize_text(track.album)
    return name, artist


def _preference(track: Track, rank: int) -> tuple[int, int, int, int, int]:
    """合并时挑选主来源：非会员 > 有封面 > 有完整时长 > 有 id > 平台更靠前。"""
    return (
        0 if track.vip else 1,
        1 if str(track.cover_url or "").strip() else 0,
        1 if int(track.duration_s or 0) > 0 else 0,
        1 if str(track.id or "").strip() else 0,
        -rank,
    )


def merge_tracks(
    groups: list[tuple[str, list[Track]]],
    keyword: str = "",
    *,
    limit: int | None = None,
) -> list[Track]:
    """
    把各平台的原始结果合并成一行一首。

    :param groups: ``[(platform_key, tracks), ...]``，按平台优先级排列。
    :param keyword: 原始搜索词，用于相关度排序。
    :param limit: 合并后最多返回多少首；``None`` 表示不限。
    :return: 合并、排序后的曲目列表；每项的 ``sources`` 列出全部来源平台。
    """
    order = {key: index for index, key in enumerate(_platform_order())}
    buckets: dict[tuple[str, str], list[tuple[int, Track]]] = {}
    for platform, tracks in groups:
        rank = order.get(platform, len(order))
        for track in tracks:
            if not str(track.name or "").strip():
                continue
            key = _merge_key(track)
            if not key[0]:
                continue
            buckets.setdefault(key, []).append((rank, track))

    merged: list[Track] = []
    for items in buckets.values():
        # 主来源：偏好最高者；同分时保持平台顺序
        items.sort(key=lambda pair: _preference(pair[1], pair[0]), reverse=True)
        primary = items[0][1]
        # 其余来源按平台优先级排列（而不是偏好），这样同一首歌的来源顺序
        # 只取决于注册表，搜索完成顺序不会影响输出。
        rest = sorted(
            (pair for pair in items if pair[1] is not primary),
            key=lambda pair: pair[0],
        )
        sources: list[str] = []
        for track in (primary, *(pair[1] for pair in rest)):
            provider = str(track.provider or "").strip()
            if provider and provider not in sources:
                sources.append(provider)
        primary.sources = sources
        merged.append(primary)

    needle = normalize_text(keyword)
    merged.sort(
        key=lambda track: (
            -_relevance(track, needle),
            1 if track.vip else 0,
            -len(track.sources),
            order.get(str(track.provider or ""), len(order)),
            normalize_text(track.name),
            normalize_text(track.artists),
        )
    )
    if limit is not None:
        return merged[: max(0, limit)]
    return merged


def _select_platforms(platforms: list[str] | None) -> list[str]:
    """请求的平台列表（去重、保序）；``None`` 表示全部已注册平台。"""
    if platforms is None:
        return _platform_order()
    selected: list[str] = []
    for key in platforms:
        value = str(key or "").strip()
        if value and value not in selected:
            selected.append(value)
    known = set(_platform_order())
    unknown = [key for key in selected if key not in known]
    if unknown:
        raise KeyError(f"未知音乐平台：{', '.join(unknown)}")
    return selected


def search_all(
    keyword: str,
    platforms: list[str] | None = None,
    limit: int | None = None,
    *,
    per_platform_limit: int = DEFAULT_PER_PLATFORM_LIMIT,
    timeout: float = DEFAULT_TIMEOUT,
    workers: int = _MAX_WORKERS,
) -> AggregateResult:
    """
    并发搜索多个平台并合并结果。

    ``platforms=None`` 搜索全部已注册平台。 单个平台的异常（缺依赖、
    网络不可达、解析失败）只记为该项的 ``error``，不影响其它平台。

    :param keyword: 搜索词。
    :param platforms: 平台 key 列表；``None`` 为全部。
    :param limit: 合并后最多返回多少首。
    :param per_platform_limit: 每个平台取多少条原始结果。
    :param timeout: 整体墙钟预算（秒）；超时的平台按失败上报。
    :param workers: 同时搜索的平台数。
    :return: :class:`AggregateResult`。
    """
    text = str(keyword or "").strip()
    if not text:
        return AggregateResult()

    keys = _select_platforms(platforms)
    if not keys:
        return AggregateResult()
    per_platform = max(1, int(per_platform_limit))
    deadline_seconds = max(0.1, float(timeout))

    def run_one(key: str) -> tuple[list[Track], int]:
        # musicdl 平台支持 (connect, read) 超时；把它们压到低于整体预算，
        # 这样即便调用方到点返回，后台请求也会很快结束，不会在解释器退出
        # 时把进程拖住（vendor 内部的线程池不是 daemon）。netease 等不接
        # 受该参数的 provider 退回默认构造。
        try:
            provider = create(key, timeout=(4.0, 8.0))
        except TypeError:
            provider = create(key)
        tracks, total = provider.search(text, limit=per_platform)
        return list(tracks or ()), int(total or len(tracks or ()))

    outcomes: dict[str, PlatformOutcome] = {}
    groups: list[tuple[str, list[Track]]] = []

    # 手写 daemon 线程池，而不是 ThreadPoolExecutor：后者的工作线程不是
    # daemon，某个平台无视取消（比如 socket 读数卡在 45s 的平台超时上）
    # 会把整个进程的退出一起拖住——CLI 一次性命令尤其明显。
    pending = list(keys)
    pending_lock = threading.Lock()
    finished = threading.Event()
    results: dict[str, tuple[list[Track], int] | BaseException] = {}

    def worker() -> None:
        while True:
            with pending_lock:
                if not pending:
                    return
                key = pending.pop(0)
            try:
                outcome: tuple[list[Track], int] | BaseException = run_one(key)
            except BaseException as exc:  # noqa: BLE001 - 单平台失败不影响全局
                outcome = exc
            with pending_lock:
                results[key] = outcome
                if len(results) >= len(keys):
                    finished.set()

    threads = [
        threading.Thread(target=worker, name=f"music-search-{index}", daemon=True)
        for index in range(max(1, min(int(workers), len(keys))))
    ]
    for thread in threads:
        thread.start()
    # 到点就返回：还在跑的平台由 daemon 线程自行结束，进程退出不会被它们
    # 拦住；它们的结果也不会再被使用。
    finished.wait(deadline_seconds)

    with pending_lock:
        for key in keys:
            outcome = results.get(key)
            if outcome is None:
                outcomes[key] = PlatformOutcome(
                    key, False, error=f"搜索超时（{deadline_seconds:g}s）"
                )
            elif isinstance(outcome, BaseException):
                outcomes[key] = PlatformOutcome(key, False, error=_short_error(outcome))
            else:
                tracks, _total = outcome
                outcomes[key] = PlatformOutcome(key, True, count=len(tracks))
                groups.append((key, tracks))

    merged = merge_tracks(
        sorted(groups, key=lambda pair: _platform_position(pair[0])),
        text,
        limit=limit,
    )
    ordered = [outcomes[key] for key in keys if key in outcomes]
    return AggregateResult(tracks=merged, platforms=ordered, total=len(merged))


def _short_error(exc: BaseException) -> str:
    """把平台异常压成一行可读原因（完整堆栈由调用方按需记录）。"""
    message = str(exc).strip() or type(exc).__name__
    return message[:200]


def _child_search(conn: Any, kwargs: dict[str, Any]) -> None:
    """子进程入口：跑完搜索、把结果送回去，然后立刻退出。"""
    try:
        result = search_all(**kwargs)
        conn.send(("ok", result))
    except BaseException as exc:  # noqa: BLE001 - 交给父进程展示
        with contextlib.suppress(Exception):
            conn.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        try:
            conn.close()
        finally:
            # vendor 平台内部有自己的线程池，且不是 daemon；正常退出会被
            # 卡住的请求拖住。结果已经送出，直接结束进程。
            os._exit(0)


def search_all_isolated(
    keyword: str,
    platforms: list[str] | None = None,
    limit: int | None = None,
    *,
    per_platform_limit: int = DEFAULT_PER_PLATFORM_LIMIT,
    timeout: float = DEFAULT_TIMEOUT,
    startup_grace: float = 10.0,
) -> AggregateResult:
    """
    在子进程里执行 :func:`search_all`，返回后立即结束子进程。

    一次性命令（CLI）用这个版本：musicdl 的平台客户端会在内部再开线程池，
    其中个别平台的请求可能完全无视超时；它们会把解释器的正常退出拖到
    几十秒甚至更久。子进程把「搜索耗时」和「进程退出」彻底隔开，父进程
    拿到结果就 ``terminate`` 掉它。

    长驻进程（桌面/API/内嵌引擎）请直接用 :func:`search_all`：它们不需要
    等待进程退出，也就不受这个影响。

    子进程崩溃或超时返回空的 :class:`AggregateResult`，``platforms`` 里每个
    选中的平台都标记为失败，便于界面提示。
    """
    text = str(keyword or "").strip()
    if not text:
        return AggregateResult()
    keys = _select_platforms(platforms)
    if not keys:
        return AggregateResult()

    timeout = max(0.1, float(timeout))
    kwargs = {
        "keyword": text,
        "platforms": keys,
        "limit": limit,
        "per_platform_limit": per_platform_limit,
        "timeout": timeout,
    }

    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    process = ctx.Process(
        target=_child_search,
        args=(child_conn, kwargs),
        name="music-search",
        daemon=True,
    )
    try:
        process.start()
    except Exception as exc:  # noqa: BLE001 - 无法 fork 时退回进程内
        parent_conn.close()
        child_conn.close()
        logger.warning("music aggregate: cannot start worker (%s); running inline", exc)
        return search_all(
            text,
            keys,
            limit,
            per_platform_limit=per_platform_limit,
            timeout=timeout,
        )
    child_conn.close()

    payload: tuple[str, Any] | None = None
    try:
        # 子进程内部还有自己的 timeout；这里只等「启动 + 搜索 + 回传」。
        deadline = timeout + max(1.0, float(startup_grace))
        if parent_conn.poll(deadline):
            payload = parent_conn.recv()
    except (EOFError, OSError):
        payload = None
    finally:
        parent_conn.close()
        if process.is_alive():
            process.terminate()
        process.join(1.0)

    if payload is None:
        return AggregateResult(
            platforms=[
                PlatformOutcome(key, False, error="搜索进程未返回结果") for key in keys
            ]
        )
    status, value = payload
    if status == "ok" and isinstance(value, AggregateResult):
        return value
    message = str(value or "搜索进程失败")[:200]
    return AggregateResult(
        platforms=[PlatformOutcome(key, False, error=message) for key in keys]
    )
