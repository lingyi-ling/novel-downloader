"""Provider 注册表：按 key 惰性创建平台实现。

v2.0 关键决策：
- 注册表只存「显示名 + 构造器」，不预先 import 平台实现，
  这样重依赖平台（FFmpeg / DRM）只在被选中时才加载
- 新增平台只需 _reg 一行
"""

from . import creds  # noqa: F401
from .base import MusicProvider, ResolveResult, Track  # noqa: F401
from .musicdl import (  # noqa: F401
    GROUP_ORDER,
    PLATFORM_NOTES,
    PLATFORMS,
    MusicdlProvider,
    group_of,
    heavy_available,
)

_REGISTRY = {}


def _reg(key, display, builder):
    _REGISTRY[key] = (display, builder)


def _netease_builder(**kwargs):
    from .netease import NeteaseProvider

    return NeteaseProvider(**kwargs)


_reg("netease", "网易云音乐", _netease_builder)

# musicdl 平台（轻档 19 + 中档 5）
for _k, (_disp, _mod, _cls) in PLATFORMS.items():
    _reg(_k, _disp, (lambda k=_k, **kw: MusicdlProvider(key=k, **kw)))

# 「重」档：仅当依赖（pywidevine / Node）已安装才注册
for _k, _disp in heavy_available():
    _reg(_k, _disp, (lambda k=_k, **kw: MusicdlProvider(key=k, **kw)))


def supports_credentials(key):
    """该平台是否支持通过「平台凭据」配置账号信息。"""
    return key != "netease"


def _sorted_items():
    """注册项按「分组顺序 + 显示名」排序，让同组平台在下拉里聚在一起。"""

    def sort_key(kv):
        g = group_of(kv[0])
        gi = GROUP_ORDER.index(g) if g in GROUP_ORDER else len(GROUP_ORDER)
        return (gi, kv[1][0])

    return sorted(_REGISTRY.items(), key=sort_key)


def available():
    """返回按分组排序的 [(key, 显示名), ...]。"""
    return [(k, v[0]) for k, v in _sorted_items()]


def available_labels():
    """返回按分组排序的 [(key, 下拉标签), ...]，标签形如「国内 · 网易云音乐」。"""
    return [(k, f"{group_of(k)} · {v[0]}") for k, v in _sorted_items()]


def platform_note(key):
    """返回该平台的使用提示（如需账号/网络条件），没有则返回空串。"""
    return PLATFORM_NOTES.get(key, "")


def create(key, **kwargs):
    """按 key 创建 provider 实例。kwargs 透传给构造函数。"""
    if key not in _REGISTRY:
        raise KeyError(f"未知平台：{key}")
    _, builder = _REGISTRY[key]
    return builder(**kwargs)
