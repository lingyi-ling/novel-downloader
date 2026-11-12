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
import math
import multiprocessing
import os
import pickle
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, cast

from .providers import available, create
from .providers.base import Track
from .serialize import strip_url_query

logger = logging.getLogger(__name__)

__all__ = [
    "ALL_PLATFORM_KEY",
    "AggregateResult",
    "MAX_PER_PLATFORM_LIMIT",
    "PlatformOutcome",
    "is_all_platforms",
    "merge_tracks",
    "normalize_text",
    "per_platform_limit_for",
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

#: 聚合搜索时单个平台最多取多少条。对外 ``limit`` 已经封顶，这里再限一次：
#: 平台越多，「每平台条数」带来的调用量增长越快，而上限对一个聚合列表
#: 已经足够（去重后通常远超 20 条）。
MAX_PER_PLATFORM_LIMIT = 15

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
_FAILURE_LOCK = threading.RLock()

#: 统计文件本该只有几十个平台条目；比这还大说明文件已损坏（或被别的程序
#: 写坏），整份读取会在 ``json.load`` 里吃掉任意内存。
_STATS_MAX_BYTES = 256 * 1024
_STATS_MAX_ENTRIES = 128
_STATS_MAX_KEY_CHARS = 64
_STATS_MAX_MS = 24 * 60 * 60 * 1000
#: ``failed_until`` 最多允许比当前时间晚这么多秒。合法值是 now + 600，
#: 超过这个余量只能是损坏数据或时钟大幅回拨；不截断的话一个 NaN/超大数
#: 就能把平台永久拉黑（NaN 的比较永远为 False，熔断器再也关不上）。
_STATS_FUTURE_SLACK = 3600.0

#: 最近一次读取的磁盘状态。``_STATS_WRITABLE`` 为 False 表示文件存在但
#: 读不出来（共享冲突、权限、目录被占用……）：此时内存里的空统计并不是
#: 磁盘内容，写回去会毁掉一份本来完好的熔断记录，所以先不写，等后续某次
#: 读取成功再合并落盘。
_STATS_WRITABLE = True
_STATS_SIG: tuple[int, int] | None = None


def _now() -> float:
    """熔断截止时间用的墙钟（独立函数，便于测试替换成确定性的时钟）。"""
    return time.time()


class _SearchClock:
    """可注入的计时/等待接缝，供搜索预算与「提前收工」宽限使用。

    生产环境用真实时间；测试替换 :data:`_clock` 后，预算到期与宽限触发
    由显式事件驱动，不再依赖线程恰好何时被调度，也不需要用 sleep 制造
    时间窗口。
    """

    def monotonic(self) -> float:
        return time.monotonic()

    def wait(self, event: threading.Event, timeout: float) -> bool:
        return event.wait(timeout)

    def schedule(self, delay: float, callback: Callable[[], None]) -> threading.Timer:
        timer = threading.Timer(delay, callback)
        timer.daemon = True
        timer.start()
        return timer


#: 单测会替换成确定性实现；模块内所有预算/宽限计时都走这里。
_clock = _SearchClock()


def _stats_file() -> Path:
    from .paths import music_data_dir

    return music_data_dir() / "search_stats.json"


def _stats_path() -> Path | None:
    """统计文件路径；数据目录不可用时返回 ``None``（统计只是最佳努力）。"""
    try:
        return _stats_file()
    except Exception:  # noqa: BLE001 - 路径/目录错误不能拖垮搜索
        logger.debug("music aggregate: music data dir unavailable", exc_info=True)
        return None


def _finite_float(value: object, default: float = 0.0) -> float:
    """把任意 JSON 值转成有限浮点；NaN/Infinity/垃圾类型返回默认值。"""
    if isinstance(value, bool):
        return float(value)
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _bounded_int(value: object, default: int = 0, *, maximum: int) -> int:
    """把任意值转成 ``0..maximum`` 的整数。"""
    try:
        if isinstance(value, bool):
            number = int(value)
        elif isinstance(value, float):
            if not math.isfinite(value):
                return default
            number = int(value)
        else:
            number = int(cast(Any, value))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, min(number, maximum))


def _coerce_stats_entry(value: object, now: float) -> dict[str, Any] | None:
    """规范化一条平台记录；字段类型错误的结果是“按默认值”，不是整份丢弃。"""
    if not isinstance(value, dict):
        return None
    until = max(0.0, _finite_float(value.get("failed_until")))
    if until > now + _STATS_FUTURE_SLACK:
        until = 0.0
    entry: dict[str, Any] = {
        "ok": value.get("ok") is True,
        "last_ms": _bounded_int(value.get("last_ms"), 0, maximum=_STATS_MAX_MS),
        "failed_until": until,
    }
    streak = _bounded_int(value.get("empty_streak"), 0, maximum=SLOW_EMPTY_STREAK * 10)
    if streak:
        entry["empty_streak"] = streak
    return entry


def _file_signature(path: Path) -> tuple[int, int] | None:
    """``(mtime_ns, size)``，用于判断有没有别的进程改过文件。"""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _read_stats_file(
    path: Path,
) -> tuple[dict[str, dict[str, Any]], bool, tuple[int, int] | None]:
    """
    读取并规范化统计文件。

    :return: ``(entries, readable, signature)``。``readable`` 只在“文件存在
        但读不出来”时为 False——调用方此时绝不能覆盖它。文件缺失、内容损坏
        或超大都返回空表 + ``readable=True``：这些都是可以安全重写的垃圾。
    """
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {}, True, None
    except OSError:
        return {}, False, None

    signature = (stat.st_mtime_ns, stat.st_size)
    if stat.st_size > _STATS_MAX_BYTES:
        logger.warning(
            "music aggregate: ignoring oversized search stats (%d bytes)", stat.st_size
        )
        return {}, True, signature

    try:
        # Read at most the cap + 1: the file can grow between stat() and open().
        with path.open("rb") as fh:
            blob = fh.read(_STATS_MAX_BYTES + 1)
    except OSError:
        return {}, False, None
    if len(blob) > _STATS_MAX_BYTES:
        return {}, True, signature

    try:
        raw = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.warning("music aggregate: ignoring malformed search stats")
        return {}, True, signature

    platforms = raw.get("platforms") if isinstance(raw, dict) else None
    entries: dict[str, dict[str, Any]] = {}
    if isinstance(platforms, dict):
        now = _now()
        for key, value in platforms.items():
            if len(entries) >= _STATS_MAX_ENTRIES:
                break
            name = str(key).strip()
            if not name or len(name) > _STATS_MAX_KEY_CHARS:
                continue
            entry = _coerce_stats_entry(value, now)
            if entry is not None:
                entries[name] = entry
    return entries, True, signature


def _load_stats() -> dict[str, dict[str, Any]]:
    """载入并缓存平台统计；任何失败都 fail-open 到空统计。"""
    global _STATS, _STATS_SIG, _STATS_WRITABLE
    with _FAILURE_LOCK:
        if _STATS is not None:
            return _STATS
        path = _stats_path()
        if path is None:
            _STATS, _STATS_WRITABLE, _STATS_SIG = {}, False, None
            return _STATS
        try:
            data, readable, signature = _read_stats_file(path)
        except Exception:  # noqa: BLE001 - 解析/文件系统怪异行为都不能上抛
            logger.debug("music aggregate: could not read search stats", exc_info=True)
            data, readable, signature = {}, False, None
        _STATS = data
        _STATS_WRITABLE = readable
        _STATS_SIG = signature
        return _STATS


def _merge_stats(
    disk: dict[str, dict[str, Any]], memory: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """合并另一个进程写入的记录；同名条目以本进程的观察为准。"""
    merged: dict[str, dict[str, Any]] = {}
    for key, entry in memory.items():
        if len(merged) >= _STATS_MAX_ENTRIES:
            break
        merged[key] = dict(entry)
    for key, entry in disk.items():
        if len(merged) >= _STATS_MAX_ENTRIES:
            break
        if key not in merged:
            merged[key] = dict(entry)
    return merged


def _save_stats(*, merge: bool = True) -> None:
    """Best-effort atomic write; never let stats break a search.

    The whole snapshot + write happens under ``_FAILURE_LOCK``: concurrent
    ``_record_failure`` / ``_record_success`` calls mutate the same nested
    dicts, and serialising them while a key is being added used to raise
    ``RuntimeError: dictionary changed size during iteration`` (not caught,
    so it escaped into ``search_all``). Each save also uses a per-process,
    per-thread temp file: two searches (or a forked worker) saving at the same
    time must never interleave bytes in one ``.tmp`` and then ``os.replace``
    the garbage over the real file.

    When the file on disk changed since it was loaded (another process saved),
    its entries are merged in first: the atomic replace still makes the last
    writer win for the same platform, but platforms recorded by the other
    process are not silently dropped. ``merge=False`` is used by
    :func:`clear_failure_cache`, which must actually erase the file.
    """
    global _STATS, _STATS_SIG, _STATS_WRITABLE
    target = _stats_path()
    if target is None:
        return
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with _FAILURE_LOCK:
            data = _load_stats()
            if not _STATS_WRITABLE:
                # The file exists but could not be read: its contents may be
                # perfectly good, so never replace it with this partial view.
                disk, readable, signature = _read_stats_file(target)
                if not readable:
                    logger.debug(
                        "music aggregate: stats unreadable; keeping them untouched"
                    )
                    return
                _STATS_WRITABLE = True
                _STATS_SIG = signature
                data = _merge_stats(disk, data) if merge else data
                _STATS = data
            elif merge:
                signature = _file_signature(target)
                if signature != _STATS_SIG:
                    disk, readable, fresh_sig = _read_stats_file(target)
                    if readable:
                        data = _merge_stats(disk, data)
                        _STATS = data
                        _STATS_SIG = fresh_sig
            # Shallow-copied mapping still shares the inner dicts; copy them
            # too so the payload is a true snapshot.
            payload = {
                "version": 1,
                "platforms": {key: dict(entry) for key, entry in data.items()},
            }
            with tmp.open("w", encoding="utf-8", newline="\n") as fh:
                json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, target)
            _STATS_SIG = _file_signature(target)
    except Exception:  # noqa: BLE001 - stats must never break a search
        logger.debug("music aggregate: could not persist search stats", exc_info=True)
        with contextlib.suppress(OSError):
            tmp.unlink()


def _breaker_open(key: str, now: float | None = None) -> bool:
    """该平台是否在熔断期（失败或连续「慢且空」）。"""
    stamp = _now() if now is None else now
    with _FAILURE_LOCK:
        entry = _load_stats().get(key)
        if entry is None:
            return False
        raw_until = entry.get("failed_until")
        until = _finite_float(raw_until)
        if until <= stamp:
            if raw_until:
                # The ban expired: clear it *and* the slow-empty counter, or
                # one more slow empty result would immediately re-open it and
                # the platform would never get a fresh observation window.
                entry["failed_until"] = 0.0
                entry.pop("empty_streak", None)
            return False
        return True


def _record_failure(key: str, elapsed_ms: int = 0) -> None:
    with _FAILURE_LOCK:
        entry = _load_stats().setdefault(key, {})
        entry.update(
            ok=False,
            last_ms=_bounded_int(elapsed_ms, 0, maximum=_STATS_MAX_MS),
            failed_until=_now() + FAILURE_TTL,
        )
        # A hard failure supersedes any half-built slow-empty streak: after the
        # ban expires the platform starts counting from zero again.
        entry.pop("empty_streak", None)


def _record_success(key: str, elapsed_ms: int = 0, count: int = 0) -> None:
    with _FAILURE_LOCK:
        entry = _load_stats().setdefault(key, {})
        elapsed = _bounded_int(elapsed_ms, 0, maximum=_STATS_MAX_MS)
        slow_empty = (
            elapsed >= SLOW_EMPTY_MS
            and _bounded_int(count, 0, maximum=_STATS_MAX_MS) == 0
        )
        streak = _bounded_int(
            entry.get("empty_streak"), 0, maximum=SLOW_EMPTY_STREAK * 10
        )
        streak = streak + 1 if slow_empty else 0
        entry.update(
            ok=True,
            last_ms=elapsed,
            empty_streak=streak,
            failed_until=0.0,
        )
        if streak >= SLOW_EMPTY_STREAK:
            entry["failed_until"] = _now() + SLOW_EMPTY_TTL
            logger.info(
                "music aggregate: %s keeps returning nothing slowly; skipping for %gs",
                key,
                SLOW_EMPTY_TTL,
            )


def clear_failure_cache() -> None:
    """清空平台熔断记录（健康检查成功或用户手动重试时使用）。"""
    global _STATS, _STATS_SIG, _STATS_WRITABLE
    with _FAILURE_LOCK:
        _STATS = {}
        _STATS_WRITABLE = True
        _STATS_SIG = None
    # merge=False: an explicit clear must wipe the file, even if another
    # process wrote entries after we loaded it.
    _save_stats(merge=False)


def failed_platforms() -> list[str]:
    """当前处于熔断期的平台（调试/界面提示用）。"""
    now = _now()
    with _FAILURE_LOCK:
        return [
            key
            for key, entry in _load_stats().items()
            if _finite_float(entry.get("failed_until")) > now
        ]


def _ordered_keys(keys: list[str]) -> list[str]:
    """把优先平台提到前面，其余保持注册表顺序；重复项只保留第一次。"""
    priority = {key: index for index, key in enumerate(_SEARCH_PRIORITY)}
    fallback = {key: index for index, key in enumerate(_platform_order())}
    unique: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if key not in seen:
            seen.add(key)
            unique.append(key)
    return sorted(
        unique,
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


def per_platform_limit_for(limit: int) -> int:
    """把聚合搜索请求的总 ``limit`` 换算成每平台取几条。

    至少要取 :data:`DEFAULT_PER_PLATFORM_LIMIT`（否则合并后可能不够一页），
    至多 :data:`MAX_PER_PLATFORM_LIMIT`（平台数量会放大这个值）。REST 与
    内嵌引擎共用这一个换算，避免两边的上限慢慢漂移。
    """
    try:
        wanted = int(limit)
    except (TypeError, ValueError):
        wanted = DEFAULT_PER_PLATFORM_LIMIT
    return max(DEFAULT_PER_PLATFORM_LIMIT, min(wanted, MAX_PER_PLATFORM_LIMIT))


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
        items = list(tracks or ())
        # A provider returning dicts/objects instead of Tracks must only lose
        # its own rows: merge_tracks mutates attributes and would otherwise
        # raise while building the snapshot, failing the whole aggregate
        # search for every platform.
        valid = [item for item in items if isinstance(item, Track)]
        if len(valid) != len(items):
            logger.warning(
                "music aggregate: %s returned %d malformed track(s); dropped",
                key,
                len(items) - len(valid),
            )
        return valid, int(total or len(valid))

    state_lock = threading.Lock()
    callback_lock = threading.Lock()
    finished = threading.Event()
    results: dict[str, tuple[list[Track], int] | BaseException] = {}
    #: Keys that were actually handed to a worker. A platform still sitting in
    #: ``pending`` when the deadline hits had no chance to answer, so it must
    #: not be penalised in the breaker (see the close block below).
    started: set[str] = set()
    timeouts: set[str] = set()
    first_hit: list[float] = []
    closed = [False]
    early_stop = [False]
    early_timer: list[threading.Timer] = []
    deadline_at = _clock.monotonic() + deadline_seconds

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
            limit=None,
        )
        # ``total`` is the number of distinct songs before ``limit`` truncates
        # the returned rows (this is what the UI shows as the match count).
        total = len(merged)
        if limit is not None:
            merged = merged[: max(0, int(limit))]
        return AggregateResult(tracks=merged, platforms=outcomes, total=total)

    def record(
        key: str, outcome: tuple[list[Track], int] | BaseException, elapsed_ms: int
    ) -> None:
        with state_lock:
            if closed[0]:
                return
            results[key] = outcome
            is_failure = isinstance(outcome, BaseException)
            # Record platform health *inside* this lock. The final
            # ``_save_stats()`` runs after ``closed`` is set; if a worker
            # published its result and then lost the race to update the stats,
            # the last success/failure of the search would never be persisted.
            try:
                if is_failure:
                    _record_failure(key, elapsed_ms)
                elif isinstance(outcome, tuple):
                    _record_success(key, elapsed_ms, len(outcome[0]))
            except Exception:  # noqa: BLE001 - stats must never break a search
                logger.debug("music aggregate: could not update stats", exc_info=True)
            if isinstance(outcome, tuple) and outcome[0] and not first_hit:
                first_hit.append(_clock.monotonic())
                # Start the grace clock now instead of waiting for the next
                # platform to finish: the tail may take the whole budget.
                early_timer.append(_clock.schedule(EARLY_EXIT_GRACE, end_early))
            done = len(results) >= len(keys)
            if done:
                finished.set()
        if on_update is not None:
            try:
                # Serialise callbacks and take the snapshot *inside* the lock:
                # several platform threads can finish at the same instant, and
                # delivering an older snapshot after a newer one would make the
                # UI's result list visibly shrink again.
                with callback_lock:
                    on_update(snapshot())
            except Exception:  # noqa: BLE001 - 界面回调失败不影响搜索
                logger.debug("music aggregate: on_update failed", exc_info=True)

    def worker(key: str) -> None:
        started_at = _clock.monotonic()
        try:
            outcome: tuple[list[Track], int] | BaseException = run_one(key)
        except BaseException as exc:  # noqa: BLE001 - 单平台失败不影响全局
            outcome = exc
        record(key, outcome, int((_clock.monotonic() - started_at) * 1000))

    # A bounded pool, not one thread per platform and not ``keys[:workers]``:
    # slicing silently dropped every platform after the Nth (it was never
    # started yet reported as timed out, or omitted entirely on early exit).
    pending = list(keys)

    def next_key() -> str | None:
        """Take the next platform, under the same lock that guards results."""
        with state_lock:
            if closed[0] or early_stop[0] or not pending:
                return None
            # Do not start new requests after the budget: their result could
            # never be delivered, and blaming them for the timeout would put a
            # platform that never ran into the 10-minute breaker.
            if _clock.monotonic() >= deadline_at:
                return None
            key = pending.pop(0)
            started.add(key)
            return key

    def pool_worker(first: str) -> None:
        worker(first)
        while (key := next_key()) is not None:
            worker(key)

    thread_count = max(1, min(int(workers), len(keys)))
    # Give every worker its first platform up front. Thread startup can take a
    # while on a loaded machine; a platform that was already assigned to a
    # worker must not be reclassified as "never started" just because the
    # thread had not been scheduled before the deadline.
    initial: list[str] = []
    with state_lock:
        while pending and len(initial) < thread_count:
            key = pending.pop(0)
            started.add(key)
            initial.append(key)
    threads = [
        threading.Thread(
            target=pool_worker,
            args=(initial[index],),
            name=f"music-search-{index}",
            daemon=True,
        )
        for index in range(thread_count)
    ]
    for thread in threads:
        thread.start()
    # 到点就返回：还在跑的平台由 daemon 线程自行结束，进程退出不会被它们
    # 拦住；它们的结果也不会再被使用。
    _clock.wait(finished, deadline_seconds)

    for timer in early_timer:
        timer.cancel()

    timed_out: list[str] = []
    with state_lock:
        closed[0] = True
        missing = [key for key in keys if results.get(key) is None]
        if early_stop[0]:
            # The tail is simply omitted: not a success, not a failure, and
            # not recorded in the breaker (it may have been mid-request).
            pass
        else:
            for key in missing:
                if key not in started:
                    # The pool never picked it up inside the budget: it had no
                    # chance to answer, so it is neither a success nor a
                    # failure (same rule as the early-exit tail). Reporting it
                    # as a timeout used to blacklist an entire unsearched tail.
                    continue
                timeouts.add(key)
                timed_out.append(key)
                results[key] = TimeoutError(f"搜索超时（{deadline_seconds:g}s）")

    # A budget timeout *is* a failure: the platform never answered inside the
    # window, so the next aggregate search should not pay the same budget for
    # it again. Early-stopped and never-started platforms are deliberately
    # excluded above -- they may have been mid-request or never ran at all.
    timeout_ms = int(deadline_seconds * 1000)
    for key in timed_out:
        _record_failure(key, timeout_ms)
    _save_stats()

    # Take the final snapshot under ``callback_lock``: an in-flight callback
    # merges the same Track objects (``sources``/``_alternatives`` are mutated
    # in place), and a callback queued behind it must not deliver a snapshot
    # older than the returned result. Waiting here also guarantees no late
    # callback can run after ``search_all`` returns.
    with callback_lock:
        final = snapshot()
    return AggregateResult(
        tracks=final.tracks, platforms=final.platforms, total=final.total
    )


def _short_error(exc: BaseException) -> str:
    """把平台异常压成一行可读原因（完整堆栈由调用方按需记录）。"""
    try:
        message = str(exc).strip()
    except Exception:  # noqa: BLE001 - 自定义异常可能带坏掉的 __str__
        message = ""
    message = message or type(exc).__name__
    # Strip signed query strings *before* truncating, or a partially cut URL
    # could still leak most of a token.
    return strip_url_query(message)[:200]


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
        # Pass ``platforms`` unchanged: substituting the resolved ``keys``
        # would turn "all platforms" into an explicit list and disable the
        # failure breaker for the inline fallback.
        return search_all(
            text,
            platforms,
            limit,
            per_platform_limit=per_platform_limit,
            timeout=timeout,
        )
    child_conn.close()

    payload: Any = None
    try:
        # 子进程内部还有自己的 timeout；这里只等「启动 + 搜索 + 回传」。
        deadline = timeout + max(1.0, float(startup_grace))
        if parent_conn.poll(deadline):
            payload = parent_conn.recv()
    except (EOFError, OSError, pickle.PickleError):
        # A worker killed mid-send leaves a truncated pickle in the pipe.
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
    if not isinstance(payload, tuple) or len(payload) != 2:
        return AggregateResult(
            platforms=[
                PlatformOutcome(key, False, error="搜索进程返回了无效结果")
                for key in keys
            ]
        )
    status, value = payload
    if status == "ok" and isinstance(value, AggregateResult):
        return value
    message = strip_url_query(str(value or "搜索进程失败"))[:200]
    return AggregateResult(
        platforms=[PlatformOutcome(key, False, error=message) for key in keys]
    )
