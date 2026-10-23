"""网易云音乐 WEAPI 客户端。

背景：网易云没有面向第三方的公开 OAuth，第三方工具统一走其 Web 端的
WEAPI 协议（AES 两层加密 params + RSA 加密 encSecKey），登录沿用扫码授权。
本模块只依赖 requests + pycryptodome。

v1.0 关键决策：
- 扫码登录走 weapi 接口，失败时回退老的 /api/ 接口
- Cookie 落盘保存，避免每次重开都要重新扫码
- 只做只读接口（歌单/歌曲/歌词/播放地址），不做任何写操作
"""

import base64
import json
import os
import random
import string
from binascii import hexlify

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

BASE = "https://music.163.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

# WEAPI 固定参数（来自网易云 Web 端 JS）
_MODULUS = (
    "e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab17a876aea8a5aa76d2e417629ec4"
    "ee341f56135fccf695280104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d8"
    "13cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7"
)
_PUB_KEY = "010001"
_NONCE = "0CoJUm6Qyw8W8jud"  # 第一层 AES 固定密钥
_IV = "0102030405060708"

# 音质档位 -> 接口 level 值
LEVELS = {
    "standard": "标准",
    "higher": "较高",
    "exhigh": "极高",
    "lossless": "无损",
    "hires": "Hi-Res",
}


def _aes_encrypt(text, key):
    """AES-128-CBC 加密后 base64。"""
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, _IV.encode("utf-8"))
    return base64.b64encode(cipher.encrypt(pad(text.encode("utf-8"), 16))).decode(
        "utf-8"
    )


def _rsa_encrypt(secret):
    """RSA 加密第二层密钥：先把密钥倒序，再按十六进制做大数模幂，末尾补零到 256 位。"""
    text = secret[::-1]
    rs = int(hexlify(text.encode("utf-8")), 16)
    return format(pow(rs, int(_PUB_KEY, 16), int(_MODULUS, 16)), "x").zfill(256)


def weapi_encrypt(data):
    """把请求体加密成 WEAPI 的 {params, encSecKey}。"""
    text = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    secret = "".join(random.choices(string.ascii_letters + string.digits, k=16))
    params = _aes_encrypt(_aes_encrypt(text, _NONCE), secret)
    return {"params": params, "encSecKey": _rsa_encrypt(secret)}


class NeteaseError(Exception):
    """接口返回了非预期结果。"""


class LoginExpired(NeteaseError):
    """登录态已失效/过期。网易云对未登录的用户类接口返回 code 301。"""


def _guard(j):
    """识别登录态失效，转成明确异常，避免被当成"没有数据"静默吞掉。"""
    if isinstance(j, dict) and j.get("code") == 301:
        raise LoginExpired("登录态已失效（网易云返回 301），请重新登录")
    return j


class NeteaseClient:
    def __init__(self, cookie_file=None):
        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": UA,
                "Referer": BASE,
                "Origin": BASE,
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        self.cookie_file = cookie_file
        if cookie_file:
            self.load_cookies()

    # ---------- 会话持久化 ----------

    def load_cookies(self):
        if self.cookie_file and os.path.exists(self.cookie_file):
            try:
                with open(self.cookie_file, encoding="utf-8") as f:
                    for k, v in json.load(f).items():
                        self.s.cookies.set(k, v, domain=".music.163.com")
            except Exception:
                pass

    def save_cookies(self):
        if not self.cookie_file:
            return
        data = {c.name: c.value for c in self.s.cookies}
        tmp = self.cookie_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.cookie_file)

    def clear_cookies(self):
        self.s.cookies.clear()
        if self.cookie_file and os.path.exists(self.cookie_file):
            os.remove(self.cookie_file)

    @property
    def logged_in(self):
        return bool(self.s.cookies.get("MUSIC_U"))

    @property
    def cookie_names(self):
        """当前会话里有哪些 cookie，用于排查登录态。"""
        return sorted({c.name for c in self.s.cookies})

    # ---------- 请求底层 ----------

    def _csrf(self):
        return self.s.cookies.get("__csrf") or ""

    def post_weapi(self, path, data=None, timeout=20):
        """走 WEAPI 加密通道。自动带上 __csrf，避免部分接口判为未登录。"""
        body = dict(data or {})
        csrf = self._csrf()
        if csrf:
            body.setdefault("csrf_token", csrf)
        payload = weapi_encrypt(body)
        r = self.s.post(
            f"{BASE}/weapi/{path}",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=timeout,
        )
        return self._parse(r)

    def post_api(self, path, data=None, timeout=20):
        """走老的明文 /api/ 通道（部分接口更稳）。"""
        r = self.s.post(f"{BASE}/api/{path}", data=dict(data or {}), timeout=timeout)
        return self._parse(r)

    def _post_weapi_raw(self, path, data=None, timeout=20):
        """同 post_weapi，但返回原始 Response，便于读取 Set-Cookie。"""
        body = dict(data or {})
        csrf = self._csrf()
        if csrf:
            body.setdefault("csrf_token", csrf)
        return self.s.post(
            f"{BASE}/weapi/{path}",
            data=weapi_encrypt(body),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=timeout,
        )

    def _post_api_raw(self, path, data=None, timeout=20):
        """同 post_api，但返回原始 Response。"""
        return self.s.post(f"{BASE}/api/{path}", data=dict(data or {}), timeout=timeout)

    @staticmethod
    def _parse(r):
        if r.status_code != 200:
            raise NeteaseError(f"HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise NeteaseError(f"返回非 JSON：{r.text[:120]!r}") from None

    # ---------- 扫码登录 ----------

    def qr_start(self):
        """申请二维码 key，返回 (unikey, 二维码内容URL)。"""
        try:
            j = self.post_weapi("login/qrcode/unikey", {"type": 1})
            key = j.get("unikey")
        except Exception:
            key = None
        if not key:
            j = self.post_api("login/qrcode/unikey", {"type": 1})
            key = j.get("unikey")
        if not key:
            raise NeteaseError("获取二维码 key 失败，可能需要挂代理或稍后重试")
        return key, f"{BASE}/login?codekey={key}"

    def _absorb_cookies(self, r):
        """把响应里的 Set-Cookie 显式并入会话。

        803 时 MUSIC_U 就藏在这一次响应里，某些传输路径不会自动落进 Session，
        显式吸收一遍，并返回拿到的 cookie 名，便于排查。
        """
        if r is None:
            return []
        names = []
        for c in r.cookies:
            self.s.cookies.set(c.name, c.value, domain=".music.163.com")
            names.append(c.name)
        return names

    def qr_check(self, unikey):
        """查询扫码状态。返回 (code, message)。

        code: 800 已过期 / 801 等待扫码 / 802 待确认 / 803 授权成功
        官方实现走 /api/ 端点，这里以它为主，weapi 作回退。
        """
        data = {"key": unikey, "type": 1}
        j, r = None, None
        for raw in (self._post_api_raw, self._post_weapi_raw):
            try:
                r = raw("login/qrcode/client/login", data)
                j = self._parse(r)
            except Exception:
                j, r = None, None
                continue
            if j.get("code") is not None:
                break
            j, r = None, None
        if j is None:
            raise NeteaseError("扫码状态查询失败（两个端点都没返回有效结果）")

        code = j.get("code")
        if code == 803:
            self._absorb_cookies(r)
            self.save_cookies()
            # 关键校验：拿不到 MUSIC_U 就等于没登录，必须显式报错而不是假装成功
            if not self.s.cookies.get("MUSIC_U"):
                raise NeteaseError("显示授权成功，但未取到 MUSIC_U，请重新扫码")
        msg = {
            800: "二维码已过期",
            801: "等待扫码",
            802: "已扫码，请在手机上确认",
            803: "授权成功",
        }.get(code, str(j.get("message") or j.get("msg") or code))
        return code, msg

    def account(self):
        """返回 (account, profile)，未登录时均为 None。

        依次尝试 api / weapi 两个端点，任一返回账号即算成功。
        """
        last = None
        for fn in (self.post_api, self.post_weapi):
            try:
                j = fn("nuser/account/get", {})
            except Exception as e:
                last = e
                continue
            _guard(j)
            if j.get("account"):
                return j.get("account"), j.get("profile")
        if last is not None:
            raise last
        return None, None

    def login_by_cookie(self, text):
        """用浏览器里的 Cookie 登录。

        网易云对第三方扫码登录启用了行为验证风控（8821），扫码不可用，
        故改为复用浏览器里已登录的会话。接受整串 Cookie，也接受只粘贴
        MUSIC_U 的值。校验通过后落盘保存。
        """
        text = (text or "").strip()
        if not text:
            raise NeteaseError("Cookie 为空")
        if "MUSIC_U" not in text:
            # 只给了 MUSIC_U 的值
            text = "MUSIC_U=" + text
        n = 0
        for part in text.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                self.s.cookies.set(k, v, domain=".music.163.com")
                n += 1
        if not self.s.cookies.get("MUSIC_U"):
            raise NeteaseError("没能从输入里解析出 MUSIC_U")
        try:
            account, profile = self.account()
        except Exception as e:
            self.clear_cookies()
            raise NeteaseError(f"校验登录态失败：{e}") from e
        if not account:
            self.clear_cookies()
            raise NeteaseError("Cookie 无效或已过期，请确认是在已登录状态下复制")
        self.save_cookies()
        return account, profile, n

    # ---------- 歌单 ----------

    def user_playlists(self, uid):
        """拿用户全部歌单。第一条通常是「我喜欢的音乐」(specialType=5)。"""
        j = self.post_weapi(
            "user/playlist",
            {"uid": uid, "offset": 0, "limit": 1000, "includeVideo": True},
        )
        _guard(j)
        return j.get("playlist") or []

    def playlist_tracks(self, pid):
        """拿歌单全部歌曲。超过 1000 首时用 song/detail 分批补齐。"""
        j = self.post_weapi("v6/playlist/detail", {"id": pid, "n": 0, "s": 0})
        _guard(j)
        pl = j.get("playlist") or {}
        tracks = pl.get("tracks") or []
        track_ids = [t["id"] for t in (pl.get("trackIds") or [])]
        if len(track_ids) > len(tracks):
            tracks = self.song_detail(track_ids)
        return tracks

    def song_detail(self, ids):
        """按 id 批量取歌曲详情，每批 500。"""
        out = []
        ids = list(ids)
        for i in range(0, len(ids), 500):
            batch = ids[i : i + 500]
            c = json.dumps([{"id": str(x)} for x in batch], separators=(",", ":"))
            j = self.post_weapi("v3/song/detail", {"c": c})
            out.extend(j.get("songs") or [])
        return out

    def search(self, keyword, limit=100, offset=0):
        """搜索歌曲。返回 (歌曲列表, 总命中数)。

        优先用 cloudsearch（结果字段更全，与歌单曲目格式一致），
        失败则退回老的 search/get。
        """
        data = {
            "s": keyword,
            "type": 1,
            "limit": limit,
            "offset": offset,
            "total": "true",
        }
        songs, total, j = [], 0, None
        for path in ("cloudsearch/get/web", "search/get"):
            try:
                j = self.post_weapi(path, data)
            except Exception:
                continue
            _guard(j)
            res = j.get("result") or {}
            songs = res.get("songs") or []
            if songs or res.get("songCount"):
                total = res.get("songCount") or len(songs)
                break
        return songs, total

    # ---------- 播放地址 / 歌词 ----------

    def song_urls(self, ids, level="exhigh"):
        """批量取播放直链。返回列表，每项含 url/size/type/level/br。"""
        ids = list(ids)
        out = []
        for i in range(0, len(ids), 100):
            batch = ids[i : i + 100]
            j = self.post_weapi(
                "song/enhance/player/url/v1",
                {
                    "ids": json.dumps(batch, separators=(",", ":")),
                    "level": level,
                    "encodeType": "flac" if level in ("lossless", "hires") else "mp3",
                },
            )
            _guard(j)
            out.extend(j.get("data") or [])
        return out

    def lyric(self, sid):
        """返回 (原文歌词, 翻译歌词)，均为 LRC 文本。"""
        j = self.post_weapi("song/lyric", {"id": sid, "lv": -1, "kv": -1, "tv": -1})
        return (j.get("lrc") or {}).get("lyric", ""), (j.get("tlyric") or {}).get(
            "lyric", ""
        )


# ---------- 歌曲字段读取（网易云字段名不直观，统一收口） ----------


def track_name(t):
    return t.get("name") or "未知歌曲"


def track_artists(t):
    ars = t.get("ar") or t.get("artists") or []
    names = [a.get("name") for a in ars if a.get("name")]
    return " / ".join(names) if names else "未知歌手"


def track_album(t):
    al = t.get("al") or t.get("album") or {}
    return al.get("name") or "未知专辑"


def track_cover(t):
    al = t.get("al") or t.get("album") or {}
    return al.get("picUrl") or ""


def track_duration(t):
    ms = t.get("dt") or t.get("duration") or 0
    return ms // 1000


def track_no(t):
    no = t.get("no")
    return int(no) if no else 0


if __name__ == "__main__":
    # 自检：用公开歌单验证 WEAPI 加密是否可用
    c = NeteaseClient()
    print("匿名请求公开歌单 3778678（热歌榜）...")
    r = c.post_weapi("v6/playlist/detail", {"id": 3778678, "n": 3, "s": 0})
    pl = r.get("playlist") or {}
    print("歌单名:", pl.get("name"))
    print("歌曲数:", len(pl.get("tracks") or []))
    for t in (pl.get("tracks") or [])[:3]:
        print("  -", track_name(t), "/", track_artists(t))
    print("登录态:", c.logged_in)
