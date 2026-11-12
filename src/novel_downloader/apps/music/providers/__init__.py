"""Provider 注册表：按 key 惰性创建平台实现。

v2.0 关键决策：
- 注册表只存「显示名 + 构造器」，不预先 import 平台实现，
  这样重依赖平台（FFmpeg / DRM）只在被选中时才加载
- 新增平台只需 _reg 一行

v2.2：注册表同时是平台能力的唯一来源。``capabilities()`` 为每个已注册
平台生成一份 :class:`PlatformCapability`（搜索/下载/歌词、账号字段、CDM/
外部工具依赖、限制），未注册的重档平台也能通过 ``all_capabilities()``
拿到同一套描述和缺失原因。``available_labels()`` / ``platform_note()``
都从这份数据派生，CLI、REST、内嵌引擎与桌面端因此不会再各写一份判断。

能力探测只使用 ``find_spec`` / ``shutil.which``：列平台不会 import
pywidevine、ytmusicapi 或任何 vendored 平台模块。
"""

from collections.abc import Callable, Iterable
from typing import Any, cast

from . import creds  # noqa: F401
from .base import (  # noqa: F401
    CAPABILITY_MISSING_DEPENDENCY,
    CAPABILITY_MISSING_TOOL,
    CAPABILITY_READY,
    CAPABILITY_UNAVAILABLE,
    DOWNLOAD_MODES,
    LIMITATIONS,
    REQUIREMENT_KINDS,
    MusicProvider,
    PlatformCapability,
    Requirement,
    ResolveResult,
    Track,
)
from .health import health_check as health_check
from .musicdl import (  # noqa: F401
    ALL_PLATFORMS,
    GROUP_ORDER,
    LIGHT_PLATFORMS,
    PLATFORM_NOTES,
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

# musicdl 平台（轻档 + 中档；依赖缺失的如 TIDAL 不注册）
for _k, (_disp, _mod, _cls) in LIGHT_PLATFORMS.items():
    _reg(_k, _disp, (lambda k=_k, **kw: MusicdlProvider(key=k, **kw)))

# 「重」档：仅当依赖（pywidevine / Node）已安装才注册
for _k, _disp in heavy_available():
    _reg(_k, _disp, (lambda k=_k, **kw: MusicdlProvider(key=k, **kw)))


# ---------------------------------------------------------------------------
# 能力矩阵
# ---------------------------------------------------------------------------

#: 未在上面列出的 musicdl 平台使用这组默认能力：可搜索、可直链下载、有歌词。
#: 只记录「与默认不同」的平台，新增平台时默认值即为最常见情况。
_CAPABILITY_OVERRIDES: dict[str, dict[str, Any]] = {
    # ---- 需要账号 / CDM / 外部工具 ----
    "tidal": {
        "download_mode": "external",
        "credential_fields": (
            "access_token",
            "refresh_token",
            "client_id",
            "client_secret",
        ),
        "credential_required": True,
        "requirements": (
            Requirement(
                "python",
                "aigpy",
                install_hint='pip install "novel-downloader[music]"',
            ),
            Requirement(
                "tool",
                "N_m3u8DL-RE",
                install_hint="Install N_m3u8DL-RE and add it to PATH.",
            ),
            Requirement(
                "tool",
                "ffmpeg",
                install_hint="Install ffmpeg and add it to PATH.",
            ),
            Requirement(
                "cdm",
                "widevine",
                install_hint="Provide a Widevine .wvd device file.",
            ),
        ),
        "limitations": (
            "drm_protected",
            "hls_stream",
            "requires_credentials",
            "requires_external_tool",
            "region_restricted",
        ),
    },
    "apple": {
        "download_mode": "external",
        "credential_fields": ("media-user-token",),
        "requirements": (
            Requirement(
                "python",
                "pywidevine",
                install_hint='pip install "novel-downloader[music]"',
            ),
            Requirement(
                "tool",
                "ffmpeg",
                install_hint="Install ffmpeg and add it to PATH.",
            ),
            Requirement(
                "cdm",
                "widevine",
                install_hint="Provide a Widevine .wvd device file.",
            ),
        ),
        "limitations": (
            "drm_protected",
            "hls_stream",
            "requires_cdm",
            "requires_external_tool",
            "preview_only_without_credentials",
        ),
    },
    "soundcloud": {
        "download_mode": "external",
        "credential_fields": ("oauth_token", "client_id"),
        "requirements": (
            Requirement(
                "python",
                "pywidevine",
                install_hint='pip install "novel-downloader[music]"',
            ),
            Requirement(
                "tool",
                "N_m3u8DL-RE",
                install_hint="Install N_m3u8DL-RE and add it to PATH.",
            ),
            Requirement(
                "cdm",
                "widevine",
                install_hint="Provide a Widevine .wvd device file.",
            ),
        ),
        "limitations": (
            "drm_protected",
            "hls_stream",
            "requires_cdm",
            "requires_external_tool",
        ),
    },
    "youtube": {
        "download_mode": "third_party",
        "requirements": (
            Requirement(
                "python",
                "ytmusicapi",
                install_hint='pip install "novel-downloader[music]"',
            ),
        ),
        "limitations": ("third_party_service", "region_restricted"),
    },
    "spotify": {
        "download_mode": "third_party",
        "limitations": ("third_party_service", "region_restricted"),
    },
    "qobuz": {
        "download_mode": "external",
        "credential_fields": ("user_auth_token",),
        "limitations": (
            "encrypted_segments",
            "quality_limited_without_credentials",
            "region_restricted",
        ),
    },
    "moov": {
        "download_mode": "external",
        "credential_fields": ("MOOVUUID",),
        "requirements": (
            Requirement(
                "tool",
                "N_m3u8DL-RE",
                install_hint="Install N_m3u8DL-RE and add it to PATH.",
            ),
            Requirement(
                "tool",
                "ffmpeg",
                install_hint="Install ffmpeg and add it to PATH.",
            ),
        ),
        "limitations": ("hls_stream", "requires_external_tool", "region_restricted"),
    },
    "streetvoice": {
        "download_mode": "external",
        "credential_fields": ("csrf-token",),
        "limitations": ("hls_stream",),
    },
    # ---- 凭据只影响音质/歌单，不影响搜索 ----
    "deezer": {
        "credential_fields": ("arl",),
        "limitations": ("quality_limited_without_credentials", "region_restricted"),
    },
    "qq": {
        "credential_fields": ("cookies",),
        "limitations": ("quality_limited_without_credentials",),
        # The vendored QQ client imports orjson at module load. It has no
        # Android wheel, so musicdl.PLATFORM_DEPS hides QQ there; describing
        # the requirement keeps the platform list from hiding it silently.
        "requirements": (
            Requirement(
                "python",
                "orjson",
                install_hint='pip install "novel-downloader[music]"',
            ),
        ),
    },
    "soda": {
        "credential_fields": ("cookies",),
        "limitations": ("quality_limited_without_credentials",),
    },
    "suno": {
        "credential_fields": ("auth_token",),
        "limitations": ("quality_limited_without_credentials",),
    },
    "fma": {
        "credential_fields": ("cookies",),
        "limitations": ("playlists_need_credentials",),
    },
    # ---- 平台本身不提供歌词 ----
    "audius": {"lyrics": False},
    "bilibili": {"lyrics": False},
    "ccmixter": {"lyrics": False},
    "jiosaavn": {"lyrics": False},
    "opengameart": {"lyrics": False},
    "wikimediacommons": {"lyrics": False},
}

#: 已知平台（含因缺依赖而未注册的重档平台）。netease 不在 musicdl 表里。
_KNOWN_KEYS: tuple[str, ...] = ("netease", *ALL_PLATFORMS)

#: 非 musicdl 平台的显示名兜底：注册表被替换时仍能描述已知平台。
_STANDALONE_LABELS = {"netease": "网易云音乐"}


def _display_name(key: str) -> str:
    if key in _REGISTRY:
        return str(_REGISTRY[key][0])
    if key in _STANDALONE_LABELS:
        return _STANDALONE_LABELS[key]
    return str(ALL_PLATFORMS[key][0])


def _group_name(key: str) -> str:
    return "国内" if key == "netease" else str(group_of(key))


def _sort_key(capability: PlatformCapability) -> tuple[int, str]:
    """与下拉列表同序：分组顺序 + 显示名。"""
    group = capability.group
    index = GROUP_ORDER.index(group) if group in GROUP_ORDER else len(GROUP_ORDER)
    return (index, capability.label)


def _requirement_state(
    requirement: Requirement,
    probe: Callable[[Requirement], bool | None] | None,
) -> bool | None:
    return requirement.probe() if probe is None else probe(requirement)


def _build_one(
    key: str,
    *,
    registered: bool,
    probe: Callable[[Requirement], bool | None] | None = None,
) -> PlatformCapability:
    overrides = dict(_CAPABILITY_OVERRIDES.get(key, {}))
    if key == "netease":
        overrides.setdefault("login_required", True)

    mode = str(overrides.get("download_mode", "direct"))
    if mode not in DOWNLOAD_MODES:  # 描述写错时退回最保守的值
        mode = "none"

    requirements = tuple(
        requirement.with_state(_requirement_state(requirement, probe))
        for requirement in overrides.get("requirements", ())
    )
    missing = tuple(
        requirement.name
        for requirement in requirements
        if not requirement.optional and requirement.satisfied is False
    )
    if any(
        requirement.kind == "python"
        and not requirement.optional
        and requirement.satisfied is False
        for requirement in requirements
    ):
        status = CAPABILITY_MISSING_DEPENDENCY
    elif missing:
        status = CAPABILITY_MISSING_TOOL
    elif not registered:
        status = CAPABILITY_UNAVAILABLE
    else:
        status = CAPABILITY_READY

    return PlatformCapability(
        key=key,
        label=_display_name(key),
        group=_group_name(key),
        search=bool(overrides.get("search", True)),
        download=mode != "none",
        download_mode=mode,
        lyrics=bool(overrides.get("lyrics", True)),
        login_required=bool(overrides.get("login_required", False)),
        credential_fields=tuple(overrides.get("credential_fields", ())),
        credential_required=bool(overrides.get("credential_required", False)),
        requirements=requirements,
        limitations=tuple(overrides.get("limitations", ())),
        note=str(PLATFORM_NOTES.get(key, "")),
        registered=registered,
        status=status,
        missing=missing,
    )


def build_capabilities(
    *,
    registered: Iterable[str] | None = None,
    probe: Callable[[Requirement], bool | None] | None = None,
) -> list[PlatformCapability]:
    """构建完整能力矩阵。

    :param registered: 视为「已注册」的平台 key 集合；``None`` 使用当前
        注册表，便于测试在缺依赖场景下构造稳定快照。
    :param probe: 前置条件探测器；``None`` 使用 ``find_spec`` / ``which``。
    :return: 按分组顺序排列的全部已知平台（含不可用平台）。
    """
    if registered is None:
        present = set(_REGISTRY)
        # 注册表可能被测试（或插件）替换/扩充：live 模式下把额外 key 也纳入
        # 描述，保证 available() 与 capabilities() 始终同源。
        keys = _live_keys()
    else:
        present = set(registered)
        keys = _KNOWN_KEYS
    capabilities = [
        _build_one(key, registered=key in present, probe=probe) for key in keys
    ]
    return sorted(capabilities, key=_sort_key)


def _live_keys() -> tuple[str, ...]:
    """已知平台 + 运行时额外注册的平台（保持注册表顺序）。"""
    extra = tuple(key for key in _REGISTRY if key not in _KNOWN_KEYS)
    return (*_KNOWN_KEYS, *extra)


#: 能力快照。注册表被替换（测试、插件）时按 key 集合自动失效重建；
#: 否则每次列平台都会重复 find_spec/which。
_CAPABILITY_CACHE: tuple[frozenset[str], list[PlatformCapability]] | None = None


def _current_capabilities() -> list[PlatformCapability]:
    global _CAPABILITY_CACHE
    signature = frozenset(_REGISTRY)
    if _CAPABILITY_CACHE is None or _CAPABILITY_CACHE[0] != signature:
        _CAPABILITY_CACHE = (signature, build_capabilities())
    return _CAPABILITY_CACHE[1]


def all_capabilities() -> list[PlatformCapability]:
    """全部已知平台的能力描述，包括因缺依赖而未注册的平台。"""
    return list(_current_capabilities())


def capabilities() -> list[PlatformCapability]:
    """当前已注册（可用）平台的能力描述，顺序与 ``available()`` 一致。"""
    return [item for item in _current_capabilities() if item.registered]


def platform_capabilities(
    *, include_unavailable: bool = False
) -> list[PlatformCapability]:
    """能力矩阵入口；``include_unavailable=True`` 时也返回缺依赖的平台。"""
    return all_capabilities() if include_unavailable else capabilities()


def capability(key: str) -> PlatformCapability:
    """按 key 取能力描述（未注册的重档平台也可查）。

    :raises KeyError: 平台 key 不在已知平台表中。
    """
    item = get_capability(key)
    if item is None:
        raise KeyError(f"未知平台：{key}")
    return item


def get_capability(key: str) -> PlatformCapability | None:
    """按 key 取能力描述；未知平台返回 ``None``。"""
    name = str(key)
    for item in _current_capabilities():
        if item.key == name:
            return item
    return None


def capabilities_json(*, include_unavailable: bool = False) -> list[dict[str, Any]]:
    """能力矩阵的 JSON 友好形态（元组已转列表）。"""
    source = all_capabilities() if include_unavailable else capabilities()
    return [item.as_dict() for item in source]


def supports_credentials(key):
    """该平台是否支持通过「平台凭据」配置账号信息。"""
    item = get_capability(key)
    return bool(item and item.credential_fields)


def available() -> list[tuple[str, str]]:
    """返回按分组排序的 [(key, 显示名), ...]。"""
    return [(item.key, item.label) for item in capabilities()]


def available_labels() -> list[tuple[str, str]]:
    """返回按分组排序的 [(key, 下拉标签), ...]，标签形如「国内 · 网易云音乐」。"""
    return [(item.key, f"{item.group} · {item.label}") for item in capabilities()]


def platform_note(key: str) -> str:
    """返回该平台的使用提示（如需账号/网络条件），没有则返回空串。"""
    item = get_capability(key)
    return item.note if item else ""


def create(key: str, **kwargs: Any) -> MusicProvider:
    """按 key 创建 provider 实例。kwargs 透传给构造函数。"""
    if key not in _REGISTRY:
        raise KeyError(f"未知平台：{key}")
    _, builder = _REGISTRY[key]
    return cast("MusicProvider", builder(**kwargs))
