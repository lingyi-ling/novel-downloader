"""平台连通性自检：只搜一次，不解析直链、不下载。

健康检查必须是「有界」的：某些平台（或代理配置错误）会让 requests 长期
卡住。这里用守护线程 + ``join(timeout)`` 给调用方一个硬上界；超时后线程
自行结束，不会阻塞进程退出。调用方拿到的是统一的三字段结果：

``{"ok": bool, "elapsed_ms": int, "error": str}``
"""

from __future__ import annotations

import threading
import time
from typing import Any

_ERROR_LIMIT = 200


def _describe(exc: BaseException) -> str:
    """把底层异常归纳成一句人能看懂的话。"""
    if isinstance(exc, ModuleNotFoundError):
        missing = exc.name or str(exc)
        return f"缺少依赖：{missing}"
    if isinstance(exc, TimeoutError):
        return "搜索超时"
    name = type(exc).__name__
    text = str(exc).strip().replace("\n", " ")
    if text:
        return f"{name}: {text[:_ERROR_LIMIT]}"
    return name


def health_check(
    platform: str,
    keyword: str = "test",
    *,
    timeout: float = 15.0,
    limit: int = 1,
) -> dict[str, Any]:
    """检查某平台能否完成一次搜索。

    :param platform: 注册表里的平台 key。
    :param keyword: 用于探测的关键词。
    :param timeout: 总耗时上限（秒）；超时视为不健康。
    :param limit: 请求的结果条数（只做连通性探测，默认 1）。
    :return: ``{"ok", "elapsed_ms", "error"}``，成功时 ``error`` 为空串。
        搜索成功但零结果也算连通——鉴权型平台没配凭据时就是空结果。
    """
    # 延迟导入，避免 health <-> __init__ 循环依赖，也方便测试替换 create
    from . import create

    started = time.perf_counter()
    box: dict[str, Any] = {}
    bound = max(0.0, float(timeout))

    def run() -> None:
        try:
            try:
                # musicdl provider 支持读写超时；netease 等构造器不接受该参数
                provider = create(platform, timeout=(min(5.0, bound), bound))
            except TypeError:
                provider = create(platform)
            box["result"] = provider.search(keyword, limit=max(1, int(limit)))
        except KeyError:
            box["error"] = ValueError(f"未知平台：{platform}")
        except Exception as exc:  # noqa: BLE001 - 健康检查只报告，不抛出
            box["error"] = exc

    worker = threading.Thread(target=run, name=f"music-health-{platform}", daemon=True)
    worker.start()
    worker.join(bound)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    if worker.is_alive():
        return {
            "ok": False,
            "elapsed_ms": elapsed_ms,
            "error": f"搜索超时（>{bound:g}s）",
        }
    if "error" in box:
        return {"ok": False, "elapsed_ms": elapsed_ms, "error": _describe(box["error"])}
    return {"ok": True, "elapsed_ms": elapsed_ms, "error": ""}
