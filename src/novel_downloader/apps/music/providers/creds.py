"""平台凭据存储。

v2.1 关键决策：
- 只存「平台 key -> 原始 dict」，不解释语义——各平台需要的字段不同
  （TIDAL 要 token 字段的 JSON，汽水音乐要 {"cookies": {...}}），由用户按提示粘贴
- 落盘在项目根目录 platform_creds.json，纯文本便于用户自查
"""

import contextlib
import json
import os

from ..paths import creds_file

_FILE = str(creds_file())


def load():
    if os.path.exists(_FILE):
        try:
            with open(_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def get(key):
    return load().get(key) or {}


def set(key, data):
    """写入或清除某平台凭据（data 为空则清除）。

    凭据是敏感数据：写入失败时清掉 ``.tmp`` 残留，避免半截明文留在盘上。
    """
    all_ = load()
    if data:
        all_[key] = data
    else:
        all_.pop(key, None)
    tmp = _FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(all_, f, ensure_ascii=False, indent=2)
        # 里面存着平台登录凭据（TIDAL token、站点 cookies），落盘前收紧到
        # 仅属主可读写——与 weapi.save_cookies / cookie_io.write_cookie_items
        # 保持一致（Windows 上 chmod 只能尽力而为，失败不影响保存）。
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, _FILE)
    finally:
        # os.replace 成功后 tmp 已不存在；失败时把半截明文清掉
        with contextlib.suppress(OSError):
            if os.path.exists(tmp):
                os.remove(tmp)


def clear(key):
    set(key, {})
