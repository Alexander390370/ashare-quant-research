"""akshare 稳定调用层：强制走代理 + 自动重试。

问题（实测 2026-10-30）
-----------------------
本机环境下 akshare 的调用会出现两类失败：

1. **首请求必失败**：代理连接需要预热，第一次请求常报
   `ProxyError: Unable to connect to proxy / RemoteDisconnected`。
   实测走代理连打 5 次：第 1 次 FAIL，第 2-5 次全 OK。

2. **直连必失败**：显式 `proxies=None` 直连时，实测 5/5 全失败
   （`RemoteDisconnected`）。说明东财对直连做了限制，必须经代理。

3. **requests 不读 Windows 系统代理**：只认 `HTTP_PROXY`/`HTTPS_PROXY`
   环境变量。虽然 `netfix.py` 已设置，但某些路径下可能被覆盖。

对策
----
包一层 `call()`：
- 首次失败自动重试（默认 3 次，间隔递增）
- 显式传入代理，不依赖环境变量
- 失败时给出可读诊断，而不是抛原始堆栈

用法
----
    from tools.akshare_stable import call, call_df

    df = call_df(ak.stock_zh_a_hist, symbol="600519", period="daily", ...)
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# 代理地址：优先环境变量，否则用实测可用值
DEFAULT_PROXY = os.environ.get("HTTPS_PROXY") or "http://127.0.0.1:65532"

# 重试参数
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 1.5


class AkshareCallError(RuntimeError):
    """akshare 调用重试后仍失败。"""


def _describe(exc: Exception) -> str:
    """把常见异常翻译成可操作的诊断。"""
    name = type(exc).__name__
    text = str(exc)
    if "ProxyError" in name or "Unable to connect to proxy" in text:
        return (
            f"{name}: 代理不可用。检查代理是否在运行"
            f"（当前 {DEFAULT_PROXY}），或先用 tools/check_data_sources.py 体检。"
        )
    if "RemoteDisconnected" in text:
        return f"{name}: 远端主动断开，通常是源站限流。稍后重试或换数据源。"
    if "JSONDecodeError" in name:
        return f"{name}: 返回不是 JSON，通常是源站返回了 HTML 错误页（被拦或改版）。"
    if "SSLError" in name:
        return f"{name}: TLS 握手失败，可能是代理链路的证书问题。"
    return f"{name}: {text[:120]}"


def call(
    func: Callable[..., Any],
    *args: Any,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
    warmup: bool = True,
    **kwargs: Any,
) -> Any:
    """调用 akshare 函数，自动重试。

    参数
    ----
    func:
        akshare 里的函数，如 `ak.stock_zh_a_hist`
    retries:
        最多尝试次数（含首次）
    backoff:
        重试间隔基数（秒），第 n 次重试等 backoff * n
    warmup:
        首次失败时额外等一会儿再试（代理预热）

    返回
    ----
    函数原始返回值。

    异常
    ----
    AkshareCallError: 全部重试失败，消息里含可读诊断。
    """
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - akshare 异常类型不统一
            last_exc = exc
            if attempt >= retries:
                break
            # 首次失败等久一点：代理预热
            wait = backoff * attempt * (2 if (attempt == 1 and warmup) else 1)
            logger.debug(
                "%s 第 %d/%d 次失败（%s），%.1fs 后重试",
                getattr(func, "__name__", str(func)), attempt, retries,
                type(exc).__name__, wait,
            )
            time.sleep(wait)

    raise AkshareCallError(
        f"{getattr(func, '__name__', str(func))} 调用失败（{retries} 次重试后）："
        + _describe(last_exc)  # type: ignore[arg-type]
    ) from last_exc


def call_df(
    func: Callable[..., Any],
    *args: Any,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
    **kwargs: Any,
) -> Any:
    """同 `call`，但额外校验返回的是非空 DataFrame。"""
    data = call(func, *args, retries=retries, backoff=backoff, **kwargs)
    if data is None:
        raise AkshareCallError(f"{getattr(func, '__name__', str(func))} 返回 None")
    if hasattr(data, "empty") and data.empty:
        raise AkshareCallError(f"{getattr(func, '__name__', str(func))} 返回空表")
    return data


def ensure_proxy_env() -> str:
    """确保代理环境变量存在，返回实际使用的代理。"""
    current = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not current:
        os.environ["HTTP_PROXY"] = DEFAULT_PROXY
        os.environ["HTTPS_PROXY"] = DEFAULT_PROXY
        os.environ["http_proxy"] = DEFAULT_PROXY
        os.environ["https_proxy"] = DEFAULT_PROXY
        current = DEFAULT_PROXY
    return current
