"""musicdl 平台适配器：把 vendored 的 musicdl 客户端统一到我们的 MusicProvider。

v2.0 关键决策：
- 复用 musicdl 的 BaseMusicClient.search()（含 URL 拼接、多线程、直链有效性校验），
  不自己重写各平台协议；搜索时即解析好直链，download 阶段零额外请求
- 传一个空 Progress 替身，避免 vendored 库往终端刷进度条
- musicdl 无分页 offset 概念，「加载更多」对这些平台不适用（返回空）
- HLS(m3u8) 流暂不支持直链下载，明确给出原因而不是产出坏文件
"""

import importlib
import importlib.util
import os
import shutil
import tempfile

from .base import MusicProvider, ResolveResult, Track
from .creds import get as get_cred

# 平台 key -> (显示名, 模块路径, 类名)
# 模块路径指向 vendored 的实现，惰性 import：选中哪个平台才加载哪个
_BASE = "novel_downloader.apps.music.providers._vendor.musicdl.modules.sources."
PLATFORMS = {
    "migu": ("咪咕音乐", _BASE + "migu", "MiguMusicClient"),
    "kuwo": ("酷我音乐", _BASE + "kuwo", "KuwoMusicClient"),
    "kugou": ("酷狗音乐", _BASE + "kugou", "KugouMusicClient"),
    "qianqian": ("千千音乐", _BASE + "qianqian", "QianqianMusicClient"),
    "fivesing": ("5sing", _BASE + "fivesing", "FiveSingMusicClient"),
    "bilibili": ("哔哩哔哩", _BASE + "bilibili", "BilibiliMusicClient"),
    "deezer": ("Deezer", _BASE + "deezer", "DeezerMusicClient"),
    "jamendo": ("Jamendo", _BASE + "jamendo", "JamendoMusicClient"),
    "audius": ("Audius", _BASE + "audius", "AudiusMusicClient"),
    "streetvoice": ("StreetVoice", _BASE + "streetvoice", "StreetVoiceMusicClient"),
    "jiosaavn": ("JioSaavn", _BASE + "jiosaavn", "JioSaavnMusicClient"),
    "fma": ("Free Music Archive", _BASE + "fma", "FMAMusicClient"),
    "ccmixter": ("ccMixter", _BASE + "ccmixter", "CCMixterMusicClient"),
    "opengameart": ("OpenGameArt", _BASE + "opengameart", "OpenGameArtMusicClient"),
    "wikimediacommons": (
        "Wikimedia Commons",
        _BASE + "wikimediacommons",
        "WikimediaCommonsMusicClient",
    ),
    "suno": ("Suno", _BASE + "suno", "SunoMusicClient"),
    "bodian": ("波点音乐", _BASE + "bodian", "BodianMusicClient"),
    "moov": ("MOOV", _BASE + "moov", "MOOVMusicClient"),
    "joox": ("JOOX", _BASE + "joox", "JooxMusicClient"),
    # 「中」档：鉴权较多，搜索/下载可能依赖平台账号 Cookie
    "qq": ("QQ音乐", _BASE + "qq", "QQMusicClient"),
    "qobuz": ("Qobuz", _BASE + "qobuz", "QobuzMusicClient"),
    "spotify": ("Spotify", _BASE + "spotify", "SpotifyMusicClient"),
    "tidal": ("TIDAL", _BASE + "tidal", "TIDALMusicClient"),
    "soda": ("汽水音乐", _BASE + "soda", "SodaMusicClient"),
}

# 需要额外条件（账号凭据 / 可访问性）才能出结果的平台 -> 界面提示语
PLATFORM_NOTES = {
    "qobuz": "Qobuz 需能访问 open.qobuz.com；当前网络不可达时会搜不到结果。",
    "soda": (
        "汽水音乐需配置设备 Cookie（点「平台凭据」粘贴 "
        '{"cookies": {...}}），否则可能长时间无响应。'
    ),
    "deezer": "Deezer 需能访问 deezer.com；当前网络不可达时会报错。",
    "suno": "Suno 需能访问 suno.com；当前网络不可达时会报错。",
    "moov": "MOOV 需能访问 moov.hk；当前网络不可达时会超时。",
    "joox": "JOOX 需能访问 joox.com；当前网络不可达时会超时。",
    "tidal": (
        "TIDAL 需在本平台配置账号凭据（token JSON，点「平台凭据」），否则搜索为空。"
    ),
    "spotify": (
        "Spotify 需能访问 open.spotify.com，且下载依赖第三方服务；当前环境通常不可达。"
    ),
    "apple": (
        "Apple Music 需账号 + DRM 设备密钥(pywidevine)，本机需能访问 music.apple.com。"
    ),
    "soundcloud": "SoundCloud 需安装 pywidevine，且本机需能访问 soundcloud.com。",
    "youtube": "YouTube 需本机安装 Node 运行时，且需能访问 youtube.com。",
}

# 「重」档：装了依赖才注册。needs 里 "node" 表示需要外部 Node 可执行文件。
HEAVY_PLATFORMS = {
    "apple": ("Apple Music", _BASE + "apple", "AppleMusicClient", ("pywidevine",)),
    "soundcloud": (
        "SoundCloud",
        _BASE + "soundcloud",
        "SoundCloudMusicClient",
        ("pywidevine",),
    ),
    "youtube": (
        "YouTube",
        _BASE + "youtube",
        "YouTubeMusicClient",
        ("ytmusicapi", "node"),
    ),
}

# 所有可构建的平台（重档也在内；是否注册由依赖可用性决定）
ALL_PLATFORMS = {**PLATFORMS, **{k: v[:3] for k, v in HEAVY_PLATFORMS.items()}}


def _deps_ok(needs):
    for dep in needs or ():
        if dep == "node":
            if not (shutil.which("node") or shutil.which("nodejs")):
                return False
        elif importlib.util.find_spec(dep) is None:
            return False
    return True


def heavy_available():
    """返回依赖已满足的「重」档平台 [(key, 显示名), ...]。"""
    return [(k, v[0]) for k, v in HEAVY_PLATFORMS.items() if _deps_ok(v[3])]


# 平台分组：界面下拉按组归类、组内按显示名排序
_GROUP_OF = {
    "netease": "国内",
    "qq": "国内",
    "migu": "国内",
    "kuwo": "国内",
    "kugou": "国内",
    "qianqian": "国内",
    "fivesing": "国内",
    "bilibili": "国内",
    "bodian": "国内",
    "soda": "国内",
    "streetvoice": "国内",
    "moov": "国内",
    "joox": "国内",
    "apple": "国际",
    "spotify": "国际",
    "tidal": "国际",
    "qobuz": "国际",
    "deezer": "国际",
    "soundcloud": "国际",
    "youtube": "国际",
    "jamendo": "曲库",
    "audius": "曲库",
    "jiosaavn": "曲库",
    "fma": "曲库",
    "ccmixter": "曲库",
    "opengameart": "曲库",
    "wikimediacommons": "曲库",
    "suno": "曲库",
}
GROUP_ORDER = ["国内", "国际", "曲库"]


def group_of(key):
    return _GROUP_OF.get(key, "其它")


class _NullTask:
    def __init__(self):
        self.total = None
        self.completed = 0


class _NullProgress:
    """rich.Progress 的替身。

    musicdl 的 search/_search 会调用它，但界面不需要终端进度条。
    """

    def __init__(self):
        self.tasks = {}

    def add_task(self, *args, **kwargs):
        tid = len(self.tasks)
        self.tasks[tid] = _NullTask()
        return tid

    def update(self, task_id=None, **kwargs):
        t = self.tasks.get(task_id)
        if t is None:
            return
        if "total" in kwargs and kwargs["total"] is not None:
            t.total = kwargs["total"]
        if "completed" in kwargs and kwargs["completed"] is not None:
            t.completed = kwargs["completed"]

    def advance(self, task_id=None, advance=1):
        t = self.tasks.get(task_id)
        if t is not None:
            t.completed += advance


class MusicdlProvider(MusicProvider):
    """由 vendored musicdl 客户端驱动的通用 provider。"""

    requires_login = False
    supports_playlists = False

    def __init__(
        self, key, work_dir=None, search_size=15, max_retries=2, timeout=(10, 45)
    ):
        if key not in ALL_PLATFORMS:
            raise KeyError(f"未移植的平台：{key}")
        display, module_path, cls_name = ALL_PLATFORMS[key]
        self.key = key
        self.display_name = display
        self._module_path = module_path
        self._cls_name = cls_name
        self._search_size = int(search_size)
        self._max_retries = max(1, int(max_retries))
        # (连接, 读取) 超时：海外不可达的平台不必干等到天荒地老，正常平台几乎无感
        self._timeout = tuple(timeout) if timeout else None
        self._work_dir = work_dir or os.path.join(tempfile.gettempdir(), "nydl_musicdl")
        self._client = None

    # ---------- 内部 ----------

    def reload(self):
        """丢弃已建客户端；下次搜索会用最新凭据重建。"""
        self._client = None

    def _get_client(self):
        if self._client is None:
            cls = getattr(importlib.import_module(self._module_path), self._cls_name)
            cookies = get_cred(self.key) or None
            self._client = cls(
                search_size_per_source=self._search_size,
                search_size_per_page=self._search_size,
                work_dir=self._work_dir,
                disable_print=True,
                max_retries=self._max_retries,
                default_search_cookies=cookies,
                default_parse_cookies=cookies,
                default_download_cookies=cookies,
            )
        return self._client

    def _to_track(self, info):
        return Track(
            provider=self.key,
            id=str(info.identifier or info.song_name or ""),
            name=info.song_name or "未知歌曲",
            artists=info.singers or "未知歌手",
            album=info.album or "未知专辑",
            duration_s=int(info.duration_s or 0),
            cover_url=info.cover_url or "",
            ext=(info.ext or "").lstrip("."),
            extra={"info": info},
        )

    # ---------- 统一接口 ----------

    def search(self, keyword, limit=None, offset=0):
        if offset:
            return [], 0  # musicdl 不支持分页偏移
        client = self._get_client()
        try:
            infos = client.search(
                keyword=keyword,
                num_threadings=5,
                request_overrides=(
                    {"timeout": self._timeout} if self._timeout else None
                ),
                main_process_context=_NullProgress(),
                main_progress_id=None,
            )
        except Exception as e:
            # 平台接口不可达/变更时，把底层晦涩异常包成可读信息，界面直接可见
            raise RuntimeError(
                f"{self.display_name} 搜索失败（接口不可达或已变更）："
                f"{type(e).__name__} {str(e)[:80]}"
            ) from e
        tracks = [self._to_track(i) for i in infos]
        return tracks, len(tracks)

    def resolve(self, track, level=None):
        info = track.extra.get("info")
        if info is None:
            return ResolveResult(reason="缺少平台解析数据")
        if str(getattr(info, "protocol", "HTTP")).upper() == "HLS":
            return ResolveResult(reason="HLS 流暂不支持直链下载")
        if not info.with_valid_download_url:
            return ResolveResult(reason="该平台未返回可下载直链")
        return ResolveResult(
            url=info.download_url,
            ext=(info.ext or "").lstrip("."),
            http_headers=dict(info.default_download_headers or {}),
        )

    def lyric(self, track):
        info = track.extra.get("info")
        return ((info.lyric or "") if info is not None else ""), ""
