"""音乐提供方（Provider）抽象层。

v2.0 关键决策：
- 用统一的 Track 结构屏蔽各平台字段差异，界面与下载器只认 Track
- Provider 只负责「搜索」「解析直链」「取歌词」，下载落盘统一复用 downloader.py
- 注册表按 key 惰性导入：重依赖平台（FFmpeg / DRM 等）只在被选中时才加载
"""

import importlib.util
import shutil
from dataclasses import dataclass, field, replace
from typing import Any


@dataclass
class Track:
    """统一曲目结构，所有字段平台无关。"""

    provider: str = ""  # 提供方 key，如 "netease"
    id: str = ""  # 平台内唯一 id
    name: str = "未知歌曲"
    artists: str = "未知歌手"
    album: str = "未知专辑"
    duration_s: int = 0
    cover_url: str = ""
    track_no: int = 0
    ext: str = ""  # 期望扩展名（可空，解析直链时再定）
    vip: bool = False  # 是否为会员/付费曲目（仅用于界面标注）
    # 以下字段在 provider.resolve() 后填充
    download_url: str = ""
    http_headers: dict = field(default_factory=dict)  # 下载该直链所需的额外请求头
    path: str = ""  # 本地落盘位置（下载成功后由 downloader 回填）
    lyric: str = ""  # 已获取的歌词（可空）
    # 聚合搜索命中的平台 key 列表（最佳来源在前）；单平台搜索为空列表。
    sources: list[str] = field(default_factory=list)
    # provider 私有数据（如网易云原始 dict），仅供该 provider 自己使用
    extra: dict = field(default_factory=dict)

    def display(self):
        return f"{self.name} - {self.artists}"


@dataclass
class ResolveResult:
    """直链解析结果。url 为空表示失败，reason 说明原因。"""

    url: str = ""
    ext: str = ""
    reason: str = ""
    retryable: bool = False  # 传输类失败可重试；权限类失败不可
    level: str = ""  # 实际命中的音质档位（各平台可空）
    http_headers: dict = field(default_factory=dict)

    @property
    def ok(self):
        return bool(self.url)


# ---------------------------------------------------------------------------
# 平台能力描述
# ---------------------------------------------------------------------------

#: 下载方式：
#:
#: * ``direct``      解析出普通 HTTP 直链，公共下载器可直接落盘
#: * ``external``    需要 CDM / 外部工具（ffmpeg、N_m3u8DL-RE）或额外解密流程
#: * ``third_party`` 依赖第三方解析服务，不是平台官方直链
#: * ``preview``     无账号/CDM 时只能拿到试听片段
#: * ``none``        不提供下载
DOWNLOAD_MODES = frozenset({"direct", "external", "third_party", "preview", "none"})

#: 前置条件类别：Python 包 / 外部可执行文件 / 用户自备 CDM。
REQUIREMENT_KINDS = frozenset({"python", "tool", "cdm"})

#: 稳定的限制代码（展示层自行翻译，不把中文写进机器可读字段）。
LIMITATIONS = frozenset(
    {
        "drm_protected",
        "hls_stream",
        "encrypted_segments",
        "requires_cdm",
        "requires_credentials",
        "requires_external_tool",
        "third_party_service",
        "region_restricted",
        "preview_only_without_credentials",
        "quality_limited_without_credentials",
        "playlists_need_credentials",
    }
)

#: ``PlatformCapability.status`` 的取值，按优先级从高到低。
CAPABILITY_READY = "ready"
CAPABILITY_MISSING_DEPENDENCY = "missing_dependency"
CAPABILITY_MISSING_TOOL = "missing_tool"
CAPABILITY_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Requirement:
    """平台的一项前置条件。

    探测只使用 ``importlib.util.find_spec`` / ``shutil.which``，绝不真的
    import 重型包：列平台、渲染状态页都不应该加载 pywidevine / Node 客户端。
    ``satisfied`` 由注册表构建时填入，之后作为快照保持稳定；``None`` 表示
    无法自动探测（如需要用户自备的 Widevine ``.wvd``）。
    """

    kind: str  # "python" | "tool" | "cdm"
    name: str
    optional: bool = False
    install_hint: str = ""
    satisfied: bool | None = None

    def probe(self) -> bool | None:
        """在不导入包的前提下检查该前置条件是否满足。"""
        if self.kind == "python":
            try:
                return importlib.util.find_spec(self.name) is not None
            except (ImportError, ValueError):  # 例如 sys.modules[name] = None
                return False
        if self.kind == "tool":
            return shutil.which(self.name) is not None
        return None  # CDM 只能由用户提供，无法据此判断

    def with_state(self, state: bool | None) -> "Requirement":
        return replace(self, satisfied=state)

    def as_dict(self) -> dict[str, Any]:
        reason = ""
        if self.satisfied is False:
            if self.kind == "python":
                reason = f"缺少 Python 依赖：{self.name}"
            elif self.kind == "tool":
                reason = f"缺少外部工具：{self.name}"
            else:
                reason = f"缺少前置条件：{self.name}"
        return {
            "kind": self.kind,
            "name": self.name,
            "optional": self.optional,
            "satisfied": self.satisfied,
            "install_hint": self.install_hint,
            "reason": reason,
        }


@dataclass(frozen=True)
class PlatformCapability:
    """一个平台能力的稳定、可 JSON 序列化描述。

    由 ``providers/__init__.py`` 的注册表统一生成，CLI / REST / 内嵌引擎 /
    桌面端都读这同一份数据，避免各端各写一套「这个平台要不要账号、要不要
    CDM」的判断。
    """

    key: str
    label: str
    group: str = ""
    search: bool = True
    download: bool = True
    download_mode: str = "direct"
    lyrics: bool = True
    login_required: bool = False
    credential_fields: tuple[str, ...] = ()
    credential_required: bool = False
    requirements: tuple[Requirement, ...] = ()
    limitations: tuple[str, ...] = ()
    note: str = ""
    registered: bool = False
    status: str = CAPABILITY_READY
    missing: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """JSON 友好形态（元组转列表，不暴露任何凭据值）。"""
        return {
            "key": self.key,
            "label": self.label,
            "group": self.group,
            "search": self.search,
            "download": self.download,
            "download_mode": self.download_mode,
            "lyrics": self.lyrics,
            "login_required": self.login_required,
            "credential_fields": list(self.credential_fields),
            "credential_required": self.credential_required,
            "requirements": [item.as_dict() for item in self.requirements],
            "limitations": list(self.limitations),
            "note": self.note,
            "registered": self.registered,
            "status": self.status,
            "missing": list(self.missing),
        }


class MusicProvider:
    """所有平台的统一接口。子类至少实现 search 与 resolve。"""

    key = ""  # 唯一标识，如 "netease"
    display_name = ""  # 界面显示名
    requires_login = False
    supports_playlists = False
    #: Whether ``search(offset=...)`` actually skips hits. Search responses
    #: expose this so a "load more" control can stop instead of receiving a
    #: silently empty page from a provider that only ever returns page one.
    supports_offset = True

    # ---------- 搜索 / 解析 ----------

    def search(
        self, keyword: str, limit: int = 30, offset: int = 0
    ) -> tuple[list[Track], int]:
        """返回 (tracks, total)。total 为平台侧总命中数，未知时给 len(tracks)。"""
        raise NotImplementedError

    def resolve(self, track, level=None):
        """把 Track 解析成可下载直链，返回 ResolveResult。"""
        raise NotImplementedError

    def lyric(self, track):
        """返回 (原文, 翻译) LRC 文本；不支持则返回 ("", "")。"""
        return "", ""

    # ---------- 登录 / 账号（按需覆写） ----------

    def is_logged_in(self):
        return True

    def account(self):
        return None, None

    def login_by_cookie(self, text):
        raise NotImplementedError

    def clear_cookies(self):
        pass

    @property
    def cookie_names(self):
        return []

    # ---------- 歌单（按需覆写） ----------

    def user_playlists(self, uid):
        return []

    def playlist_tracks(self, pid):
        return []
