"""网易云音乐 Provider：把现有 weapi 客户端适配为统一接口。

v2.0 关键决策：
- 音质档位「上限语义 + standard 兜底」逻辑从 downloader 收敛到本 provider
- 权限类失败（VIP / 需购买 / 下架）标记为不可重试，只有传输类失败才可重试
"""

import re

from ..paths import session_file
from ..weapi import (
    NeteaseClient,
    track_album,
    track_artists,
    track_cover,
    track_duration,
    track_name,
    track_no,
)
from .base import MusicProvider, ResolveResult, Track

# 音质从高到低。请求不到直链时只补一个 standard 兜底，避免放大请求量触发风控。
_LEVEL_ORDER = ["hires", "lossless", "exhigh", "higher", "standard"]


def _levels_to_try(want):
    """可尝试的档位序列，最多两次请求。"""
    if want == "standard" or want not in _LEVEL_ORDER:
        return [want]
    return [want, "standard"]


def _ext_from(res):
    """根据接口返回推断扩展名。"""
    t = (res.get("type") or "").lower()
    if t in ("flac", "mp3", "m4a", "mp4", "aac", "wav"):
        return t
    url = (res.get("url") or "").lower()
    m = re.search(r"\.(flac|mp3|m4a|aac|wav)(?:\?|$)", url)
    return m.group(1) if m else "mp3"


class NeteaseProvider(MusicProvider):
    key = "netease"
    display_name = "网易云音乐"
    requires_login = True
    supports_playlists = True

    def __init__(self, cookie_file=None, client=None):
        self.client = client or NeteaseClient(cookie_file or str(session_file()))

    # ---------- 登录 / 账号 ----------

    @property
    def logged_in(self):
        return self.client.logged_in

    def is_logged_in(self):
        return self.client.logged_in

    @property
    def cookie_names(self):
        return self.client.cookie_names

    def account(self):
        return self.client.account()

    def login_by_cookie(self, text):
        return self.client.login_by_cookie(text)

    def clear_cookies(self):
        self.client.clear_cookies()

    # ---------- 歌单 ----------

    def user_playlists(self, uid):
        return self.client.user_playlists(uid)

    def playlist_tracks(self, pid):
        return [self._to_track(s) for s in self.client.playlist_tracks(pid)]

    # ---------- 搜索 / 解析 / 歌词 ----------

    def search(self, keyword, limit=30, offset=0):
        songs, total = self.client.search(keyword, limit=limit, offset=offset)
        return [self._to_track(s) for s in songs], total

    def resolve(self, track, level=None):
        sid = track.extra.get("tid") or track.id
        raw = track.extra.get("raw") or {}
        res, used = {}, ""
        for lv in _levels_to_try(level or "exhigh"):
            lst = self.client.song_urls([sid], level=lv)
            cand = lst[0] if lst else {}
            if cand.get("url"):
                res, used = cand, lv
                break
            if not res:
                res = cand  # 留一份用于判断失败原因
        url = res.get("url")
        if not url:
            if res.get("freeTrialInfo"):
                return ResolveResult(reason="仅试听片段（VIP 专属），已跳过")
            fee = raw.get("fee")
            if fee == 1:
                return ResolveResult(reason="VIP 专属，当前账号无权限")
            if fee == 4:
                return ResolveResult(reason="需购买专辑")
            return ResolveResult(reason="无版权或已下架")
        return ResolveResult(
            url=url,
            ext=_ext_from(res),
            level=used,
            http_headers={"Referer": "https://music.163.com/"},
        )

    def lyric(self, track):
        try:
            return self.client.lyric(track.id)
        except Exception:
            return "", ""

    # ---------- 转换 ----------

    def _to_track(self, song):
        return Track(
            provider=self.key,
            id=str(song.get("id")),
            name=track_name(song),
            artists=track_artists(song),
            album=track_album(song),
            duration_s=track_duration(song),
            cover_url=track_cover(song),
            track_no=track_no(song),
            vip=(song.get("fee") == 1),
            extra={"raw": song, "tid": song.get("tid")},
        )
