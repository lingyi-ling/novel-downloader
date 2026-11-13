"""下载与落盘：直链下载、ID3/FLAC 标签、封面内嵌、歌词文件。

v1.0 关键决策：
- 先下到 .part 临时文件再改名，避免中断留下半截文件被当成"已下载"
- 标签用 mutagen 的 easy 模式（mp3/flac 通吃），封面单独按格式处理
- 受限歌曲（VIP/付费/无版权）不报错中断，而是记录原因跳过
"""

import contextlib
import errno
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import requests

from novel_downloader.infra.i18n import t
from novel_downloader.libs.filesystem import sanitize_filename

from .weapi import UA, LoginExpired

logger = logging.getLogger(__name__)

_CHUNK = 64 * 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)")

#: Cover art is a small image.  A hostile CDN (or a broken ``cover_url``) must
#: not be able to make the process buffer an arbitrary document in memory.
MAX_COVER_BYTES = 8 * 1024 * 1024


def _fetch_cover(url: str) -> bytes | None:
    """Download cover art with a hard size bound; never raise.

    ``requests.get(...).content`` buffers the whole body before any size check,
    so a redirect to a huge response could exhaust memory.  The body is read
    incrementally here and abandoned as soon as it passes
    :data:`MAX_COVER_BYTES`.  Any failure simply means "no embedded cover";
    the audio file itself is still usable.
    """
    response = None
    try:
        response = requests.get(
            url, headers={"User-Agent": UA}, timeout=20, stream=True
        )
        if response.status_code != 200:
            return None
        declared = response.headers.get("Content-Length")
        if declared:
            try:
                if int(declared) > MAX_COVER_BYTES:
                    return None
            except ValueError:
                pass
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(_CHUNK):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_COVER_BYTES:
                return None
            chunks.append(chunk)
        return b"".join(chunks)
    except Exception:
        return None
    finally:
        if response is not None:
            with contextlib.suppress(Exception):
                response.close()


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


def _lyric_missing(audio_path):
    """音频旁是否还缺歌词（按原文 ``.lrc`` 判断，与 :func:`lyric_paths` 一致）。"""
    original_path, _ = lyric_paths(audio_path)
    return not os.path.exists(original_path)


def _write_lyrics(provider, track, dest):
    """获取并落盘歌词；任何失败只记 WARNING，绝不影响音频结果。

    provider 可能返回空白/占位值，归一后再判断是否用 Track 自带字段
    回退（聚合搜索/序列化往返后常常已经带着 lyric，不必再联网）。
    """
    try:
        lrc, tlrc = provider.lyric(track) or ("", "")
    except Exception as exc:  # noqa: BLE001 - 歌词获取失败不影响音频
        logger.warning("music: 歌词获取失败 %s: %s", track.id or dest, exc)
        lrc, tlrc = "", ""
    lrc = _coerce_lyric(lrc)
    tlrc = _coerce_lyric(tlrc)
    fallback_lrc, fallback_tlrc = track_lyrics(track)
    try:
        save_lyric(dest, lrc or fallback_lrc, tlrc or fallback_tlrc)
    except Exception as exc:  # noqa: BLE001 - 歌词落盘失败不影响音频
        logger.warning("music: 歌词落盘失败 %s: %s", dest, exc)


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
            # 音频已存在就不重新下载，但歌词可能是后加的：缺 .lrc 才补写，
            # 已有歌词则完全跳过（不联网，也不改 mtime）。
            if opts.save_lyric and _lyric_missing(dest):
                _write_lyrics(provider, track, dest)
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
                    cover = _fetch_cover(pic)
            _write_tags(dest, track, cover if opts.embed_cover else None)

        if opts.save_lyric:
            _write_lyrics(provider, track, dest)

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


def group_by_provider(tracks, platform=None):
    """把曲目按 provider 分组，保留全局下标。

    ``platform`` 非空时强制全部使用该平台（历史单平台调用）；否则用每
    首歌自己的 ``provider``。返回 ``[(key, [(index, track), ...]), ...]``，
    组顺序按首次出现顺序，便于进度条从左到右推进。
    """
    groups = {}
    for index, track in enumerate(tracks):
        key = str(platform or getattr(track, "provider", "") or "").strip()
        groups.setdefault(key, []).append((index, track))
    return list(groups.items())


def download_mixed(
    create_provider,
    tracks,
    opts,
    workers=3,
    on_result=None,
    on_tick=None,
    should_stop=None,
    min_interval=0.2,
    platform=None,
):
    """按歌曲所属平台分组下载，进度与结果下标仍对应传入的完整列表。

    聚合搜索的结果可能来自多个平台（甚至同一行来自多个来源），单平台
    的 :func:`download_many` 无法处理。这里按 provider 依次下载：每个
    平台内部仍是并发；进度折算到全局下标；``on_result`` /
    ``failed_tracks`` 返回原始的曲目对象。

    ``platform`` 非空时忽略曲目自带的 provider（单平台界面的历史行为）。

    返回值与 :func:`download_many` 相同：
    ``(ok, skip, fail, failed_pairs, failed_tracks)``。
    """
    tracks = list(tracks)
    total = len(tracks)
    if total == 0:
        return 0, 0, 0, [], []

    groups = group_by_provider(tracks, platform)
    counts = {"ok": 0, "skip": 0, "fail": 0}
    # (track, reason) pairs, kept as objects so a rescued track can be removed
    # by identity. Removing from two parallel lists by value/label is wrong
    # when two different songs share the same "name - artist" label: the
    # fallback then deleted the *other* track's failure row.
    failure_records: list[tuple[Any, str]] = []
    base_done = 0
    # 有备选来源的失败先不回调：回退成功时只上报一次成功，避免界面把
    # 同一首歌记成「失败一次 + 成功一次」。
    deferred_failures: dict[int, tuple[Any, Any]] = {}

    for key, items in groups:
        if should_stop and should_stop():
            break
        provider = None
        if key and create_provider is not None:
            try:
                provider = create_provider(key)
            except LoginExpired:
                raise
            except Exception as exc:  # noqa: BLE001 - 单平台失败不影响其它平台
                for index, track in items:
                    counts["fail"] += 1
                    reason = f"平台不可用（{key}）：{exc}"
                    failure_records.append((track, reason))
                    if on_result:
                        result = TrackResult("fail", reason)
                        if getattr(track, "_alternatives", ()):
                            deferred_failures[index] = (track, result)
                        else:
                            on_result(index, track, result)
                base_done += len(items)
                continue
        if provider is None:
            for index, track in items:
                counts["fail"] += 1
                reason = "缺少平台信息，请重新搜索后再下载"
                failure_records.append((track, reason))
                if on_result:
                    result = TrackResult("fail", reason)
                    if getattr(track, "_alternatives", ()):
                        deferred_failures[index] = (track, result)
                    else:
                        on_result(index, track, result)
            base_done += len(items)
            continue

        group_tracks = [track for _index, track in items]
        group_len = len(group_tracks)
        group_base = base_done

        def group_result(local_index, track, result, _items=items):
            if not on_result:
                return
            index = _items[local_index][0]
            if result.status == "fail" and getattr(track, "_alternatives", ()):
                deferred_failures[index] = (track, result)
                return
            on_result(index, track, result)

        def group_tick(fraction, done_n, _base=group_base, _len=group_len):
            if on_tick:
                done = _base + min(_len, max(0, int(done_n)))
                overall = (done / total) if total else 1.0
                on_tick(min(1.0, overall), done)

        ok, skip, fail, pairs, failed = download_many(
            provider,
            group_tracks,
            opts,
            workers=workers,
            on_result=group_result,
            on_tick=group_tick,
            should_stop=should_stop,
            min_interval=min_interval,
        )
        counts["ok"] += ok
        counts["skip"] += skip
        counts["fail"] += fail
        # ``download_many`` appends both lists under one lock, so their order
        # is parallel; pair each failed track with its own reason.
        for position, track in enumerate(failed):
            reason = pairs[position][1] if position < len(pairs) else ""
            failure_records.append((track, reason))
        base_done += group_len

    # 主来源失败时回退到同曲的其它平台。聚合搜索把备选来源挂在原始 Track
    # 的 ``_alternatives`` 上（进程内对象，不参与序列化）；单平台搜索没有
    # 备选，这一段自然跳过。
    if failure_records:
        index_of = {id(track): index for index, track in enumerate(tracks)}
        for failed_track, _reason in list(failure_records):
            alternatives = [
                alt
                for alt in getattr(failed_track, "_alternatives", ()) or ()
                if alt is not failed_track and alt.id
            ]
            if not alternatives:
                continue
            if should_stop and should_stop():
                break
            for alt in alternatives:
                provider = None
                if create_provider is not None and alt.provider:
                    try:
                        provider = create_provider(alt.provider)
                    except LoginExpired:
                        raise
                    except Exception:  # noqa: BLE001 - 换下一个来源
                        provider = None
                if provider is None:
                    continue
                try:
                    result = download_track(provider, alt, opts, stop_flag=should_stop)
                except LoginExpired:
                    raise
                except Exception:  # noqa: BLE001 - 换下一个来源
                    continue
                if result.status not in ("ok", "skip"):
                    continue
                counts["fail"] -= 1
                counts["ok" if result.status == "ok" else "skip"] += 1
                failed_track.path = result.path
                for position, (recorded, _recorded_reason) in enumerate(
                    failure_records
                ):
                    if recorded is failed_track:
                        del failure_records[position]
                        break
                index = index_of.get(id(failed_track), 0)
                deferred_failures.pop(index, None)
                if on_result:
                    on_result(index, failed_track, result)
                logger.info(
                    "music: %s failed on %s, downloaded from fallback %s",
                    failed_track.name,
                    failed_track.provider,
                    alt.provider,
                )
                break

    # 没被回退救回来的失败，现在补上唯一一次回调。
    if on_result:
        for index, (track, result) in sorted(deferred_failures.items()):
            on_result(index, track, result)

    failed_pairs = [
        (f"{track.name} - {track.artists}", reason) for track, reason in failure_records
    ]
    failed_tracks = [track for track, _reason in failure_records]

    if on_tick and total:
        # Finished a cancelled/partly-stopped batch? Report what was actually
        # processed instead of claiming 100%: the UI must not show complete
        # after the user hit stop.
        processed = counts["ok"] + counts["skip"] + counts["fail"]
        if processed >= total:
            on_tick(1.0, total)
        else:
            on_tick(min(1.0, processed / total), processed)
    return counts["ok"], counts["skip"], counts["fail"], failed_pairs, failed_tracks


def human(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


# ---------------------------------------------------------------------------
# 歌词落盘
# ---------------------------------------------------------------------------


#: 歌词是纯文本。超过这个大小的响应多半是错误页、完整网页或别的
#: 东西，原样落盘只会把播放器目录撑坏；直接跳过并告警。
MAX_LYRIC_BYTES = 1024 * 1024

#: Track / provider 私有数据里可能承载歌词的键。
_LYRIC_KEYS = ("lyric", "lyrics", "lrc", "lyric_text")
_LYRIC_TRANSLATION_KEYS = (
    "tlyric",
    "translated_lyric",
    "lyric_translation",
    "translation",
    "translated",
)
#: musicdl 的部分源会把「没有歌词」写成这些占位符，不能当成正文。
_EMPTY_LYRIC_VALUES = frozenset({"null", "none", "nil"})


def lyric_paths(audio_path):
    """返回某音频对应的 (原文 LRC 路径, 翻译 LRC 路径)。

    两个路径都直接由音频路径换后缀得到，因此与 ``<title> - <artist>.mp3``
    的落盘名天然一致：``<title> - <artist>.lrc`` / ``.zh.lrc``。
    """
    stem = os.path.splitext(str(audio_path))[0]
    return stem + ".lrc", stem + ".zh.lrc"


def _attr(value, name):
    """读取属性，属性 getter 抛异常时按不存在处理。"""
    try:
        return getattr(value, name, None)
    except Exception:  # noqa: BLE001 - provider 对象可能带会抛异常的 property
        return None


def _coerce_lyric(value, depth=0):
    """把各种歌词形态归一成纯文本 / LRC 字符串。

    平台返回值可能是字符串、``{"lrc": {"lyric": ...}}`` 这类嵌套 dict、
    行列表，或带 ``lyric`` 属性的 provider 对象。只沿已知歌词键向下找，
    并限制递归深度，避免顺着 provider 的 ``extra`` 无界遍历。
    """
    if value is None or depth > 4:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, Mapping):
        text = ""
        for key in _LYRIC_KEYS:
            text = _coerce_lyric(value.get(key), depth + 1)
            if text:
                break
    elif isinstance(value, (list, tuple)):
        text = "\n".join(
            item for item in (_coerce_lyric(v, depth + 1) for v in value) if item
        )
    else:
        text = ""
        for key in _LYRIC_KEYS:
            text = _coerce_lyric(_attr(value, key), depth + 1)
            if text:
                break
    if not text.strip() or text.strip().lower() in _EMPTY_LYRIC_VALUES:
        return ""
    return text


def _from_lyric_mapping(payload):
    """从一个 dict 形态的歌词载荷里取 (原文, 翻译)。"""
    original = ""
    for key in _LYRIC_KEYS:
        original = _coerce_lyric(payload.get(key))
        if original:
            break
    translated = ""
    for key in _LYRIC_TRANSLATION_KEYS:
        translated = _coerce_lyric(payload.get(key))
        if translated:
            break
    return original, translated


def _lyric_pair(value):
    """把任意歌词载荷归一成 (原文, 翻译)。"""
    if isinstance(value, Mapping):
        return _from_lyric_mapping(value)
    original = _coerce_lyric(value)
    translated = ""
    for key in _LYRIC_TRANSLATION_KEYS:
        translated = _coerce_lyric(_attr(value, key))
        if translated:
            break
    return original, translated


def _lyrics_from_extra(extra):
    """从 Track.extra 的常见层级里找歌词（不联网，只读已有字段）。

    ``extra`` 里可能是 provider 的原始对象（musicdl ``SongInfo``）或它的
    dict 形态（搜索/序列化往返后）。已知容器键有 ``info`` / ``raw`` /
    ``data`` / ``song`` / ``result``；找不到就返回空，绝不猜测。
    """
    if not isinstance(extra, Mapping):
        # 极少数调用方会把 provider 对象直接放进 extra。
        return _lyric_pair(extra)
    original, translated = _from_lyric_mapping(extra)
    if original and translated:
        return original, translated
    for key in ("info", "raw", "data", "song", "result"):
        node = extra.get(key)
        if node is None:
            continue
        node_original, node_translated = _lyric_pair(node)
        original = original or node_original
        translated = translated or node_translated
        if original and translated:
            break
    return original, translated


def track_lyrics(track):
    """从 Track 自带字段提取 (原文, 翻译)，没有歌词时返回 ("", "")。

    下载流程优先用 provider.lyric()（可能联网）；本函数只读已在对象上的
    ``lyric`` / ``lyrics`` / ``extra``，用作回退，避免重复请求。既接受
    纯文本，也原样保留 LRC 的时间轴标签。
    """
    original, translated = _lyric_pair(_attr(track, "lyric"))
    if not original:
        lyrics_original, lyrics_translated = _lyric_pair(_attr(track, "lyrics"))
        original = original or lyrics_original
        translated = translated or lyrics_translated
    if not translated:
        for key in _LYRIC_TRANSLATION_KEYS:
            translated = _coerce_lyric(_attr(track, key))
            if translated:
                break
    extra_original, extra_translated = _lyrics_from_extra(_attr(track, "extra"))
    return original or extra_original, translated or extra_translated


def _write_text(path, text):
    """原子写入 UTF-8 文本，返回是否真的产生写盘。

    - 目标已存在且字节完全一致：直接跳过，避免重复下载时反复改 mtime
    - 先写同目录临时文件，``os.replace`` 原子替换；失败时删除临时文件，
      原有歌词保持完整
    - 超过 :data:`MAX_LYRIC_BYTES` 的内容不写（抛 ``ValueError``，由
      :func:`save_lyric` 记日志后忽略）
    """
    data = (text.rstrip() + "\n").encode("utf-8")
    if len(data) > MAX_LYRIC_BYTES:
        raise ValueError(f"歌词超过 {MAX_LYRIC_BYTES} 字节上限")

    try:
        with open(path, "rb") as f:
            if f.read(len(data) + 1) == data:
                return False
    except OSError:
        pass

    directory = os.path.dirname(os.path.abspath(path)) or os.curdir
    os.makedirs(directory, exist_ok=True)
    base = os.path.basename(path)
    # ``mkstemp`` would create the file as 0600 on POSIX; opening with
    # ``0o666`` lets the umask decide, matching a plain ``open(path, "w")``.
    fd = -1
    tmp_path = ""
    for _ in range(8):
        tmp_path = os.path.join(directory, f".{base}.{secrets.token_hex(6)}.tmp")
        try:
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            continue
        break
    else:  # pragma: no cover - eight random collisions are not realistic
        raise FileExistsError(
            errno.EEXIST, "Could not create a temporary lyric file", path
        )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
    return True


def _normalize_lrc(text):
    """LRC 文本统一换行为 ``\\n``（部分来源是 CRLF，很多播放器解析不了）。"""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def save_lyric(audio_path, lyric, translated=""):
    """把歌词写到音频旁，返回实际产生写盘的文件列表。

    落盘策略（固定为独立文件，便于播放器/工具各取所需）：

    - 有原文：``<stem>.lrc`` 写原文；若另有译文，``<stem>.zh.lrc`` 写译文
    - 只有译文：把译文写进 ``<stem>.lrc``，避免播放器完全看不到歌词
    - 两者都为空：不创建任何文件

    空白文本会被忽略；换行统一为 ``\\n``，文件编码 UTF-8（无 BOM）。
    内容与现有文件相同则跳过；不同则原子覆盖。单个文件写入失败只记
    WARNING 并继续处理另一个文件，绝不把已下载的音频变成失败。
    """
    original_path, translation_path = lyric_paths(audio_path)
    text = _normalize_lrc(lyric)
    translation = _normalize_lrc(translated)
    pending = []
    if text:
        pending.append((original_path, text))
        if translation:
            pending.append((translation_path, translation))
    elif translation:
        pending.append((original_path, translation))

    written = []
    for path, content in pending:
        try:
            if _write_text(path, content):
                written.append(path)
        except Exception as exc:  # noqa: BLE001 - 歌词失败不影响音频
            logger.warning("music: 歌词保存失败 %s: %s", path, exc)
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
    """生成写入 M3U 的路径：默认相对歌单所在目录，统一用正斜杠。

    M3U 用 ``/`` 作目录分隔符，而 Windows 的播放器也接受它，所以只在与
    ``os.sep`` 不同时才替换：POSIX 上反斜杠是合法文件名字符，不能被改写。
    """
    if absolute:
        location = os.path.abspath(file_path)
    else:
        try:
            location = os.path.relpath(os.path.abspath(file_path), playlist_dir)
        except ValueError:
            # Windows 跨盘符没有相对路径，退回绝对路径
            location = os.path.abspath(file_path)
    if os.sep != "/":
        location = location.replace(os.sep, "/")
    location = _CONTROL.sub(" ", location).strip()
    # 「#」开头的行会被 M3U 当作注释/指令，相对路径前加 ./ 规避
    if not absolute and location.startswith("#"):
        location = "./" + location
    return location


def _playlist_encoding(playlist_path, text):
    """选择歌单文件的编码。

    ``.m3u8`` 的定义就是 UTF-8，不加 BOM，避免严格解析器把 BOM 当成路径的
    一部分。``.m3u`` 没有编码声明：纯 ASCII 时保持原样，含非 ASCII 时写
    UTF-8 BOM——否则 Windows 上按 ANSI（如 GBK）解码的播放器会把中文标题和
    路径显示成乱码。未知后缀（CLI 先写 ``*.tmp`` 再改名）按 UTF-8 处理。
    """
    suffix = os.path.splitext(str(playlist_path))[1].lower()
    if suffix == ".m3u" and not text.isascii():
        return "utf-8-sig"
    return "utf-8"


def _write_playlist_text(playlist_path, text):
    """原子写入歌单：先写同目录临时文件，再 ``os.replace``。

    直接 ``open(path, "w")`` 会先截断目标文件；磁盘满或写入中断时，用户原有
    的歌单就被毁掉了。临时文件放在同一目录，保证 ``os.replace`` 是原子改名，
    并且 ``export_m3u`` 计算出的相对路径仍然以歌单目录为基准。
    """
    if os.path.isdir(playlist_path):
        # 各平台 open() 对目录报的错不同（Windows 是 PermissionError），
        # 统一成 IsADirectoryError，调用方才能稳定区分。
        raise IsADirectoryError(errno.EISDIR, "Is a directory", playlist_path)

    directory = os.path.dirname(os.path.abspath(playlist_path)) or os.curdir
    os.makedirs(directory, exist_ok=True)
    encoding = _playlist_encoding(playlist_path, text)
    # ``mkstemp`` would create the file as 0600 on POSIX; opening with
    # ``0o666`` lets the umask decide, so the playlist keeps the permissions a
    # plain ``open(path, "w")`` used to give it.
    prefix = f".{os.path.basename(playlist_path)}."
    fd = -1
    temp_path = ""
    for _ in range(8):
        temp_path = os.path.join(directory, f"{prefix}{secrets.token_hex(6)}.tmp")
        try:
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            continue
        break
    else:  # pragma: no cover - eight random collisions are not realistic
        raise FileExistsError(
            errno.EEXIST, "Could not create a temporary playlist file", playlist_path
        )
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
        os.replace(temp_path, playlist_path)
    except BaseException:
        # 失败时绝不留下半个歌单或临时文件。
        with contextlib.suppress(OSError):
            os.unlink(temp_path)
        raise


def export_m3u(tracks, path, *, absolute=False):
    """把曲目列表导出为 M3U8 歌单，返回歌单文件路径。

    ``tracks`` 中每项可以是 ``Track``（需已由下载流程回填 ``.path``）、
    ``(track, file_path)`` 元组，或包含 ``path``/``file`` 的 dict。
    ``absolute=False``（默认）写入相对歌单目录的路径，便于整个目录搬家。
    列表为空时也会写入只有 ``#EXTM3U`` 头的合法歌单。

    同一个文件（按 ``normcase(abspath(...))`` 判断）只写一条，保留第一次出现
    的顺序。写入是原子的：失败不会毁掉已有的同名歌单。
    """
    playlist_path = str(path)
    playlist_dir = os.path.dirname(os.path.abspath(playlist_path))
    entries = []
    seen = set()
    for item in tracks or ():
        track, file_path = _playlist_entry(item)
        try:
            identity = os.path.normcase(os.path.abspath(file_path))
        except (OSError, ValueError):
            identity = ""
        if identity:
            if identity in seen:
                continue
            seen.add(identity)
        name = _CONTROL.sub(
            " ", str(_playlist_field(track, "name", "title", default="未知歌曲"))
        ).strip()
        if not name:
            # ``name: ""`` on a submitted track is not a title: use the same
            # placeholder a freshly built Track gets, so the EXTINF line never
            # ends as "歌手 - " (some players reject that entry).
            name = "未知歌曲"
        artists = _CONTROL.sub(
            " ", str(_playlist_field(track, "artists", "artist", default="未知歌手"))
        ).strip()
        if not artists:
            artists = "未知歌手"
        duration = _playlist_field(track, "duration_s", "duration", default=0)
        try:
            duration = int(float(duration or 0))
        except (TypeError, ValueError, OverflowError):
            # ``float("inf")`` raises OverflowError, not ValueError; it is as
            # unusable as a non-numeric duration.
            duration = 0
        if duration <= 0:
            duration = -1  # M3U 用 -1 表示时长未知
        location = _playlist_location(file_path, playlist_dir, absolute)
        entries.append(f"#EXTINF:{duration},{artists} - {name}\n{location}")

    text = "#EXTM3U\n" + "".join(entry + "\n" for entry in entries)
    _write_playlist_text(playlist_path, text)
    return playlist_path
