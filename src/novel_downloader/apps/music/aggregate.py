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
import json
import logging
import multiprocessing
import os
import re
import threading
import time
import unicodedata
from collections.abc import Callable
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
    "clear_failure_cache",
    "failed_platforms",
    "search_all",
    "search_all_isolated",
]

#: ``platform`` 参数的「全部平台」取值。
ALL_PLATFORM_KEY = "all"

#: 聚合搜索默认每平台取多少条。20+ 个平台各取 8 条已经足够挑歌，再多
#: 只会让合并后的列表变长、也更慢。
DEFAULT_PER_PLATFORM_LIMIT = 8

#: 聚合搜索默认墙钟预算（秒）。超时的平台按失败上报，已回来的结果照常合并。
DEFAULT_TIMEOUT = 5.0

#: 失败平台的熔断时长（秒）。一次聚合搜索里绝大多数平台只是被墙/已失效；
#: 记住这个结果，后续搜索直接跳过，避免每次都为同一批死站等满预算。
FAILURE_TTL = 600.0

#: 「慢且空」平台的观察窗口。连续两次超过 2.5 秒还一条结果都没有的平台，
#: 短暂跳过 5 分钟：它们贡献为零，却会把每次搜索拖到预算上限。
SLOW_EMPTY_MS = 2000
SLOW_EMPTY_STREAK = 2
SLOW_EMPTY_TTL = 300.0

#: 首个「有结果」的平台返回后，再等这么久就收工：剩下的基本是慢站，
#: 而它们多数返回空；继续等只会让用户盯着进度条。未完成的平台不记为失败
#: （它们可能只是慢），下次搜索照常参与。
EARLY_EXIT_GRACE = 2.0

#: 搜索线程数上限。每个平台一个线程：以前固定 10 个 worker 会让队列里的
#: 平台饿死——前 10 个恰好全是卡死的站时，netease 这种 1 秒就能返回的
#: 平台根本轮不到执行，整次搜索看起来「什么都没搜到」。
MAX_SEARCH_THREADS = 32

#: 聚合搜索时优先启动的平台：能快速返回、结果质量也稳定的站排前面，
#: 这样渐进式界面能在一秒内出第一批结果。
_SEARCH_PRIORITY = (
    "netease",
    "qq",
    "kugou",
    "kuwo",
    "migu",
    "qianqian",
    "joox",
    "fivesing",
    "bilibili",
    "suno",
    "jamendo",
    "audius",
)

#: 平台名 -> ``{"ok": bool, "last_ms": int, "failed_until": epoch 秒}``。
#: 落盘到 ``<音乐数据目录>/search_stats.json``：重启后仍然知道哪些站是死的，
#: 第一次搜索就不会再为同一批被墙平台等满预算。
_STATS: dict[str, dict[str, Any]] | None = None
_FAILURE_LOCK = threading.Lock()


def _stats_file() -> Any:
    from .paths import music_data_dir

    return music_data_dir() / "search_stats.json"


def _load_stats() -> dict[str, dict[str, Any]]:
    global _STATS
    if _STATS is not None:
        return _STATS
    data: dict[str, dict[str, Any]] = {}
    try:
        with _stats_file().open(encoding="utf-8") as fh:
            raw = json.load(fh)
        platforms = raw.get("platforms") if isinstance(raw, dict) else None
        if isinstance(platforms, dict):
            for key, value in platforms.items():
                if not isinstance(value, dict):
                    continue
                data[str(key)] = {
                    "ok": bool(value.get("ok")),
                    "last_ms": int(value.get("last_ms") or 0),
                    "failed_until": float(value.get("failed_until") or 0.0),
                }
    except (OSError, ValueError, TypeError):
        data = {}
    _STATS = data
    return data


def _save_stats() -> None:
    """Best-effort atomic write; never let stats break a search."""
    data = _load_stats()
    target = _stats_file()
    tmp = target.with_suffix(".tmp")
    try:
        with _FAILURE_LOCK:
            payload = {"version": 1, "platforms": dict(data)}
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, target)
    except (OSError, ValueError, TypeError):
        logger.debug("music aggregate: could not persist search stats", exc_info=True)


def _breaker_open(key: str, now: float | None = None) -> bool:
    """该平台是否在熔断期（失败或连续「慢且空」）。"""
    stamp = now if now is not None else time.time()
    with _FAILURE_LOCK:
        entry = _load_stats().get(key)
        if entry is None:
            return False
        until = float(entry.get("failed_until") or 0.0)
        if until <= stamp:
            entry["failed_until"] = 0.0
            return False
        return True


def _record_failure(key: str, elapsed_ms: int = 0) -> None:
    with _FAILURE_LOCK:
        entry = _load_stats().setdefault(key, {})
        entry.update(
            ok=False,
            last_ms=int(elapsed_ms or 0),
            failed_until=time.time() + FAILURE_TTL,
        )


def _record_success(key: str, elapsed_ms: int = 0, count: int = 0) -> None:
    with _FAILURE_LOCK:
        entry = _load_stats().setdefault(key, {})
        slow_empty = int(elapsed_ms or 0) >= SLOW_EMPTY_MS and int(count) == 0
        streak = int(entry.get("empty_streak") or 0) + 1 if slow_empty else 0
        entry.update(
            ok=True,
            last_ms=int(elapsed_ms or 0),
            empty_streak=streak,
            failed_until=0.0,
        )
        if streak >= SLOW_EMPTY_STREAK:
            entry["failed_until"] = time.time() + SLOW_EMPTY_TTL
            logger.info(
                "music aggregate: %s keeps returning nothing slowly; skipping for %gs",
                key,
                SLOW_EMPTY_TTL,
            )


def clear_failure_cache() -> None:
    """清空平台熔断记录（健康检查成功或用户手动重试时使用）。"""
    global _STATS
    with _FAILURE_LOCK:
        _STATS = {}
    _save_stats()


def failed_platforms() -> list[str]:
    """当前处于熔断期的平台（调试/界面提示用）。"""
    now = time.time()
    with _FAILURE_LOCK:
        return [
            key
            for key, entry in _load_stats().items()
            if float(entry.get("failed_until") or 0.0) > now
        ]


def _ordered_keys(keys: list[str]) -> list[str]:
    """把优先平台提到前面，其余保持注册表顺序。"""
    priority = {key: index for index, key in enumerate(_SEARCH_PRIORITY)}
    fallback = {key: index for index, key in enumerate(_platform_order())}
    return sorted(
        keys,
        key=lambda key: (
            priority.get(key, len(priority) + fallback.get(key, len(fallback))),
            fallback.get(key, 0),
        ),
    )


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
        # 下载回退用：主来源失败时按顺序尝试同曲的其它平台。挂在对象上而
        # 不是 dataclass 字段，序列化（asdict / JSON）不会带出去，只有进程内
        # 的原始 Track（含 trackcache 里的对象）能读到。
        primary.__dict__["_alternatives"] = [pair[1] for pair in rest]
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
    workers: int = MAX_SEARCH_THREADS,
    on_update: Callable[[AggregateResult], None] | None = None,
    skip_failed: bool | None = None,
) -> AggregateResult:
    """
    并发搜索多个平台并合并结果。

    ``platforms=None`` 搜索全部已注册平台。 单个平台的异常（缺依赖、
    网络不可达、解析失败）只记为该项的 ``error``，不影响其它平台。
    聚合模式下最近失败过的平台会被跳过（:data:`FAILURE_TTL`），所以连续
    搜索时不会每次都为一排被墙的站等满预算。显式指定 ``platforms`` 时
    不跳过，方便用户手动重试。

    ``on_update`` 每完成一个平台就收到一次当前快照，界面可以先把结果画
    出来，而不是等所有平台都结束。

    :param keyword: 搜索词。
    :param platforms: 平台 key 列表；``None`` 为全部。
    :param limit: 合并后最多返回多少首。
    :param per_platform_limit: 每个平台取多少条原始结果。
    :param timeout: 整体墙钟预算（秒）；超时的平台按失败上报。
    :param workers: 并发线程上限；每个平台一个线程，避免队列饿死。
    :param on_update: 渐进式回调，收到 ``(部分结果)``。
    :param skip_failed: 是否跳过熔断中的平台；``None`` 表示仅在聚合模式跳过。
    :return: :class:`AggregateResult`。
    """
    text = str(keyword or "").strip()
    if not text:
        return AggregateResult()

    keys = _select_platforms(platforms)
    if not keys:
        return AggregateResult()
    explicit = platforms is not None
    skip = (not explicit) if skip_failed is None else bool(skip_failed)
    if skip:
        keys = [key for key in keys if not _breaker_open(key)]
        if not keys:
            # 所有平台都在熔断期：给用户一次完整的重试机会，而不是直接空结果。
            clear_failure_cache()
            keys = _select_platforms(platforms)
    keys = _ordered_keys(keys)

    per_platform = max(1, int(per_platform_limit))
    deadline_seconds = max(0.1, float(timeout))
    # 平台自己的请求超时要比整体预算短：到点后后台请求很快自行结束。
    read_timeout = max(1.0, min(4.0, deadline_seconds - 0.5))

    def run_one(key: str) -> tuple[list[Track], int]:
        try:
            # 单请求 4s、最多重试一次：vendor 的默认重试次数会把 4s 放大成
            # 十几秒，整次聚合搜索就会被一个慢站拖满预算。
            provider = create(key, timeout=(2.5, read_timeout), max_retries=1)
        except TypeError:
            provider = create(key)
        tracks, total = provider.search(text, limit=per_platform)
        return list(tracks or ()), int(total or len(tracks or ()))

    state_lock = threading.Lock()
    finished = threading.Event()
    results: dict[str, tuple[list[Track], int] | BaseException] = {}
    timeouts: set[str] = set()
    first_hit: list[float] = []
    closed = [False]
    early_stop = [False]
    early_timer: list[threading.Timer] = []

    def end_early() -> None:
        """Stop waiting for the slow tail once a usable batch is on screen."""
        with state_lock:
            if closed[0]:
                return
            early_stop[0] = True
            finished.set()

    def snapshot() -> AggregateResult:
        """Merge whatever has arrived so far (caller must hold no lock)."""
        with state_lock:
            completed = dict(results)
        outcomes: list[PlatformOutcome] = []
        groups: list[tuple[str, list[Track]]] = []
        for key in keys:
            outcome = completed.get(key)
            if outcome is None:
                continue
            if isinstance(outcome, BaseException):
                outcomes.append(
                    PlatformOutcome(key, False, error=_short_error(outcome))
                )
                continue
            tracks, _total = outcome
            outcomes.append(PlatformOutcome(key, True, count=len(tracks)))
            if tracks:
                groups.append((key, tracks))
        merged = merge_tracks(
            sorted(groups, key=lambda pair: _platform_position(pair[0])),
            text,
            limit=limit,
        )
        return AggregateResult(tracks=merged, platforms=outcomes, total=len(merged))

    def record(
        key: str, outcome: tuple[list[Track], int] | BaseException, elapsed_ms: int
    ) -> None:
        with state_lock:
            if closed[0]:
                return
            results[key] = outcome
            if not isinstance(outcome, BaseException) and outcome[0] and not first_hit:
                first_hit.append(time.monotonic())
                # Start the grace clock now instead of waiting for the next
                # platform to finish: the tail may take the whole budget.
                timer = threading.Timer(EARLY_EXIT_GRACE, end_early)
                timer.daemon = True
                timer.start()
                early_timer.append(timer)
            done = len(results) >= len(keys)
            if done:
                finished.set()
        if isinstance(outcome, BaseException):
            _record_failure(key, elapsed_ms)
        else:
            _record_success(key, elapsed_ms, len(outcome[0]))
        if on_update is not None:
            try:
                on_update(snapshot())
            except Exception:  # noqa: BLE001 - 界面回调失败不影响搜索
                logger.debug("music aggregate: on_update failed", exc_info=True)

    def worker(key: str) -> None:
        started = time.monotonic()
        try:
            outcome: tuple[list[Track], int] | BaseException = run_one(key)
        except BaseException as exc:  # noqa: BLE001 - 单平台失败不影响全局
            outcome = exc
        record(key, outcome, int((time.monotonic() - started) * 1000))

    threads = [
        threading.Thread(
            target=worker, args=(key,), name=f"music-search-{key}", daemon=True
        )
        for key in keys[: max(1, int(workers))]
    ]
    for thread in threads:
        thread.start()
    # 到点就返回：还在跑的平台由 daemon 线程自行结束，进程退出不会被它们
    # 拦住；它们的结果也不会再被使用。
    finished.wait(deadline_seconds)

    for timer in early_timer:
        timer.cancel()

    with state_lock:
        closed[0] = True
        missing = [key for key in keys if results.get(key) is None]
        if early_stop[0]:
            # The tail is simply omitted: not a success, not a failure, and
            # not recorded in the breaker (it may have been mid-request).
            pass
        else:
            for key in missing:
                timeouts.add(key)
                results[key] = TimeoutError(f"搜索超时（{deadline_seconds:g}s）")
        _save_stats()

    final = snapshot()
    return AggregateResult(
        tracks=final.tracks, platforms=final.platforms, total=final.total
    )


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
        # Keep "all platforms" as ``None`` in the child: passing an explicit
        # list would disable the failure breaker, undoing the whole point of
        # the persisted platform stats.
        "platforms": platforms,
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
