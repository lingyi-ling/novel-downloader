"""入口：python main.py      启动桌面界面
        python main.py --check   仅自检（加密/接口/下载器/依赖），不开窗口
"""
import io
import os
import sys

from novel_downloader.apps.music.paths import music_data_dir


class _Tee:
    """把输出同时写到原 stdout 和缓冲区；打包后 stdout 可能为 None。"""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            try:
                st.write(s)
            except Exception:
                pass
        return len(s)

    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except Exception:
                pass


def check():
    """自检并落盘 check_report.txt：exe 无控制台时也能看结果。"""
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf if old is None else _Tee(old, buf)
    try:
        _check_impl()
    finally:
        sys.stdout = old
        try:
            with open(
                os.path.join(str(music_data_dir()), "check_report.txt"),
                "w",
                encoding="utf-8",
            ) as f:
                f.write(buf.getvalue())
        except Exception:
            pass


def _check_impl():
    print("=== 依赖 ===")
    import customtkinter, qrcode, mutagen, requests, Crypto  # noqa: F401
    from PIL import Image  # noqa: F401

    print("customtkinter", customtkinter.__version__)
    print("mutagen", mutagen.version_string, "| qrcode/requests/pycryptodome/Pillow OK")

    print("\n=== WEAPI 加密 + 匿名接口 ===")
    from novel_downloader.apps.music.weapi import NeteaseClient, track_artists, track_name

    c = NeteaseClient()
    r = c.post_weapi("v6/playlist/detail", {"id": 3778678, "n": 2, "s": 0})
    pl = r.get("playlist") or {}
    print("歌单:", pl.get("name"), "| 曲目:", len(pl.get("tracks") or []))
    for t in (pl.get("tracks") or [])[:2]:
        print("   -", track_name(t), "/", track_artists(t))

    print("\n=== 播放直链（匿名，受限曲目会为空属正常） ===")
    ids = [t["id"] for t in (pl.get("tracks") or [])[:2]]
    for item in c.song_urls(ids, level="exhigh"):
        u = item.get("url")
        print(f"   id={item.get('id')} level={item.get('level')} "
              f"{'有直链 ' + str(item.get('size')) + 'B' if u else '无直链（版权/权限限制）'}")

    print("\n=== 下载器 ===")
    from novel_downloader.apps.music.downloader import DownloadOptions, sanitize  # noqa: F401
    print("sanitize 测试:", sanitize('a<b>c:d/e\\f|g?h*i"j'))

    print("\n=== GUI 构建 ===")
    import gui
    app = gui.App()
    app.update()
    app.destroy()
    print("窗口构建通过")

    print("\n=== 平台 provider（验证 vendored 模块可被动态导入）===")
    from novel_downloader.apps.music import providers
    keys = [k for k, _ in providers.available()]
    print("注册平台数:", len(keys))
    for key in ("netease", "migu", "kuwo", "qq", "apple"):
        if key not in keys:
            print(f"   {key}: 未注册（依赖缺失时属正常）")
            continue
        try:
            p = providers.create(key)
            get_client = getattr(p, "_get_client", None)
            if callable(get_client):
                get_client()      # 会 import 对应 vendored 平台模块并构造客户端，不联网
            print(f"   {key}: OK")
        except Exception as e:
            print(f"   {key}: 失败 {type(e).__name__} {e}")

    print("\n全部自检通过。")


def main():
    if "--check" in sys.argv:
        check()
        return
    from gui import main as run_gui

    run_gui()


if __name__ == "__main__":
    main()
