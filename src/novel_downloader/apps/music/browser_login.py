"""用调试模式浏览器完成登录，再用 CDP 取回登录态 Cookie。

为什么这么做：
- 网易云对第三方扫码登录启用了行为验证风控（返回 8821），扫码已不可用
- 让用户按 F12 手动翻 Cookies 对普通用户门槛过高
- 于是反过来：我们启动一个浏览器让用户「正常登录」，再用 CDP 把 Cookie 取回来
  关键点：MUSIC_U 是 httpOnly，网页 JS 读不到，但 CDP 的 Network.getCookies 能读到

安全约束：
- 只结束本模块启动的进程树，绝不动用户自己开的浏览器
- profile 独立存放，不与用户日常浏览器混用
"""

import contextlib
import json
import os
import subprocess
import time

import requests
import websocket

PORT = 9333
LOGIN_URL = "https://music.163.com/"
COOKIE_DOMAIN_URL = "https://music.163.com"
WANT_COOKIE = "MUSIC_U"  # 登录态的核心凭据（httpOnly）

_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


class BrowserLoginError(Exception):
    """浏览器登录流程出错。"""


def find_browser():
    for p in _CANDIDATES:
        if p and os.path.exists(p):
            return p
    raise BrowserLoginError("没找到 Edge 或 Chrome，请先安装其中之一")


class BrowserLogin:
    """管理调试浏览器与登录态读取。

    用法：
        bl = BrowserLogin(profile_dir)
        bl.start(); bl.open_login_page()
        cookies = bl.wait_for_login(timeout=180, on_status=cb)
        bl.close()
    """

    def __init__(self, profile_dir, port=PORT):
        self.profile_dir = profile_dir
        self.port = port
        self.proc = None
        self.launched = False
        self.ws = None
        self._id = 0
        self._target_id = None

    # ---------- CDP 基础设施 ----------

    @property
    def _base(self):
        return f"http://127.0.0.1:{self.port}"

    def _version(self):
        try:
            return requests.get(f"{self._base}/json/version", timeout=2).json()
        except Exception:
            return None

    def start(self, timeout=40):
        """启动调试浏览器。若已有本端口实例则复用。"""
        v = self._version()
        if v:
            self.external = True
            return v.get("Browser")

        exe = find_browser()
        os.makedirs(self.profile_dir, exist_ok=True)
        self.proc = subprocess.Popen(
            [
                exe,
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self.profile_dir}",
                "--remote-allow-origins=*",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-sync",
                "about:blank",
            ],
            close_fds=True,
        )
        self.launched = True
        for _ in range(timeout):
            v = self._version()
            if v:
                return v.get("Browser")
            time.sleep(1)
        raise BrowserLoginError("浏览器启动超时，调试端口没起来")

    def _connect(self, url):
        target = None
        r = requests.put(self._base + "/json/new?" + url, timeout=10)
        if r.status_code >= 400:
            r = requests.get(self._base + "/json/new?" + url, timeout=10)
        r.raise_for_status()
        target = r.json()
        self._target_id = target.get("id")
        self.ws = websocket.create_connection(
            target["webSocketDebuggerUrl"],
            timeout=30,
            suppress_origin=True,
            max_size=32 * 1024 * 1024,
        )
        self._send("Network.enable")
        self._send("Page.enable")
        return target

    def _send(self, method, **params):
        assert self.ws is not None, "CDP websocket is not connected"
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == mid:
                if "error" in m:
                    raise BrowserLoginError(f"{method} 失败：{m['error']}")
                return m.get("result", {})

    # ---------- 业务流程 ----------

    def open_login_page(self):
        """打开网易云，让用户在里面正常登录。"""
        self._connect(LOGIN_URL)

    def read_cookies(self):
        """读取该域名下的全部 cookie（含 httpOnly）。返回 name->value。"""
        res = self._send("Network.getCookies", urls=[COOKIE_DOMAIN_URL])
        out = {}
        for c in res.get("cookies", []):
            if c.get("name") and c.get("value"):
                out[c["name"]] = c["value"]
        return out

    def wait_for_login(self, timeout=240, on_status=None, should_stop=None):
        """轮询等待登录完成。返回全部 cookie 字典。

        参数：
            on_status(str, bool)  —— 状态回调，(文本, 是否重点)
            should_stop()         —— 返回 True 则中止
        """
        deadline = time.time() + timeout
        last_note = 0
        while time.time() < deadline:
            if should_stop and should_stop():
                raise BrowserLoginError("已取消")
            try:
                ck = self.read_cookies()
            except Exception:
                time.sleep(2)
                continue
            if ck.get(WANT_COOKIE):
                return ck
            el = int(time.time() - (deadline - timeout))
            if on_status and el - last_note >= 10:
                last_note = el
                on_status(f"等待登录中…（{el} 秒）请在弹出的浏览器里完成登录", False)
            time.sleep(2)
        raise BrowserLoginError(f"等待登录超时（{timeout} 秒），请重试")

    def close(self):
        """只关闭「本模块启动的那个浏览器实例」。

        绝不使用 taskkill /T 按 PID 连带杀进程树：PID 可能已被系统回收，
        一旦复用就会误杀无关进程（包括用户自己开的浏览器）。
        这里用两种精确方式：
            1. CDP 的 Browser.close —— 优雅关闭当前调试实例
            2. 按专属 profile 路径匹配命令行，只杀匹配到的那几个 PID
        """
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        try:
            if self._target_id:
                requests.get(f"{self._base}/json/close/{self._target_id}", timeout=5)
        except Exception:
            pass

        if not self.launched:
            return  # 复用了他人的实例，绝不关闭

        if not self._close_via_cdp():
            self._kill_by_profile()

    def _close_via_cdp(self):
        """通过浏览器级 CDP 优雅关闭本实例。"""
        try:
            v = self._version()
            url = (v or {}).get("webSocketDebuggerUrl")
            if not url:
                return False
            ws = websocket.create_connection(url, timeout=10, suppress_origin=True)
            ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
            with contextlib.suppress(Exception):
                ws.recv()
            with contextlib.suppress(Exception):
                ws.close()
            return True
        except Exception:
            return False

    def _kill_by_profile(self):
        """兜底：只杀命令行里带本实例专属 profile 路径的浏览器进程。"""
        key = os.path.abspath(self.profile_dir).lower()
        try:
            out = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_Process -Filter "
                    "\"Name='msedge.exe' OR Name='chrome.exe'\""
                    ' | ForEach-Object { "$($_.ProcessId)|$($_.CommandLine)" }',
                ],
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
        except Exception:
            return
        for line in out.splitlines():
            if "|" not in line:
                continue
            pid, cl = line.split("|", 1)
            if key in cl.lower():
                subprocess.run(
                    ["taskkill", "/F", "/PID", pid.strip()], capture_output=True
                )


if __name__ == "__main__":
    bl = BrowserLogin(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "_standalone_profile")
    )
    print("启动浏览器…")
    print("  ", bl.start())
    bl.open_login_page()
    print("请在弹出的浏览器里登录网易云，等待中…")
    try:
        ck = bl.wait_for_login(timeout=180, on_status=lambda t, k: print("  ", t))
        print("\n登录成功，拿到 cookie：")
        for k in sorted(ck):
            print(f"   {k} (长度 {len(ck[k])})")
        print("\nMUSIC_U 长度:", len(ck.get("MUSIC_U", "")))
    except Exception as e:
        print("失败:", e)
    finally:
        bl.close()
        print("已关闭")
