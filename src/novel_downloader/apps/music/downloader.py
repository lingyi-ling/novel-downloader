"""下载与落盘：直链下载、ID3/FLAC 标签、封面内嵌、歌词文件。

v1.0 关键决策：
- 先下到 .part 临时文件再改名，避免中断留下半截文件被当成"已下载"
- 标签用 mutagen 的 easy 模式（mp3/flac 通吃），封面单独按格式处理
- 受限歌曲（VIP/付费/无版权）不报错中断，而是记录原因跳过
"""

import contextlib
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests

from .weapi import UA, LoginExpired

_CHUNK = 64 * 1024
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class DownloadOptions:
    out_dir: str
    level: str = "exhigh"
    embed_tags: bool = True
    embed_cover: bool = True
    save_lyric: bool = True
    split_folder: bool = True
    skip_existing: bool = True
    retries: int = 2  # 传输类失败自动重试次数（不含首次）


@dataclass
class TrackResult:
    status: str  # ok / skip / fail
    reason: str = ""
    path: str = ""
    bytes_: int = 0
    level: str = ""  # 实际用到的音档位（可能低于请求档位）
    retryable: bool = False  # 是否属于"换个时间/网络可能成功"的失败
    resumed_from: int = 0  # 本次是从多少字节处续传的（0 = 全新下载）


def sanitize(name, fallback="未命名"):
    """清掉 Windows 非法字符和结尾的点/空格。"""
    name = _ILLEGAL.sub("_", str(name or "")).strip().rstrip(". ")
    name = re.sub(r"\s+", " ", name)
    return name[:120] or fallback


def build_dir(opts: DownloadOptions, track):
    """按需生成 输出目录[/歌手/专辑] 。"""
    if not opts.split_folder:
        return opts.out_dir
    return os.path.join(opts.out_dir, sanitize(track.artists), sanitize(track.album))


def build_stem(track):
    """文件名主体：歌名 - 歌手。"""
    return f"{sanitize(track.name)} - {sanitize(track.artists)}"


def _remove(path):
    if os.path.exists(path):
        with contextlib.suppress(OSError):
            os.remove(path)


def _download_file(
    url,
    dest,
    progress=None,
    stop_flag=None,
    timeout=60,
    resume=True,
    extra_headers=None,
):
    """流式下载到 dest，支持断点续传。

    返回 (写入总字节数, 续传起点)。起点 > 0 表示确实接续了已有分片。

    续传策略：
    - 已有 .part 就带 Range 请求，服务端回 206 表示支持，接续写入
    - 回 200 说明服务端不支持 Range，只能截断重下
    - 回 416 说明分片比服务端文件还大（不可信），丢弃重来
    - 下完校验总大小，不符视为失败，避免留下损坏文件
    """
    headers = {"User-Agent": UA}
    if extra_headers:
        headers.update(extra_headers)
    tmp = dest + ".part"
    start = os.path.getsize(tmp) if (resume and os.path.exists(tmp)) else 0
    if start:
        headers["Range"] = f"bytes={start}-"

    r = requests.get(url, headers=headers, stream=True, timeout=timeout)
    try:
        if start and r.status_code == 416:
            r.close()
            _remove(tmp)
            return _download_file(
                url,
                dest,
                progress,
                stop_flag,
                timeout,
                resume=False,
                extra_headers=extra_headers,
            )
        if start and r.status_code != 206:
            start = 0  # 服务端不支持续传，截断重下
        if r.status_code not in (200, 206):
            raise OSError(f"HTTP {r.status_code}")

        ctype = (r.headers.get("Content-Type") or "").lower()
        # 直链过期时 CDN 常返回 200 + 一段 HTML/JSON 错误页；
        # 不拦掉就会把网页当音频存下来
        if ctype.startswith("text/") or "html" in ctype or "json" in ctype:
            raise OSError(f"返回的不是音频（Content-Type: {ctype}），直链可能已过期")

        total = 0
        if r.status_code == 206:
            m = re.search(r"/(\d+)\s*$", r.headers.get("Content-Range") or "")
            if m:
                total = int(m.group(1))
        if not total:
            total = start + int(r.headers.get("Content-Length") or 0)

        done = start
        if progress:
            progress(done, total)
        with open(tmp, "ab" if start else "wb") as f:
            for chunk in r.iter_content(_CHUNK):
                if stop_flag and stop_flag():
                    raise InterruptedError("用户取消")
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    finally:
        with contextlib.suppress(Exception):
            r.close()

    if total and done != total:
        raise OSError(f"大小不符（期望 {total}，实得 {done}），断点已保留")

    os.replace(tmp, dest)
    return done, start


def _cleanup_part(dest):
    """清掉中断留下的 .part 临时文件。"""
    p = dest + ".part"
    if os.path.exists(p):
        with contextlib.suppress(OSError):
            os.remove(p)


def _write_tags(path, track, cover_bytes=None):
    """写 ID3/Vorbis 标签，按需内嵌封面。"""
    from mutagen import File as MutaFile
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import APIC, ID3

    try:
        audio = MutaFile(path, easy=True)
    except Exception:
        return
    if audio is None:
        return

    audio["title"] = track.name
    audio["artist"] = track.artists
    audio["album"] = track.album
    no = track.track_no
    if no:
        audio["tracknumber"] = str(no)
    audio.save()

    if not cover_bytes:
        return

    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".flac":
            flac = FLAC(path)
            flac.clear_pictures()
            pic = Picture()
            pic.type = 3  # front cover
            pic.mime = "image/jpeg"
            pic.desc = "Cover"
            pic.data = cover_bytes
            flac.add_picture(pic)
            flac.save()
        else:
            mime = (
                "image/png" if cover_bytes[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
            )
            tags = ID3(path)
            tags.delall("APIC")
            tags.add(
                APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover_bytes)
            )
            tags.save(v2_version=3)
    except Exception:
        # 封面失败不影响音频本身，直接忽略
        pass


def download_track(
    provider, track, opts: DownloadOptions, progress=None, stop_flag=None
):
    """下载单首歌。返回 TrackResult。

    直链解析交给 provider（各平台逻辑不同），本函数只管落盘。
    """
    if not track.id:
        return TrackResult("fail", "缺少歌曲 id")

    rr = provider.resolve(track, level=opts.level)
    if not rr.ok:
        return TrackResult(
            "fail", rr.reason or "无法获取播放地址", retryable=rr.retryable
        )
    url = rr.url
    ext = rr.ext or track.ext or "mp3"
    folder = build_dir(opts, track)
    stem = build_stem(track)
    dest = os.path.join(folder, f"{stem}.{ext}")

    # 同名不同曲的情况：用 id 兜底避免互相覆盖
    if os.path.exists(dest):
        try:
            from mutagen import File as MutaFile

            other = MutaFile(dest, easy=True)
            if other and str(other.get("title", [""])[0]) != track.name:
                dest = os.path.join(folder, f"{stem} [{track.id}].{ext}")
        except Exception:
            dest = os.path.join(folder, f"{stem} [{track.id}].{ext}")

    if opts.skip_existing and os.path.exists(dest) and os.path.getsize(dest) > 10240:
        return TrackResult("skip", "已存在", path=dest, bytes_=os.path.getsize(dest))

    os.makedirs(folder, exist_ok=True)

    # 传输类失败自动重试；重试期间 .part 保留，因此顺带就是续传
    size, resumed = 0, 0
    attempts = max(0, opts.retries) + 1  # 0 次重试 = 只尝试 1 次
    last_err = None
    for i in range(attempts):
        try:
            size, resumed = _download_file(
                url,
                dest,
                progress=progress,
                stop_flag=stop_flag,
                extra_headers=rr.http_headers,
            )
            last_err = None
            break
        except InterruptedError:
            # 用户中途停止：保留 .part，下次可从这里续传
            raise
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(min(1.5 * (i + 1), 5))
    if last_err is not None:
        detail = f"{type(last_err).__name__} {str(last_err)[:50]}"
        return TrackResult(
            "fail",
            f"下载失败（试了 {attempts} 次）：{detail}",
            retryable=True,
        )

    if opts.embed_tags or opts.embed_cover:
        cover = None
        if opts.embed_cover:
            pic = track.cover_url
            if pic:
                try:
                    cover = requests.get(
                        pic, headers={"User-Agent": UA}, timeout=20
                    ).content
                except Exception:
                    cover = None
        _write_tags(dest, track, cover if opts.embed_cover else None)

    if opts.save_lyric:
        try:
            lrc, tlrc = provider.lyric(track)
            text = lrc or ""
            if tlrc:
                text += "\n" + tlrc
            if text.strip():
                with open(
                    os.path.join(folder, f"{stem}.lrc"), "w", encoding="utf-8"
                ) as f:
                    f.write(text)
        except Exception:
            pass

    return TrackResult(
        "ok", path=dest, bytes_=size, level=rr.level, resumed_from=resumed
    )


def download_many(
    provider,
    tracks,
    opts,
    workers=3,
    on_result=None,
    on_tick=None,
    should_stop=None,
    min_interval=0.2,
):
    """并发下载多首。

    on_result(index, track, TrackResult) —— 每首结束回调
    on_tick(fraction, done_count)        —— 进度回调（内部节流，不会刷爆界面）
    should_stop() -> bool                —— 返回 True 则中止

    返回 (ok, skip, fail, failed_pairs, failed_tracks)。
    登录态失效（LoginExpired）会向上抛出，由界面层统一处理。
    """
    tracks = list(tracks)
    total = len(tracks)
    if total == 0:
        return 0, 0, 0, [], []

    lock = threading.Lock()
    active = {}  # index -> 该文件完成比例
    c = {"done": 0, "ok": 0, "skip": 0, "fail": 0}
    failed_pairs, failed_tracks = [], []
    cancelling = {"v": False}
    last_tick = {"t": 0.0}

    def stopped():
        return cancelling["v"] or bool(should_stop and should_stop())

    def tick(force=False):
        now = time.time()
        if not force and now - last_tick["t"] < min_interval:
            return
        last_tick["t"] = now
        if on_tick:
            with lock:
                frac = (c["done"] + sum(active.values())) / total
                done_n = c["done"]
            on_tick(min(1.0, frac), done_n)

    def work(i, t):
        if stopped():
            return
        with lock:
            active[i] = 0.0

        def prog(done, size, _i=i):
            with lock:
                active[_i] = (done / size) if size else 0.0
            tick()

        try:
            r = download_track(provider, t, opts, progress=prog, stop_flag=stopped)
        except LoginExpired:
            with lock:
                active.pop(i, None)
            raise
        except InterruptedError:
            with lock:
                active.pop(i, None)
            cancelling["v"] = True
            return
        except Exception as e:
            r = TrackResult("fail", f"异常 {type(e).__name__} {str(e)[:60]}")

        with lock:
            active.pop(i, None)
            c["done"] += 1
            if r.status == "ok":
                c["ok"] += 1
            elif r.status == "skip":
                c["skip"] += 1
            else:
                c["fail"] += 1
                failed_pairs.append((f"{t.name} - {t.artists}", r.reason))
                failed_tracks.append(t)
        if on_result:
            on_result(i, t, r)
        tick(force=True)

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        futures = [ex.submit(work, i, t) for i, t in enumerate(tracks)]
        for f in futures:
            f.result()  # 让 LoginExpired 能冒出来

    return c["ok"], c["skip"], c["fail"], failed_pairs, failed_tracks


def human(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"
