"""音乐提供方（Provider）抽象层。

v2.0 关键决策：
- 用统一的 Track 结构屏蔽各平台字段差异，界面与下载器只认 Track
- Provider 只负责「搜索」「解析直链」「取歌词」，下载落盘统一复用 downloader.py
- 注册表按 key 惰性导入：重依赖平台（FFmpeg / DRM 等）只在被选中时才加载
"""

from dataclasses import dataclass, field


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


class MusicProvider:
    """所有平台的统一接口。子类至少实现 search 与 resolve。"""

    key = ""  # 唯一标识，如 "netease"
    display_name = ""  # 界面显示名
    requires_login = False
    supports_playlists = False

    # ---------- 搜索 / 解析 ----------

    def search(self, keyword, limit=30, offset=0):
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
