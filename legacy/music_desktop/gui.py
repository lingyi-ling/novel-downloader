"""网易云音乐下载器 · 桌面界面。

布局：顶栏(账号/扫码/一键下收藏夹) / 左侧歌单 / 右侧歌曲(可多选) / 底部设置+进度+日志

v1.0 关键决策：
- 歌曲列表用原生 Listbox 而非 CTkCheckBox：上千首时勾选框会卡死，Listbox 支持
  扩展多选（Ctrl/Shift/全选）且性能无关条目数量
- 所有网络请求放后台线程，通过 self.after 回主线程更新 UI（Tk 非线程安全）
- 工作线程不直接调用 after()，而是投递到队列由主线程定时抽取执行：after 内部
  会触碰 Tcl 解释器，跨线程调用在 _tkinter 里有 1 秒超时，长时间下载时可能抛
  "main thread is not in main loop" 让线程静默死掉
"""
import io
import json
import os
import queue
import threading
import time
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox

import customtkinter as ctk
import requests
from PIL import Image

from novel_downloader.apps.music import browser_login
from novel_downloader.apps.music.downloader import (DownloadOptions, download_many, human)
from novel_downloader.apps.music import providers
from novel_downloader.apps.music.providers import create as create_provider
from novel_downloader.apps.music.weapi import LEVELS, LoginExpired
from novel_downloader.apps.music.paths import music_data_dir

APP_DIR = str(music_data_dir())
COOKIE_FILE = os.path.join(APP_DIR, "session.json")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
BROWSER_PROFILE = os.path.join(APP_DIR, "browser_profile")

# 界面配色：浅色、白底圆角，克制不花哨
BG = "#f4f5f7"
CARD = "#ffffff"
TXT = "#1f2329"
MUTED = "#8a9099"
ACCENT = "#3b82f6"
SEL_BG = "#d8e6ff"


def load_settings():
    default = {
        "out_dir": os.path.join(os.path.expanduser("~"), "Music", "网易云下载"),
        "level": "exhigh",
        "workers": 3,
        "retries": 2,
        "embed_tags": True,
        "embed_cover": True,
        "save_lyric": True,
        "split_folder": True,
        "skip_existing": True,
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                default.update(json.load(f))
        except Exception:
            pass
    return default


def save_settings(data):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def fmt_dur(sec):
    return f"{sec // 60:02d}:{sec % 60:02d}"


class UiPump:
    """Tk 不是线程安全的：工作线程只往队列投递，主线程定时取出执行。"""

    def _start_ui_pump(self, interval=30):
        self._ui_q = queue.Queue()
        self._pump_ui(interval)

    def _pump_ui(self, interval):
        try:
            while True:
                fn, a, kw = self._ui_q.get_nowait()
                try:
                    fn(*a, **kw)
                except Exception:
                    # 单个回调出错不拖垮界面循环，但必须留痕，否则问题会被静默吞掉
                    traceback.print_exc()
        except queue.Empty:
            pass
        self.after(interval, lambda: self._pump_ui(interval))

    def ui(self, fn, *a, **kw):
        self._ui_q.put((fn, a, kw))


class LoginDialog(UiPump, ctk.CTkToplevel):
    """登录窗口：自动拉起浏览器，用户正常登录后自动读取登录态。

    背景：网易云对第三方扫码登录启用了行为验证风控（8821），扫码不可用；
    让用户翻 F12 复制 Cookie 门槛又太高。折中方案是——我们起一个浏览器让
    用户用平时的方式正常登录，再用 CDP 把（含 httpOnly 的）登录态取回来。
    """

    def __init__(self, master, client, on_success, profile_dir):
        super().__init__(master)
        self.client = client
        self.on_success = on_success
        self.profile_dir = profile_dir
        self.bl = None
        self.alive = True
        self._stop = False
        self._manual_shown = False

        self.title("登录网易云音乐")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.transient(master)

        self.wrap = ctk.CTkFrame(self, fg_color="transparent")
        self.wrap.pack(fill="both", expand=True, padx=18, pady=16)

        ctk.CTkLabel(
            self.wrap, justify="left", anchor="w", text_color=TXT,
            font=ctk.CTkFont(size=12),
            text=("正在为你打开一个浏览器窗口。\n"
                  "请在那里用你平时的方式登录网易云（扫码、手机号都行），\n"
                  "登录完成后本工具会自动读取登录态，不需要任何手动操作。"),
        ).pack(anchor="w")

        self.status = ctk.CTkLabel(self.wrap, text="", text_color=MUTED,
                                   font=ctk.CTkFont(size=12), wraplength=460,
                                   justify="left", anchor="w")
        self.status.pack(fill="x", pady=(12, 12))

        btns = ctk.CTkFrame(self.wrap, fg_color="transparent")
        btns.pack(fill="x")
        ctk.CTkButton(btns, text="取消", width=90, height=32, fg_color="#eceef1",
                      hover_color="#e0e3e8", text_color=TXT,
                      command=self.close).pack(side="right")
        ctk.CTkButton(btns, text="浏览器打不开？手动粘贴", width=180, height=32,
                      fg_color="#eceef1", hover_color="#e0e3e8", text_color=TXT,
                      command=self.toggle_manual).pack(side="left")

        # 手动粘贴兜底（默认隐藏）
        self.manual = ctk.CTkFrame(self.wrap, fg_color="transparent")
        self.manual_entry = ctk.CTkTextbox(self.manual, height=80, fg_color="#fafbfc",
                                           text_color=TXT, font=ctk.CTkFont(size=11),
                                           wrap="word")
        self.manual_entry.pack(fill="x", pady=(8, 8))
        ctk.CTkButton(self.manual, text="用手动 Cookie 登录", width=160, height=30,
                      fg_color=ACCENT, hover_color="#2f6fd0",
                      command=self.do_manual).pack(anchor="e")

        self.protocol("WM_DELETE_WINDOW", self.close)
        self._center_over(master)
        self.lift()
        self.attributes("-topmost", True)
        self.focus_force()
        self._topmost_job = self.after(500, self._release_topmost)
        self._start_ui_pump()
        threading.Thread(target=self._run, daemon=True).start()

    # ---------- 窗口定位 ----------

    def _set_status(self, text, color=TXT):
        if self.alive:
            self.status.configure(text=text, text_color=color)

    def _center_over(self, master):
        """摆到主窗口正中央并夹在屏幕内，避免开在角落或被主窗口盖住。"""
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        x = master.winfo_rootx() + max(0, (master.winfo_width() - w) // 2)
        y = master.winfo_rooty() + max(0, (master.winfo_height() - h) // 2)
        x = max(0, min(x, self.winfo_screenwidth() - w))
        y = max(0, min(y, self.winfo_screenheight() - h))
        self.geometry(f"+{x}+{y}")

    def _release_topmost(self):
        if self.alive:
            try:
                self.attributes("-topmost", False)
            except Exception:
                pass

    # ---------- 浏览器流程 ----------

    def _status_cb(self, text, highlight):
        self.ui(self._set_status, text, ACCENT if highlight else MUTED)

    def _run(self):
        try:
            self.bl = browser_login.BrowserLogin(self.profile_dir)
            self.ui(self._set_status, "正在打开浏览器…", MUTED)
            self.bl.start()
            self.bl.open_login_page()
            self.ui(self._set_status,
                    "已打开浏览器，请在其中登录网易云，登录成功后会自动继续。", ACCENT)
            ck = self.bl.wait_for_login(
                timeout=240,
                on_status=self._status_cb,
                should_stop=lambda: self._stop,
            )
        except Exception as e:
            self._cleanup_browser()
            self.ui(self._fail, f"{e}")
            return
        self._cleanup_browser()
        if self._stop or not self.alive:
            return
        self.ui(self._set_status, "已读取到登录态，正在校验…", MUTED)
        try:
            self.client.login_by_cookie(browser_login.to_cookie_string(ck))
        except Exception as e:
            self.ui(self._fail, f"校验失败：{e}")
            return
        self.ui(self._done)

    def _cleanup_browser(self):
        bl, self.bl = self.bl, None
        if bl:
            try:
                bl.close()
            except Exception:
                pass

    def _fail(self, msg):
        if self.alive:
            self._set_status(msg, "#e5484d")

    def _done(self):
        if not self.alive:
            return
        self._set_status("登录成功", "#12a150")
        self.after(500, self._finish)

    def _finish(self):
        self.close()
        self.on_success()

    # ---------- 手动兜底 ----------

    def toggle_manual(self):
        self._manual_shown = not self._manual_shown
        if self._manual_shown:
            self.manual.pack(fill="x", pady=(10, 0))
            self.manual_entry.focus_set()
        else:
            self.manual.pack_forget()

    def do_manual(self):
        text = self.manual_entry.get("1.0", "end").strip()
        if not text:
            self._set_status("请先粘贴 Cookie", "#e5484d")
            return
        self._set_status("正在校验…", MUTED)
        threading.Thread(target=self._manual_work, args=(text,), daemon=True).start()

    def _manual_work(self, text):
        try:
            self.client.login_by_cookie(text)
        except Exception as e:
            self.ui(self._fail, f"登录失败：{e}")
            return
        self._stop = True
        self.ui(self._done)

    def close(self):
        self.alive = False
        self._stop = True
        self._cleanup_browser()
        job = getattr(self, "_topmost_job", None)
        if job:
            try:
                self.after_cancel(job)
            except Exception:
                pass
        self.destroy()


def parse_creds(text):
    """把用户输入解析成 dict：优先 JSON 对象，否则按 Cookie 串解析。"""
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        obj = json.loads(text)
        if not isinstance(obj, dict):
            raise ValueError("需要 JSON 对象")
        return obj
    out = {}
    for part in text.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                out[k.strip()] = v.strip()
    if not out:
        raise ValueError("无法解析为 JSON 或 Cookie 串")
    return out


class CredsDialog(UiPump, ctk.CTkToplevel):
    """平台凭据配置：粘贴 JSON 对象或 Cookie 串，保存到 platform_creds.json。"""

    def __init__(self, master, provider, on_saved):
        super().__init__(master)
        self.provider = provider
        self.on_saved = on_saved
        self.title(f"平台凭据 · {provider.display_name}")
        self.geometry("580x430")
        self.minsize(520, 380)
        self.configure(fg_color=BG)
        self.transient(master)

        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=16, pady=14)

        ctk.CTkLabel(
            wrap, justify="left", anchor="w", text_color=TXT, font=ctk.CTkFont(size=12),
            wraplength=530,
            text=("粘贴该平台的账号凭据（JSON 对象），或直接粘贴 Cookie 串（形如 a=1; b=2）。\n"
                  "各平台需要的字段不同：\n"
                  "  · TIDAL：token JSON（access_token / refresh_token / user_id / country_code / client_id / client_secret）\n"
                  "  · 汽水音乐：{\"cookies\": { ... }} 形式\n"
                  "  · QQ音乐 等：Cookie 串即可（用于获取更高音质）\n"
                  "留空后保存 = 清除该平台凭据。"),
        ).pack(anchor="w")

        note = providers.platform_note(provider.key)
        if note:
            ctk.CTkLabel(wrap, text="提示：" + note, text_color=MUTED,
                         font=ctk.CTkFont(size=11), wraplength=530, justify="left",
                         anchor="w").pack(anchor="w", pady=(6, 0))

        self.box = ctk.CTkTextbox(wrap, fg_color="#fafbfc", text_color=TXT,
                                  font=ctk.CTkFont(size=11), wrap="word")
        self.box.pack(fill="both", expand=True, pady=(10, 10))
        cur = providers.creds.get(provider.key)
        if cur:
            self.box.insert("1.0", json.dumps(cur, ensure_ascii=False, indent=2))

        btns = ctk.CTkFrame(wrap, fg_color="transparent")
        btns.pack(fill="x")
        ctk.CTkButton(btns, text="取消", width=80, height=30, fg_color="#eceef1",
                      hover_color="#e0e3e8", text_color=TXT,
                      command=self.destroy).pack(side="right")
        ctk.CTkButton(btns, text="保存", width=90, height=30, fg_color=ACCENT,
                      hover_color="#2f6fd0", command=self.save).pack(side="right", padx=8)
        self.status = ctk.CTkLabel(btns, text="", text_color=MUTED, font=ctk.CTkFont(size=11))
        self.status.pack(side="left")

        self.lift()
        self.attributes("-topmost", True)
        self.after(400, lambda: self.attributes("-topmost", False))
        self.focus_force()

    def save(self):
        text = self.box.get("1.0", "end").strip()
        try:
            data = parse_creds(text)
        except Exception as e:
            self.status.configure(text=f"解析失败：{e}", text_color="#e5484d")
            return
        try:
            providers.creds.set(self.provider.key, data)
        except Exception as e:
            self.status.configure(text=f"保存失败：{e}", text_color="#e5484d")
            return
        self.destroy()
        self.on_saved()


class App(UiPump, ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("light")
        self.title("音乐下载器")
        self.geometry("1140x780")
        self.minsize(980, 660)
        self.configure(fg_color=BG)

        self.cfg = load_settings()
        self.provider = create_provider("netease", cookie_file=COOKIE_FILE)
        self._plat_list = providers.available_labels()
        self._key_by_label = {label: k for k, label in self._plat_list}
        self._label_by_key = {k: label for k, label in self._plat_list}
        self._platform_epoch = 0
        self._search_seq = 0      # 每次新搜索 +1，旧结果按序号丢弃
        self._pending = 0         # 在途搜索数，用于按钮忙碌态
        self._uid = None
        self.profile = None
        self.playlists = []
        self.all_tracks = []       # 当前歌单的全部曲目
        self.tracks = []           # 当前显示的曲目（搜索过滤后）
        self._failed_tracks = []   # 上次失败的曲目，供「重试失败项」使用
        self._search_job = None
        self._render_job = None    # 分批渲染的定时器
        self._render_pos = 0
        self._select_all_pending = False
        self._search_kw = ""       # 当前云端搜索词
        self._search_total = 0
        self._stop = False
        self._downloading = False
        self._avatar_ref = None

        self._build_topbar()
        self._build_body()
        self._build_footer()
        self._start_ui_pump()
        self.after(60, self._center_window)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.after(200, self._auto_login)

    # ---------- 界面搭建 ----------

    def _build_topbar(self):
        bar = ctk.CTkFrame(self, fg_color=CARD, corner_radius=12, height=68)
        bar.pack(fill="x", padx=14, pady=(14, 8))
        bar.pack_propagate(False)

        self.avatar = ctk.CTkLabel(bar, text="", width=44, height=44, corner_radius=22,
                                   fg_color="#e8eaed")
        self.avatar.pack(side="left", padx=(16, 10), pady=12)

        box = ctk.CTkFrame(bar, fg_color="transparent")
        box.pack(side="left", pady=12)
        self.name_label = ctk.CTkLabel(box, text="未登录", text_color=TXT,
                                       font=ctk.CTkFont(size=15, weight="bold"), anchor="w")
        self.name_label.pack(anchor="w")
        self.sub_label = ctk.CTkLabel(box, text="请先扫码登录", text_color=MUTED,
                                      font=ctk.CTkFont(size=12), anchor="w")
        self.sub_label.pack(anchor="w")

        self.platform_menu = ctk.CTkOptionMenu(
            bar, values=[label for _k, label in self._plat_list], width=162, height=32,
            fg_color="#eceef1", button_color="#dfe3e8", text_color=TXT,
            font=ctk.CTkFont(size=12), command=self.on_platform_change,
        )
        self.platform_menu.pack(side="left", padx=(20, 8))
        self._select_platform_menu()

        self.search_cloud = ctk.CTkEntry(bar, width=220, height=32,
                                         placeholder_text="搜索网易云歌曲，回车即搜",
                                         font=ctk.CTkFont(size=12))
        self.search_cloud.pack(side="left", padx=(0, 6))
        self.search_cloud.bind("<Return>", self.on_cloud_search)
        self.search_btn = ctk.CTkButton(bar, text="搜索", width=70, height=32,
                                        fg_color="#eceef1", hover_color="#e0e3e8",
                                        text_color=TXT, command=self._on_search_btn)
        self.search_btn.pack(side="left")

        self.liked_btn = ctk.CTkButton(bar, text="一键下载我喜欢的音乐", width=170, height=34,
                                       fg_color="#12a150", hover_color="#0e8442",
                                       command=self.on_click_liked, state="disabled")
        self.liked_btn.pack(side="right", padx=(8, 16))

        self.login_btn = ctk.CTkButton(bar, text="Cookie 登录", width=110, height=34,
                                       fg_color=ACCENT, hover_color="#2f6fd0",
                                       command=self.on_click_login)
        self.login_btn.pack(side="right")

    def _platform_label(self, key):
        return self._label_by_key.get(key, key)

    def _select_platform_menu(self):
        """把下拉显示同步到当前 provider（程序化切换时用）。"""
        self.platform_menu.set(self._platform_label(self.provider.key))

    def _ui_if_current(self, epoch, fn, *args, **kwargs):
        """仅在仍处于同一平台时才把异步结果投递回界面。

        否则切平台后，上一个平台还在飞的请求结果会把列表回填成旧数据。
        """
        if epoch == self._platform_epoch:
            self.ui(fn, *args, **kwargs)

    def _search_fresh(self, seq, epoch):
        """该次搜索结果是否仍然有效（平台未切、且没有发起更新的搜索）。"""
        return seq == self._search_seq and epoch == self._platform_epoch

    def on_platform_change(self, display):
        """切换平台：重建 provider 并复位界面。

        网易云支持登录与歌单，其它平台只支持搜索下载，故相关控件隐藏/置灰。
        """
        key = self._key_by_label.get(display)
        if not key or key == self.provider.key:
            return
        if self._downloading:
            messagebox.showinfo("提示", "下载进行中，暂不能切换平台。")
            self._select_platform_menu()
            return
        try:
            new_provider = create_provider(key)
        except Exception as e:
            messagebox.showerror("提示", f"加载平台失败：{e}")
            self._select_platform_menu()
            return
        self.provider = new_provider
        self._reset_for_platform()

    def _reset_for_platform(self):
        p = self.provider
        self._platform_epoch += 1
        self._search_seq += 1
        self._pending = 0
        self._set_search_busy(False)
        self._search_kw = ""
        self._failed_tracks = []
        self.playlists = []
        self.all_tracks = []
        self.tracks = []
        self._search_total = 0
        self.pl_list.delete(0, "end")
        self.tk_list.delete(0, "end")
        self.pl_count.configure(text="")
        self.tk_count.configure(text="")
        self.tk_title.configure(text="歌曲")
        self.more_btn.pack_forget()
        self.search_var.set("")
        self.search_cloud.configure(placeholder_text=f"搜索{p.display_name}歌曲，回车即搜")
        is_ne = (p.key == "netease")
        # 歌单 / 登录 / 一键下载 / 音质 都是网易云专属；其它平台直接收起或置灰，
        # 避免界面留着一堆用不了的控件
        if is_ne:
            self.body.grid_columnconfigure(0, minsize=280)
            self.left_panel.grid()
            self.liked_btn.pack(side="right", padx=(8, 16))
            self.login_btn.pack(side="right")
            self.level_menu.configure(state="normal")
            self.level_label.configure(text="音质")
            nick = (self.profile or {}).get("nickname") or "未登录"
            self.name_label.configure(text=nick)
            self.sub_label.configure(text=f"uid {self._uid}" if self._uid else "请先登录")
        else:
            self.left_panel.grid_remove()
            self.body.grid_columnconfigure(0, minsize=0)
            self.liked_btn.pack_forget()
            self.login_btn.pack_forget()
            self.level_menu.configure(state="disabled")
            self.level_label.configure(text="音质(源定)")
            self.name_label.configure(text=p.display_name)
            self.sub_label.configure(text="无需登录，直接搜索下载")
        self.creds_btn.configure(state="disabled" if is_ne else "normal")
        self.log_line(f"已切换平台：{p.display_name}")
        note = providers.platform_note(p.key)
        if note:
            self.log_line("提示：" + note)
        if is_ne and p.is_logged_in() and self._uid:
            threading.Thread(target=self._load_playlists, args=(self._uid,), daemon=True).start()

    def _build_body(self):
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=14, pady=4)
        self.body.grid_columnconfigure(0, weight=0, minsize=280)
        self.body.grid_columnconfigure(1, weight=1)
        self.body.grid_rowconfigure(0, weight=1)

        # 左：歌单
        left = ctk.CTkFrame(self.body, fg_color=CARD, corner_radius=12)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.left_panel = left
        head = ctk.CTkFrame(left, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(12, 6))
        ctk.CTkLabel(head, text="我的歌单", text_color=TXT,
                     font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")
        self.pl_count = ctk.CTkLabel(head, text="", text_color=MUTED, font=ctk.CTkFont(size=12))
        self.pl_count.pack(side="right")

        self.pl_list = self._make_listbox(left, selectmode="browse")
        self.pl_list.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.pl_list.bind("<<ListboxSelect>>", self.on_pick_playlist)

        # 右：歌曲
        right = ctk.CTkFrame(self.body, fg_color=CARD, corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew")
        head2 = ctk.CTkFrame(right, fg_color="transparent")
        head2.pack(fill="x", padx=14, pady=(12, 6))
        self.tk_title = ctk.CTkLabel(head2, text="歌曲", text_color=TXT,
                                     font=ctk.CTkFont(size=14, weight="bold"))
        self.tk_title.pack(side="left")
        self.tk_count = ctk.CTkLabel(head2, text="", text_color=MUTED, font=ctk.CTkFont(size=12))
        self.tk_count.pack(side="right")
        ctk.CTkButton(head2, text="全选", width=64, height=26, fg_color="#eceef1",
                      hover_color="#e0e3e8", text_color=TXT,
                      command=lambda: self.tk_list.selection_set(0, "end")).pack(side="right", padx=6)
        ctk.CTkButton(head2, text="清空选择", width=76, height=26, fg_color="#eceef1",
                      hover_color="#e0e3e8", text_color=TXT,
                      command=lambda: self.tk_list.selection_clear(0, "end")).pack(side="right")

        # 搜索框：上千首的歌单里翻找太痛苦，输入即过滤
        self.search_var = tk.StringVar()
        self.search_entry = ctk.CTkEntry(head2, width=150, height=26,
                                         placeholder_text="筛选当前列表",
                                         textvariable=self.search_var,
                                         font=ctk.CTkFont(size=12))
        self.search_entry.pack(side="right", padx=6)
        self.search_var.trace_add("write", self._on_search)
        # 云端搜索结果没取完时才出现
        self.more_btn = ctk.CTkButton(head2, text="加载更多", width=84, height=26,
                                      fg_color=ACCENT, hover_color="#2f6fd0",
                                      command=self.on_click_more)

        self.tk_list = self._make_listbox(right, selectmode="extended")
        self.tk_list.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def _make_listbox(self, parent, selectmode):
        lb = tk.Listbox(
            parent, selectmode=selectmode, activestyle="none",
            bg=CARD, fg=TXT, selectbackground=SEL_BG, selectforeground=TXT,
            highlightthickness=0, bd=0, relief="flat",
            font=("Microsoft YaHei UI", 10), exportselection=False,
        )
        return lb

    def _build_footer(self):
        foot = ctk.CTkFrame(self, fg_color=CARD, corner_radius=12)
        foot.pack(fill="x", padx=14, pady=8)

        row = ctk.CTkFrame(foot, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(12, 6))

        self.level_label = ctk.CTkLabel(row, text="音质", text_color=MUTED,
                                        font=ctk.CTkFont(size=12))
        self.level_label.pack(side="left")
        self.level_menu = ctk.CTkOptionMenu(
            row, values=[f"{k} {v}" for k, v in LEVELS.items()], width=110, height=28,
            fg_color="#eceef1", button_color="#dfe3e8", text_color=TXT,
            command=self.on_level_change,
        )
        self.level_menu.pack(side="left", padx=(6, 14))
        cur = next((f"{k} {v}" for k, v in LEVELS.items() if k == self.cfg["level"]), None)
        if cur:
            self.level_menu.set(cur)

        ctk.CTkLabel(row, text="并发", text_color=MUTED,
                     font=ctk.CTkFont(size=12)).pack(side="left")
        self.workers_menu = ctk.CTkOptionMenu(
            row, values=["1", "2", "3", "4"], width=64, height=28,
            fg_color="#eceef1", button_color="#dfe3e8", text_color=TXT,
            command=self.on_workers_change,
        )
        self.workers_menu.pack(side="left", padx=(6, 14))
        self.workers_menu.set(str(self.cfg.get("workers", 3)))

        self.dir_label = ctk.CTkLabel(row, text=self._short_dir(), text_color=MUTED,
                                      font=ctk.CTkFont(size=12))
        self.dir_label.pack(side="left", padx=(4, 8))
        ctk.CTkButton(row, text="选择目录", width=80, height=28, fg_color="#eceef1",
                      hover_color="#e0e3e8", text_color=TXT,
                      command=self.on_pick_dir).pack(side="left")
        self.creds_btn = ctk.CTkButton(row, text="平台凭据", width=84, height=28,
                                       fg_color="#eceef1", hover_color="#e0e3e8",
                                       text_color=TXT, command=self.on_click_creds)
        self.creds_btn.pack(side="left", padx=(6, 0))
        self.creds_btn.configure(state="disabled")

        self.dl_btn = ctk.CTkButton(row, text="下载选中", width=110, height=32,
                                    fg_color=ACCENT, hover_color="#2f6fd0",
                                    command=self.on_click_download)
        self.dl_btn.pack(side="right")
        self.stop_btn = ctk.CTkButton(row, text="停止", width=70, height=32, fg_color="#e5484d",
                                      hover_color="#c93c40", command=self.on_click_stop)
        self.stop_btn.pack(side="right", padx=8)
        self.stop_btn.pack_forget()

        # 下面两个平时隐藏，按需出现
        self.retry_btn = ctk.CTkButton(row, text="重试失败项", width=100, height=32,
                                       fg_color="#e5484d", hover_color="#c93c40",
                                       command=self.on_click_retry)
        self.open_btn = ctk.CTkButton(row, text="打开输出目录", width=110, height=32,
                                      fg_color="#eceef1", hover_color="#e0e3e8",
                                      text_color=TXT, command=self.on_click_open_dir)

        # 开关行
        row2 = ctk.CTkFrame(foot, fg_color="transparent")
        row2.pack(fill="x", padx=14, pady=(2, 8))
        self.var_tags = tk.BooleanVar(value=self.cfg["embed_tags"])
        self.var_cover = tk.BooleanVar(value=self.cfg["embed_cover"])
        self.var_lyric = tk.BooleanVar(value=self.cfg["save_lyric"])
        self.var_split = tk.BooleanVar(value=self.cfg["split_folder"])
        self.var_skip = tk.BooleanVar(value=self.cfg["skip_existing"])
        for text, var in (
            ("嵌入标签", self.var_tags), ("嵌入封面", self.var_cover),
            ("保存歌词", self.var_lyric), ("按歌手/专辑分文件夹", self.var_split),
            ("跳过已下载", self.var_skip),
        ):
            ctk.CTkCheckBox(row2, text=text, variable=var, font=ctk.CTkFont(size=12),
                            checkbox_width=18, checkbox_height=18,
                            command=self.persist).pack(side="left", padx=(0, 16))

        # 失败自动重试次数（0 = 不重试）
        self.retries_menu = ctk.CTkOptionMenu(
            row2, values=["0", "1", "2", "3"], width=60, height=26,
            fg_color="#eceef1", button_color="#dfe3e8", text_color=TXT,
            command=self.on_workers_change,
        )
        self.retries_menu.pack(side="right")
        self.retries_menu.set(str(self.cfg.get("retries", 2)))
        ctk.CTkLabel(row2, text="失败重试", text_color=MUTED,
                     font=ctk.CTkFont(size=12)).pack(side="right", padx=(0, 6))

        self.prog = ctk.CTkProgressBar(foot, height=6, progress_color=ACCENT)
        self.prog.set(0)
        self.prog.pack(fill="x", padx=14, pady=(2, 4))
        self.prog_label = ctk.CTkLabel(foot, text="就绪", text_color=MUTED,
                                       font=ctk.CTkFont(size=12), anchor="w")
        self.prog_label.pack(fill="x", padx=14)

        self.log = ctk.CTkTextbox(foot, height=96, fg_color="#fafbfc", text_color=TXT,
                                  font=ctk.CTkFont(size=11), wrap="word")
        self.log.pack(fill="x", padx=14, pady=(6, 12))
        self.log.configure(state="disabled")

    # ---------- 工具 ----------

    def log_line(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _show_hint(self, text):
        """在歌曲列表区显示一行提示（搜索中/无结果/失败时使用）。

        此时 self.tracks 为空，列表里的这行不会对应任何曲目，
        下载时按 len(self.tracks) 过滤即可，不会误选。
        """
        self.tk_list.delete(0, "end")
        self.tk_list.insert("end", "  " + text)

    def _on_search_btn(self):
        """搜索按钮：空闲时发起搜索，搜索中时变成取消。"""
        if self._pending > 0:
            self._cancel_search()
        else:
            self.on_cloud_search()

    def _cancel_search(self):
        """作废在途搜索并立刻解锁界面。

        线程里的网络请求无法真正杀掉，但把搜索序号推进后，它们返回时会被丢弃，
        界面无需等待，也不会被旧结果污染。
        """
        self._search_seq += 1
        self._pending = 0
        self._set_search_busy(False)
        self.more_btn.configure(state="normal", text="加载更多")
        self.tk_count.configure(text="已取消搜索")
        self.log_line("已取消搜索（后台请求超时后会自行结束）。")

    def _set_search_busy(self, busy):
        if busy:
            self.search_btn.configure(state="normal", text="取消搜索",
                                      fg_color="#fdecec", hover_color="#f8d7d7",
                                      text_color="#c93c40")
        else:
            self.search_btn.configure(state="normal", text="搜索",
                                      fg_color="#eceef1", hover_color="#e0e3e8",
                                      text_color=TXT)

    def _search_done(self):
        """一次搜索线程收尾：全部在途结束后才解除忙碌态。"""
        self._pending = max(0, self._pending - 1)
        if self._pending == 0:
            self._set_search_busy(False)
            self.more_btn.configure(state="normal", text="加载更多")

    def _on_close(self):
        """关窗前清理：中断下载、关掉可能还开着的登录浏览器。

        不做这一步，登录用的那个浏览器进程会变成孤儿一直留在后台。
        """
        if self._downloading:
            if not messagebox.askyesno(
                "确认退出",
                "正在下载，确定要退出吗？\n未完成的文件会保留为 .part，下次重下即可。",
            ):
                return
            self._stop = True
        for w in list(self.winfo_children()):
            if isinstance(w, LoginDialog):
                try:
                    w.close()
                except Exception:
                    pass
        self.destroy()

    def _center_window(self):
        """居中主窗口并夹进屏幕，避免高分屏缩放下底部被切到屏幕外。"""
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        x = max(0, (self.winfo_screenwidth() - w) // 2)
        y = max(0, (self.winfo_screenheight() - h) // 2)
        self.geometry(f"+{x}+{y}")

    def _short_dir(self):
        d = self.cfg["out_dir"]
        return d if len(d) <= 42 else "…" + d[-40:]

    def persist(self):
        self.cfg.update(
            level=self.level_menu.get().split()[0],
            workers=int(self.workers_menu.get()),
            retries=int(self.retries_menu.get()),
            embed_tags=self.var_tags.get(),
            embed_cover=self.var_cover.get(),
            save_lyric=self.var_lyric.get(),
            split_folder=self.var_split.get(),
            skip_existing=self.var_skip.get(),
        )
        save_settings(self.cfg)

    def on_level_change(self, _=None):
        self.persist()

    def on_workers_change(self, _=None):
        self.persist()

    def on_pick_dir(self):
        d = filedialog.askdirectory(title="选择下载目录", initialdir=self.cfg["out_dir"])
        if d:
            self.cfg["out_dir"] = d
            self.dir_label.configure(text=self._short_dir())
            self.persist()

    def on_click_creds(self):
        """平台凭据配置：非网易云平台都可粘贴账号信息。"""
        if not providers.supports_credentials(self.provider.key):
            messagebox.showinfo("提示", "网易云请在右上角用「Cookie 登录」。")
            return
        CredsDialog(self, self.provider, self._on_creds_saved)

    def _on_creds_saved(self):
        reload_fn = getattr(self.provider, "reload", None)
        if callable(reload_fn):
            reload_fn()
        self.log_line(f"「{self.provider.display_name}」凭据已更新，下次搜索生效。")

    # ---------- 登录 ----------

    def _auto_login(self):
        if self.provider.is_logged_in():
            self.log_line("检测到已保存的登录态，正在校验…")
            threading.Thread(target=self._load_account, daemon=True).start()
        else:
            self.log_line("未登录。点击右上角「Cookie 登录」，按提示粘贴浏览器 Cookie。")

    def on_click_login(self):
        LoginDialog(self, self.provider, self._on_login_ok, BROWSER_PROFILE)

    def _on_login_ok(self):
        self.log_line("登录成功，正在拉取账号信息…")
        threading.Thread(target=self._load_account, daemon=True).start()

    def _load_account(self):
        ep = self._platform_epoch
        try:
            account, profile = self.provider.account()
        except LoginExpired:
            self.ui(self.log_line, "登录态已失效，需要重新登录。")
            self.ui(self._on_login_expired)
            return
        except Exception as e:
            self.ui(self.log_line, f"获取账号信息失败：{e}")
            return
        if not account:
            self.ui(
                self.log_line,
                "账号接口未返回信息，已清除登录态，请重新登录。"
                f"（授权时拿到的 cookie：{', '.join(self.provider.cookie_names) or '空'}）",
            )
            self.provider.clear_cookies()
            return
        self.profile = profile or {}
        uid = account.get("id")
        self._uid = uid
        nick = (profile or {}).get("nickname") or "已登录"
        self._ui_if_current(ep, self._apply_profile, nick, uid)
        self.ui(self.log_line, f"已登录：{nick}（uid={uid}）")
        self._ui_if_current(ep, self._load_playlists, uid)

    def _apply_profile(self, nick, uid):
        self.name_label.configure(text=nick)
        self.sub_label.configure(text=f"uid {uid}")
        self.login_btn.configure(text="重新登录")
        self.liked_btn.configure(state="normal")
        av = (self.profile or {}).get("avatarUrl")
        if av:
            threading.Thread(target=self._load_avatar, args=(av,), daemon=True).start()

    def _load_avatar(self, url):
        try:
            data = requests.get(url + "?param=80y80", timeout=15).content
            img = Image.open(io.BytesIO(data)).convert("RGB").resize((44, 44))
            ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(44, 44))
            self.ui(self._set_avatar, ctk_img)
        except Exception:
            pass

    def _set_avatar(self, ctk_img):
        self._avatar_ref = ctk_img
        self.avatar.configure(image=ctk_img, text="")

    # ---------- 歌单 / 歌曲 ----------

    def _load_playlists(self, uid):
        ep = self._platform_epoch
        try:
            pls = self.provider.user_playlists(uid)
        except Exception as e:
            self.ui(self.log_line, f"拉取歌单失败：{e}")
            return
        self.playlists = pls
        self._ui_if_current(ep, self._fill_playlists, pls)
        self.ui(self.log_line, f"共 {len(pls)} 个歌单。")

    def _fill_playlists(self, pls):
        self.pl_list.delete(0, "end")
        for p in pls:
            if p.get("specialType") == 5:
                tag = "★ "
            elif p.get("subscribed"):
                tag = "收藏 · "
            else:
                tag = ""
            cnt = p.get("trackCount") or 0
            # 「我喜欢的音乐」在列表接口里恒返回 0，标注一下避免误导
            cnt_txt = "歌曲数见右侧" if p.get("specialType") == 5 and not cnt else str(cnt)
            self.pl_list.insert("end", f" {tag}{p.get('name')}  ({cnt_txt})")
        self.pl_count.configure(text=f"{len(pls)} 个")
        # 自动选中「我喜欢的音乐」
        for i, p in enumerate(pls):
            if p.get("specialType") == 5:
                self.pl_list.selection_set(i)
                self.pl_list.see(i)
                self.on_pick_playlist()
                break

    def on_pick_playlist(self, _=None):
        sel = self.pl_list.curselection()
        if not sel:
            return
        p = self.playlists[sel[0]]
        self._search_kw = ""
        self.more_btn.pack_forget()
        self.tk_title.configure(text=f"歌曲 · {p.get('name')}")
        self.tk_count.configure(text="加载中…")
        self.tk_list.delete(0, "end")
        self.tracks = []
        threading.Thread(target=self._load_tracks, args=(p,), daemon=True).start()

    def _load_tracks(self, playlist):
        ep = self._platform_epoch
        try:
            tracks = self.provider.playlist_tracks(playlist["id"])
        except Exception as e:
            self.ui(self.tk_count.configure, text="加载失败")
            self.ui(self.log_line, f"加载歌曲失败：{e}")
            return
        self._ui_if_current(ep, self._fill_tracks, tracks)

    def on_cloud_search(self, _=None):
        """搜索当前平台全站歌曲。

        慢平台可能要几十秒；允许用户改词再搜，旧请求的结果靠序号丢弃，
        不会覆盖新结果，也不会把界面锁死。
        """
        kw = (self.search_cloud.get() or "").strip()
        if not kw:
            return
        self._search_kw = kw
        self._search_seq += 1
        self._pending += 1
        self._set_search_busy(True)
        self.tk_title.configure(text=f"搜索 · {kw}")
        self.tk_count.configure(text=f"正在搜索 {self.provider.display_name}…")
        self._show_hint(f"正在从 {self.provider.display_name} 搜索「{kw}」…")
        self.log_line(f"搜索「{kw}」…")
        threading.Thread(target=self._cloud_search_worker,
                         args=(kw, 0, self._search_seq, self._platform_epoch),
                         daemon=True).start()

    def on_click_more(self):
        if not self._search_kw:
            return
        self._pending += 1
        self.more_btn.configure(state="disabled", text="加载中…")
        threading.Thread(target=self._cloud_search_worker,
                         args=(self._search_kw, len(self.all_tracks),
                               self._search_seq, self._platform_epoch),
                         daemon=True).start()

    def _cloud_search_worker(self, kw, offset, seq, ep):
        try:
            songs, total = self.provider.search(kw, limit=100, offset=offset)
        except Exception as e:
            msg = str(e)
            self.ui(self._search_done)
            if self._search_fresh(seq, ep):
                self.ui(self.tk_count.configure, text="搜索失败")
                self.ui(self._show_hint, f"搜索失败：{msg}")
                self.ui(self.log_line, f"搜索失败：{msg}")
            return
        self.ui(self._search_done)
        if self._search_fresh(seq, ep):
            self.ui(self._apply_search_results, songs, total, offset)

    def _apply_search_results(self, songs, total, offset):
        self._search_total = total
        if offset == 0:
            self._fill_tracks(songs)
        else:
            self.all_tracks = list(self.all_tracks) + list(songs)
            self.tracks = list(self.all_tracks)
            self._render_tracks()
        shown = len(self.all_tracks)
        self.more_btn.configure(state="normal", text="加载更多")
        self.tk_title.configure(text=f"搜索 · {self._search_kw}（共命中 {total} 首）")
        if shown < total and songs:
            self.more_btn.pack(side="right", padx=6)
        else:
            self.more_btn.pack_forget()
        if shown == 0:
            note = providers.platform_note(self.provider.key)
            self._show_hint("未搜到结果。" + (note or "换个关键词或换个平台试试。"))
        self.log_line(f"搜索「{self._search_kw}」命中 {total} 首，已载入 {shown} 首")

    def _fill_tracks(self, tracks):
        """装载一个歌单：保存全量、清空搜索、渲染。"""
        self.all_tracks = list(tracks)
        self.tracks = list(tracks)
        # 只在搜索框有内容时才清空：否则 set("") 会触发一次多余的过滤重绘
        # （5000 首的歌单上白白多花约 0.4s）
        if hasattr(self, "search_var") and self.search_var.get():
            self.search_var.set("")
        self._render_tracks()

    def _on_search(self, *_):
        """输入即过滤；加短延时，避免每敲一个字都重绘上千行。"""
        if self._search_job:
            try:
                self.after_cancel(self._search_job)
            except Exception:
                pass
        self._search_job = self.after(200, self._apply_filter)

    def _apply_filter(self):
        self._search_job = None
        q = (self.search_var.get() or "").strip().lower()
        if q:
            self.tracks = [
                t for t in self.all_tracks
                if q in t.name.lower() or q in t.artists.lower()
            ]
        else:
            self.tracks = list(self.all_tracks)
        self._render_tracks()

    def _render_tracks(self):
        """分批插入歌曲列表。

        几千首时一次性 insert 会把主线程卡住一秒以上（实测 5000 首约 1.05s），
        改为每批 400 行、批间让出一次事件循环，界面就不会僵住。
        """
        if self._render_job:
            try:
                self.after_cancel(self._render_job)
            except Exception:
                pass
            self._render_job = None
        self.tk_list.delete(0, "end")
        self.tk_list.bind("<<ListboxSelect>>", self._update_sel_count)
        self._render_pos = 0
        self._render_chunk()

    def _render_chunk(self, size=400):
        rows = self.tracks
        end = min(self._render_pos + size, len(rows))
        for i in range(self._render_pos, end):
            t = rows[i]
            vip = "VIP " if t.vip else ""
            self.tk_list.insert(
                "end",
                f" {i + 1:>4}. {vip}{t.name}  —  {t.artists}"
                f"   [{fmt_dur(t.duration_s)}]",
            )
        self._render_pos = end
        if end < len(rows):
            self.tk_count.configure(text=f"载入中 {end}/{len(rows)}…")
            self._render_job = self.after(1, self._render_chunk)
            return
        self._render_job = None
        if self._select_all_pending:
            self._select_all_pending = False
            self.tk_list.selection_set(0, "end")
        self._update_sel_count()

    def _select_all_when_ready(self):
        """等渲染完再全选，否则只会选中已渲染的那一批。"""
        self._select_all_pending = True

    def _update_sel_count(self, _=None):
        shown, total = len(self.tracks), len(self.all_tracks)
        head = f"{shown} 首" if shown == total else f"{shown} 首 / 共 {total} 首"
        self.tk_count.configure(text=f"{head}，已选 {len(self.tk_list.curselection())}")

    # ---------- 下载 ----------

    def _make_options(self):
        # 注意：不能命名为 _options，会覆盖 tkinter Misc._options 导致窗口无法创建
        self.persist()
        return DownloadOptions(
            out_dir=self.cfg["out_dir"],
            level=self.cfg["level"],
            embed_tags=self.var_tags.get(),
            embed_cover=self.var_cover.get(),
            save_lyric=self.var_lyric.get(),
            split_folder=self.var_split.get(),
            skip_existing=self.var_skip.get(),
            retries=int(self.cfg.get("retries", 2)),
        )

    def on_click_liked(self):
        """一键下载我喜欢的音乐。"""
        liked = next((p for p in self.playlists if p.get("specialType") == 5), None)
        if not liked:
            messagebox.showwarning("提示", "没有找到「我喜欢的音乐」歌单，请先刷新歌单。")
            return
        self.log_line(f"一键下载：{liked.get('name')}（{liked.get('trackCount')} 首）")
        self.tk_title.configure(text=f"歌曲 · {liked.get('name')}")
        self.tk_count.configure(text="加载中…")
        self.tk_list.delete(0, "end")
        threading.Thread(target=self._liked_worker, args=(liked,), daemon=True).start()

    def _liked_worker(self, playlist):
        ep = self._platform_epoch
        try:
            tracks = self.provider.playlist_tracks(playlist["id"])
        except Exception as e:
            self.ui(self.log_line, f"加载失败：{e}")
            return
        self._ui_if_current(ep, self._fill_tracks, tracks)
        self._ui_if_current(ep, self._select_all_when_ready)
        self._ui_if_current(ep, self._start_download, tracks)

    def on_click_download(self):
        sel = self.tk_list.curselection()
        if not sel:
            messagebox.showinfo("提示", "请先在右侧选择要下载的歌曲（可用 Ctrl / Shift 多选，或点「全选」）。")
            return
        picked = [self.tracks[i] for i in sel if i < len(self.tracks)]
        self._start_download(picked)

    def on_click_stop(self):
        self._stop = True
        self.log_line("已请求停止，当前文件下完后中断…")

    def on_click_retry(self):
        """只重下上次失败的曲目，不必整个歌单重来。"""
        if not self._failed_tracks:
            messagebox.showinfo("提示", "没有可重试的失败曲目。")
            return
        self.log_line(f"重试 {len(self._failed_tracks)} 首失败曲目…")
        self._start_download(list(self._failed_tracks))

    def on_click_open_dir(self):
        d = self.cfg["out_dir"]
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)
        except Exception as e:
            messagebox.showinfo("提示", f"打开目录失败：{e}")

    def _start_download(self, tracks):
        if self._downloading:
            messagebox.showinfo("提示", "已有下载任务在进行中。")
            return
        if not tracks:
            return
        self._stop = False
        self._downloading = True
        self.dl_btn.pack_forget()
        self.retry_btn.pack_forget()
        self.open_btn.pack_forget()
        self.stop_btn.pack(side="right", padx=8)
        self.platform_menu.configure(state="disabled")
        threading.Thread(target=self._download_worker, args=(tracks, self._make_options()), daemon=True).start()

    def _download_worker(self, tracks, opts):
        total = len(tracks)
        workers = max(1, int(self.cfg.get("workers", 3)))
        os.makedirs(opts.out_dir, exist_ok=True)
        self.ui(self.log_line, f"开始下载 {total} 首（并发 {workers}）→ {opts.out_dir}")

        def on_result(_i, t, r):
            label = f"{t.name} - {t.artists}"
            if r.status == "ok":
                lv = f"  [{LEVELS.get(r.level, r.level)}]" if r.level else ""
                lv += "  续传" if getattr(r, "resumed_from", 0) else ""
                self.ui(self.log_line, f"  \u2713 {label}  {human(r.bytes_)}{lv}")
            elif r.status == "skip":
                self.ui(self.log_line, f"  - {label}\uff1a{r.reason}")
            else:
                mark = "  [可重试]" if getattr(r, "retryable", False) else ""
                self.ui(self.log_line, f"  \u2717 {label}\uff1a{r.reason}{mark}")

        def on_tick(frac, done_n):
            self.ui(self._set_progress, frac, f"已完成 {done_n}/{total}（并发 {workers}）")

        try:
            ok, skip, fail, failed, failed_tracks = download_many(
                self.provider, tracks, opts, workers=workers,
                on_result=on_result, on_tick=on_tick,
                should_stop=lambda: self._stop,
            )
        except LoginExpired:
            self.ui(self._on_login_expired)
            return

        self.ui(self.log_line, "已停止。" if self._stop else "")
        self.ui(self._set_progress, 0, "就绪")
        self.ui(self._finish_download, ok, skip, fail, failed, failed_tracks)

    def _on_login_expired(self):
        """登录态失效：清掉本地凭据、恢复未登录界面、引导重新登录。"""
        self._downloading = False
        self._failed_tracks = []
        self.stop_btn.pack_forget()
        self.retry_btn.pack_forget()
        self.open_btn.pack_forget()
        self.dl_btn.pack(side="right")
        self.platform_menu.configure(state="normal")
        self.prog.set(0)
        self.prog_label.configure(text="登录态已失效，请重新登录")
        self.provider.clear_cookies()
        self.profile = None
        self.playlists = []
        self.all_tracks = []
        self.tracks = []
        self.tk_list.delete(0, "end")
        self.pl_list.delete(0, "end")
        self.name_label.configure(text="未登录")
        self.sub_label.configure(text="登录态已失效，请重新登录")
        self.login_btn.configure(text="重新登录")
        self.liked_btn.configure(state="disabled")
        self.log_line("登录态已失效（网易云返回 301），本地凭据已清除。")
        self.log_line("请点右上角「重新登录」，在弹出的浏览器里重新登录。")

    def _set_progress(self, frac, text):
        self.prog.set(max(0.0, min(1.0, frac)))
        self.prog_label.configure(text=text)

    def _finish_download(self, ok, skip, fail, failed, failed_tracks):
        self._downloading = False
        self._failed_tracks = failed_tracks
        self.stop_btn.pack_forget()
        self.dl_btn.pack(side="right")
        self.platform_menu.configure(state="normal")
        if failed_tracks:
            self.retry_btn.pack(side="right", padx=8)
        if ok or skip or fail:
            self.open_btn.pack(side="right", padx=8)
        self.prog.set(1 if ok else 0)
        tail = f"（可重试 {len(failed_tracks)} 首）" if failed_tracks else ""
        self.prog_label.configure(text=f"完成：成功 {ok} / 跳过 {skip} / 失败 {fail}{tail}")
        self.log_line(f"任务结束：成功 {ok}，跳过 {skip}，失败 {fail}")
        if failed:
            self.log_line("失败清单：")
            for name, reason in failed[:40]:
                self.log_line(f"    · {name} —— {reason}")
            if len(failed) > 40:
                self.log_line(f"    …另有 {len(failed) - 40} 首")


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
