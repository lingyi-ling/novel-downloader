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
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests

from novel_downloader.infra.i18n import t
from novel_downloader.libs.filesystem import sanitize_filename

from .weapi import UA, LoginExpired

_CHUNK = 64 * 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)")


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
    """清掉 Windows 非法字符和结尾的点/空格（统一复用 libs 的清洗实现）。"""
    return sanitize_filename(
        str(name or ""),
        max_length=120,
        collapse_whitespace=True,
        fallback=fallback,
    )


def build_dir(opts: DownloadOptions, track):
    """按需生成 输出目录[/歌手/专辑] 。"""
    if not opts.split_folder:
        return opts.out_dir
    return os.path.join(opts.out_dir, sanitize(track.artists), sanitize(track.album))


def build_stem(track):
    """文件名主体：歌名 - 歌手。"""
    return f"{sanitize(track.name)} - {sanitize(track.artists)}"


def _safe_ext(ext, default="mp3"):
    """扩展名只保留首个字母数字段。

    ext 来自平台接口返回（外部数据），可能被塞进路径分隔符或 ``..``；
    原样拼进文件名会越出输出目录，这里剥成纯 token。
    """
    m = re.search(r"[A-Za-z0-9]+", str(ext or ""))
    return m.group(0) if m else default


def _content_range_start(value):
    """解析 Content-Range 的区间起点（``bytes 512-1023/1024`` -> 512）。

    头缺失或畸形时返回 None，由调用方决定是否信任。
    """
    m = _CONTENT_RANGE.match((value or "").strip())
    return int(m.group(1)) if m else None


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
    - 回 206 但 Content-Range 起点和请求不符：内容从 0 开始就丢弃分片整段
      重写；起点错位到别处则直接报错（不把可疑字节拼进分片）
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

        if start:
            served = _content_range_start(r.headers.get("Content-Range"))
            if served is not None and served != start:
                if served:
                    raise OSError(
                        f"206 续传起点与请求不符"
                        f"（请求 bytes={start}-，返回 bytes={served}-）"
                    )
                # 服务端无视 Range 却回了 206 且内容从 0 开始：
                # 分片作废整段重写，避免把完整文件拼到旧分片后面
                start = 0

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
                    raise InterruptedError(t("Cancelled by user"))
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


def _remember_path(track, path):
    """记录本地落盘位置，便于之后导出歌单（Track.path）。"""
    with contextlib.suppress(Exception):
        track.path = path


def _write_tags(path, track, cover_bytes=None):
    """写 ID3/Vorbis 标签，按需内嵌封面。

    标签与封面都是「锦上添花」：任一环节失败（缺依赖、格式不支持、
    图片字节损坏）都只跳过该环节，绝不让已下载的音频变成失败。
    """
    try:
        from mutagen import File as MutaFile
        from mutagen.flac import FLAC, Picture
        from mutagen.id3 import APIC, ID3
    except Exception:
        return

    try:
        audio = MutaFile(path, easy=True)
    except Exception:
        return
    if audio is None:
        return

    try:
        audio["title"] = track.name
        audio["artist"] = track.artists
        audio["album"] = track.album
        no = track.track_no
        if no:
            audio["tracknumber"] = str(no)
        audio.save()
    except Exception:
        # 某些容器不支持 easy 标签；音频本身仍然可用
        pass

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


# ---------------------------------------------------------------------------
# 同一目标文件的串行写
# ---------------------------------------------------------------------------

_dest_locks: dict = {}
_dest_locks_guard = threading.Lock()


@contextlib.contextmanager
def _dest_lock(dest):
    """持有期间独占 dest 的写入权（含 .part / 标签 / 歌词落盘）。

    同一批次里出现重复曲目、或多首同名歌并发下载时，保证「检查存在 ->
    下载 -> 改名 -> 写标签」对同一路径是串行的：后到者会看到前者写完的
    文件（skip_existing 因此能生效），而不是两个线程交叉写同一个文件。
    锁表带引用计数，空闲即清理，长会话不会无限增长。
    """
    key = os.path.normcase(os.path.abspath(dest))
    with _dest_locks_guard:
        entry = _dest_locks.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    entry[0].acquire()
    try:
        yield
    finally:
        entry[0].release()
        with _dest_locks_guard:
            entry[1] -= 1
            if entry[1] <= 0 and _dest_locks.get(key) is entry:
                del _dest_locks[key]


def download_track(
    provider, track, opts: DownloadOptions, progress=None, stop_flag=None
):
    """下载单首歌。返回 TrackResult。

    直链解析交给 provider（各平台逻辑不同），本函数只管落盘。
    同一目标文件全程持有文件锁（见 _dest_lock），并发批次里的重复
    曲目不会两个线程交叉写同一路径。
    """
    if not track.id:
        return TrackResult("fail", "缺少歌曲 id")

    rr = provider.resolve(track, level=opts.level)
    if not rr.ok:
        return TrackResult(
            "fail", rr.reason or "无法获取播放地址", retryable=rr.retryable
        )
    url = rr.url
    ext = _safe_ext(rr.ext or track.ext)
    folder = build_dir(opts, track)
    stem = build_stem(track)
    dest = os.path.join(folder, f"{stem}.{ext}")

    # 同名不同曲的情况：用 id 兜底避免互相覆盖。
    # ext / id 都来自平台返回的外部数据，同样要清洗，防止拼出越界路径
    if os.path.exists(dest):
        suffix = f"[{sanitize(track.id)}]"
        try:
            from mutagen import File as MutaFile

            other = MutaFile(dest, easy=True)
            if other and str(other.get("title", [""])[0]) != track.name:
                dest = os.path.join(folder, f"{stem} {suffix}.{ext}")
        except Exception:
            dest = os.path.join(folder, f"{stem} {suffix}.{ext}")

    with _dest_lock(dest):
        if (
            opts.skip_existing
            and os.path.exists(dest)
            and os.path.getsize(dest) > 10240
        ):
            _remember_path(track, dest)
            return TrackResult(
                "skip", "已存在", path=dest, bytes_=os.path.getsize(dest)
            )

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

        _remember_path(track, dest)

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
                save_lyric(dest, lrc, tlrc)
            except Exception:
                # 歌词获取/落盘失败同样不影响音频
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
    tick_lock = threading.Lock()
    active = {}  # index -> 该文件完成比例
    c = {"done": 0, "ok": 0, "skip": 0, "fail": 0}
    failed_pairs, failed_tracks = [], []
    cancelling = {"v": False}
    last_tick = {"t": 0.0}

    def stopped():
        return cancelling["v"] or bool(should_stop and should_stop())

    def tick(force=False):
        if on_tick is None:
            return
        # on_tick 通常是界面回调：节流判断、快照读取与回调本身都串行化，
        # 避免两个工作线程同时把进度刷给界面
        with tick_lock:
            now = time.time()
            if not force and now - last_tick["t"] < min_interval:
                return
            last_tick["t"] = now
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


# ---------------------------------------------------------------------------
# 歌词落盘
# ---------------------------------------------------------------------------


def lyric_paths(audio_path):
    """返回某音频对应的 (原文 LRC 路径, 翻译 LRC 路径)。"""
    stem = os.path.splitext(str(audio_path))[0]
    return stem + ".lrc", stem + ".zh.lrc"


def _write_text(path, text):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text.rstrip() + "\n")


def _normalize_lrc(text):
    """LRC 文本统一换行为 ``\\n``（部分来源是 CRLF，很多播放器解析不了）。"""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def save_lyric(audio_path, lyric, translated=""):
    """把歌词写到音频旁，返回实际写入的文件列表。

    落盘策略（固定为独立文件，便于播放器/工具各取所需）：

    - 有原文：``<stem>.lrc`` 写原文；若另有译文，``<stem>.zh.lrc`` 写译文
    - 只有译文：把译文写进 ``<stem>.lrc``，避免播放器完全看不到歌词
    - 两者都为空：不创建任何文件

    空白文本会被忽略；换行统一为 ``\\n``，文件编码 UTF-8。
    """
    original_path, translation_path = lyric_paths(audio_path)
    text = _normalize_lrc(lyric)
    translation = _normalize_lrc(translated)
    written = []
    if text:
        _write_text(original_path, text)
        written.append(original_path)
        if translation:
            _write_text(translation_path, translation)
            written.append(translation_path)
    elif translation:
        _write_text(original_path, translation)
        written.append(original_path)
    return written


# ---------------------------------------------------------------------------
# 歌单导出（M3U8）
# ---------------------------------------------------------------------------


def _playlist_field(track, *names, default=""):
    """从 Track / dict 里取第一个非空字段。"""
    for name in names:
        if isinstance(track, Mapping):
            value = track.get(name)
        else:
            value = getattr(track, name, None)
        if value not in (None, ""):
            return value
    return default


def _playlist_entry(item):
    """把支持的输入形态归一成 (元数据对象, 本地文件路径)。

    支持 ``Track``（读 ``.path``）、``(track, path)`` 元组，以及带
    ``path``/``file`` 键的 dict。
    """
    if isinstance(item, (tuple, list)) and len(item) == 2:
        track, file_path = item
    elif isinstance(item, Mapping) and "track" in item:
        track = item["track"]
        file_path = item.get("path") or item.get("file")
        if not file_path:
            file_path = _playlist_field(track, "path", "file_path")
    else:
        track = item
        file_path = _playlist_field(item, "path", "file_path")
    if not file_path:
        raise ValueError(
            "导出歌单需要每首歌的本地路径：使用 Track.path 或 (track, path) 元组"
        )
    return track, str(file_path)


def _playlist_location(file_path, playlist_dir, absolute):
    """生成写入 M3U 的路径：默认相对歌单所在目录，统一用正斜杠。"""
    if absolute:
        location = os.path.abspath(file_path)
    else:
        try:
            location = os.path.relpath(os.path.abspath(file_path), playlist_dir)
        except ValueError:
            # Windows 跨盘符没有相对路径，退回绝对路径
            location = os.path.abspath(file_path)
    location = _CONTROL.sub(" ", location.replace("\\", "/")).strip()
    # 「#」开头的行会被 M3U 当作注释/指令，相对路径前加 ./ 规避
    if not absolute and location.startswith("#"):
        location = "./" + location
    return location


def export_m3u(tracks, path, *, absolute=False):
    """把曲目列表导出为 M3U8 歌单，返回歌单文件路径。

    ``tracks`` 中每项可以是 ``Track``（需已由下载流程回填 ``.path``）、
    ``(track, file_path)`` 元组，或包含 ``path``/``file`` 的 dict。
    ``absolute=False``（默认）写入相对歌单目录的路径，便于整个目录搬家。
    列表为空时也会写入只有 ``#EXTM3U`` 头的合法歌单。
    """
    playlist_path = str(path)
    playlist_dir = os.path.dirname(os.path.abspath(playlist_path))
    entries = []
    for item in tracks or ():
        track, file_path = _playlist_entry(item)
        name = _CONTROL.sub(" ", str(_playlist_field(track, "name", "title")))
        artists = _CONTROL.sub(
            " ", str(_playlist_field(track, "artists", "artist", default="未知歌手"))
        )
        duration = _playlist_field(track, "duration_s", "duration", default=0)
        try:
            duration = int(float(duration or 0))
        except (TypeError, ValueError):
            duration = 0
        if duration <= 0:
            duration = -1  # M3U 用 -1 表示时长未知
        location = _playlist_location(file_path, playlist_dir, absolute)
        entries.append(
            f"#EXTINF:{duration},{artists.strip()} - {name.strip()}\n{location}"
        )

    if playlist_dir:
        os.makedirs(playlist_dir, exist_ok=True)
    with open(playlist_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("#EXTM3U\n")
        for entry in entries:
            f.write(entry + "\n")
    return playlist_path
