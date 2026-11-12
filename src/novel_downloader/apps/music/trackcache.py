"""搜索结果短时缓存：让下载端拿回原始 Track 对象。

音乐平台的直链解析依赖只存在于进程内的数据（musicdl 的 ``SongInfo``
对象、网易云的原始字段）。 搜索结果一旦经 JSON 往返——桌面端走
pywebview 桥、REST / 内嵌引擎走 HTTP——这些数据就退化成普通 dict，
``provider.resolve()`` 会失败。

所以搜索时把原始 Track 留在进程内，响应里带一个 ``search_id``；下载
请求只要回传 ``search_id``，服务端就能按顺序取回原始对象。缓存有上限
和 TTL，进程重启或过期后按「重新搜索」处理（调用方拿到明确的失败
原因，而不是 AttributeError）。
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Sequence

from .providers.base import Track

__all__ = ["CACHE", "TrackCache"]

#: 一个缓存条目最多留多久（秒）。足够用户挑歌，又不会无限占内存。
DEFAULT_TTL = 30 * 60.0

#: 同时保留多少份搜索结果（最近使用的在前）。
DEFAULT_MAX_ENTRIES = 32

#: 生成 id 时最多重试几次。24 位十六进制的碰撞率可以忽略，但“可以忽略”
#: 不等于“不会”：真撞上时静默覆盖会把另一份搜索结果换成这次的对象，
#: 回传的旧 ``search_id`` 会解析出错误的 Track。重试几次后改用 uuid 兜底。
_ID_ATTEMPTS = 8


class TrackCache:
    """有界的 ``search_id -> list[Track]`` 缓存（线程安全，LRU 淘汰）。"""

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl: float = DEFAULT_TTL,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._entries: OrderedDict[str, tuple[float, list[Track]]] = OrderedDict()
        self._lock = threading.Lock()
        self._max_entries = max(1, int(max_entries))
        self._ttl = max(0.0, float(ttl))
        self._clock = clock
        self._fallback_counter = 0

    def put(self, tracks: Sequence[Track]) -> str:
        """保存一次搜索的曲目，返回新的、不会与现存条目冲突的 ``search_id``。"""
        now = self._clock()
        with self._lock:
            self._prune(now)
            search_id = self._fresh_id()
            self._entries[search_id] = (now, list(tracks))
            self._entries.move_to_end(search_id)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        return search_id

    def _fresh_id(self) -> str:
        """调用方必须持有 ``_lock``：检查与插入之间不能有别的 ``put``。"""
        for _ in range(_ID_ATTEMPTS):
            candidate = secrets.token_hex(12)
            if candidate not in self._entries:
                return candidate
        # token_hex 被替换/熵源异常时仍要给出唯一 id，而不是覆盖活条目。
        # 计数器保证即使 uuid 也被替换成常量，长度不同的兜底 id 仍不重复。
        self._fallback_counter += 1
        return f"{uuid.uuid4().hex}{self._fallback_counter:x}"

    def get(self, search_id: str | None) -> list[Track] | None:
        """按 ``search_id`` 取回曲目；未知或过期返回 ``None``。"""
        if not search_id:
            return None
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(str(search_id))
            if entry is None:
                return None
            # 命中即刷新使用顺序（LRU），但不延长单条 TTL：TTL 从搜索
            # 那一刻起算，避免长期打开的面板一直拿着旧结果。
            self._entries.move_to_end(str(search_id))
            return list(entry[1])

    def resolve(self, search_id: str | None, submitted: Sequence[Track]) -> list[Track]:
        """
        用缓存里的原始对象替换客户端回传的序列化副本。

        优先按位置替换（长度一致且 provider/id 对得上）；否则按
        ``(provider, id)`` 匹配。缓存缺失时原样返回 ``submitted``——
        调用方随后会得到 provider 自己的「缺少解析数据」提示。
        """
        cached = self.get(search_id)
        if not cached:
            return list(submitted)

        if len(cached) == len(submitted):
            aligned = all(
                (not item.provider and not item.id)
                or (item.provider == original.provider and item.id == original.id)
                for item, original in zip(submitted, cached, strict=True)
            )
            if aligned:
                return cached

        by_key = {
            (track.provider, track.id): track
            for track in cached
            if track.provider or track.id
        }
        resolved: list[Track] = []
        for item in submitted:
            resolved.append(by_key.get((item.provider, item.id), item))
        return resolved

    def clear(self) -> None:
        """丢弃全部缓存（测试与退出时使用）。"""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            self._prune(self._clock())
            return len(self._entries)

    def _prune(self, now: float) -> None:
        if not self._entries:
            return
        expired = [
            key
            for key, (stamp, _tracks) in self._entries.items()
            if self._ttl <= 0 or now - stamp > self._ttl
        ]
        for key in expired:
            self._entries.pop(key, None)


#: 进程级共享缓存：REST、内嵌引擎与桌面桥共用同一份实现。
CACHE = TrackCache()
