"""筛选层：数据获取。

只做三件事：
1. 拿指数成分股名单（中证官网，实测 100% 稳定）
2. 拿股票名称（判 ST）
3. 拿日线（腾讯，实测 100% 稳定）——用于算成交额、判停牌、估上市日期

所有请求都带重试 + 请求间隔，避免触发限流。
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# 交易所后缀 -> akshare 前缀
_PREFIX = {"上海证券交易所": "sh", "深圳证券交易所": "sz", "北京证券交易所": "bj"}


class DataFetchError(RuntimeError):
    """数据获取失败。"""


def _retry(
    func,
    *args,
    retries: int = 3,
    backoff: float = 1.5,
    label: str = "",
    **kwargs,
) -> Any:
    """带重试的调用。"""
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt >= retries:
                break
            wait = backoff * attempt
            logger.debug("%s 第 %d/%d 次失败，%.1fs 后重试", label, attempt, retries, wait)
            time.sleep(wait)
    raise DataFetchError(f"{label} 失败（{retries} 次）：{type(last).__name__}: {last}")


def fetch_index_constituents(
    indices: list[dict],
    retries: int = 3,
    backoff: float = 1.5,
    interval: float = 0.3,
) -> list[dict]:
    """拉指数成分股，返回 [{code, name, exchange, index_code, index_name, snapshot_date}]。"""
    import akshare as ak

    rows: list[dict] = []
    for item in indices:
        index_code = item["code"]
        index_name = item.get("name", index_code)
        frame = _retry(
            ak.index_stock_cons_csindex,
            symbol=index_code,
            retries=retries,
            backoff=backoff,
            label=f"成分股 {index_name}",
        )
        if frame is None or len(frame) == 0:
            raise DataFetchError(f"成分股 {index_name} 返回空")

        snapshot = frame.iloc[0].get("日期")
        for _, row in frame.iterrows():
            code = str(row.get("成分券代码", "")).strip()
            if not code:
                continue
            rows.append(
                {
                    "code": code,
                    "name": str(row.get("成分券名称", "")).strip(),
                    "exchange": str(row.get("交易所", "")).strip(),
                    "index_code": index_code,
                    "index_name": index_name,
                    "snapshot_date": str(snapshot),
                }
            )
        logger.info("成分股 %s：%d 只", index_name, len(frame))
        time.sleep(interval)
    return rows


def fetch_stock_names(
    retries: int = 3,
    backoff: float = 1.5,
) -> dict[str, str]:
    """拉全 A 代码->名称映射。用于判 ST。

    实测成功率 7/8（88%），偶发交易所 SSL 失败，重试可解决。
    """
    import akshare as ak

    frame = _retry(
        ak.stock_info_a_code_name,
        retries=retries,
        backoff=backoff,
        label="全A代码名称",
    )
    if frame is None or len(frame) == 0:
        raise DataFetchError("全A代码名称返回空")
    return {
        str(row["code"]).strip(): str(row["name"]).strip()
        for _, row in frame.iterrows()
    }


def fetch_daily(
    code: str,
    exchange: str,
    start: date,
    end: date,
    retries: int = 3,
    backoff: float = 1.5,
    source: str = "tencent",
    adjust: str = "qfq",
) -> Any:
    """拉单只股票日线。

    source:
      tencent -> ak.stock_zh_a_hist_tx（实测 100% 稳定，支持 adjust）
      sina    -> ak.stock_zh_a_daily（实测 100% 稳定，支持 adjust）

    adjust:
      "qfq"  -> 前复权（**默认**）
      "hfq"  -> 后复权
      ""     -> 不复权

    为什么默认前复权
    ----------------
    实测（2026-10-30）复权前后首价差异：

        贵州茅台  1401.79 vs 1488.00  ->  -5.8%
        工商银行     6.15 vs    6.80  ->  -9.6%
        平安银行    10.28 vs   11.43  -> -10.1%
        比亚迪      88.97 vs  271.10  -> -67.2%

    不复权数据在除权日会跳空，`mom20` 会把「分红送转导致的机械下跌」
    读成「暴跌」，z-score 后假极值被推到排序两端，**恰好挤掉真正该入选的票**。
    这是六家评审一致指出的第一优先问题。
    """
    import akshare as ak

    prefix = _PREFIX.get(exchange, "sh")
    symbol = f"{prefix}{code}"
    start_str = start.strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")

    if source == "tencent":
        kwargs = {
            "symbol": symbol,
            "start_date": start_str,
            "end_date": end_str,
        }
        if adjust:
            kwargs["adjust"] = adjust
        frame = _retry(
            ak.stock_zh_a_hist_tx,
            retries=retries,
            backoff=backoff,
            label=f"腾讯日线 {symbol}",
            **kwargs,
        )
    elif source == "sina":
        kwargs = {
            "symbol": symbol,
            "start_date": start_str,
            "end_date": end_str,
        }
        if adjust:
            kwargs["adjust"] = adjust
        frame = _retry(
            ak.stock_zh_a_daily,
            retries=retries,
            backoff=backoff,
            label=f"新浪日线 {symbol}",
            **kwargs,
        )
    else:
        raise ValueError(f"未知数据源: {source}")

    if frame is None or len(frame) == 0:
        raise DataFetchError(f"{symbol} 日线返回空")
    return frame


def fetch_all_daily(
    stocks: list[dict],
    start: date,
    end: date,
    retries: int = 3,
    backoff: float = 1.5,
    interval: float = 0.3,
    source: str = "tencent",
    progress: Any = None,
    workers: int = 10,
    adjust: str = "qfq",
) -> dict[str, Any]:
    """批量拉日线，返回 {code: DataFrame}。

    并发实现（实测 2026-10-30）
    ---------------------------
    串行 vs 并发实测（20 只样本）：

        并发 1  -> 94.9s，成功 11/20（55%）
        并发 5  -> 23.7s，成功 20/20（100%）
        并发 10 -> 19.4s，成功 20/20（100%）
        并发 20 -> 14.2s，成功 20/20（100%）

    **并发不仅快，成功率还更高** —— 串行时连接复用可能有问题。
    默认 workers=10（甜点：快 6 倍，风险可控）。

    interval 在并发模式下改为「提交间隔」，不是「每次请求后等待」。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    result: dict[str, Any] = {}
    failed: list[tuple[str, str]] = []
    total = len(stocks)
    done_count = 0

    def task(stock: dict) -> tuple[str, Any, str | None]:
        code = stock["code"]
        try:
            frame = fetch_daily(
                code, stock["exchange"], start, end,
                retries=retries, backoff=backoff, source=source,
                adjust=adjust,
            )
            return code, frame, None
        except Exception as exc:  # noqa: BLE001 - 单只失败不中断整体
            return code, None, f"{type(exc).__name__}: {str(exc)[:60]}"

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {}
        for stock in stocks:
            futures[executor.submit(task, stock)] = stock
            if interval:
                time.sleep(interval)  # 提交节流，避免瞬时打满

        for future in as_completed(futures):
            code, frame, error = future.result()
            done_count += 1
            if error:
                failed.append((code, error))
                logger.warning("跳过 %s：%s", code, error)
            else:
                result[code] = frame
            if progress:
                progress(done_count, total, code)

    logger.info(
        "日线拉取完成：成功 %d，失败 %d（并发 %d）",
        len(result), len(failed), workers)
    return result


def default_window(lookback_years: int = 3, today: date | None = None) -> tuple[date, date]:
    """默认回看窗口。"""
    end = today or date.today()
    start = end - timedelta(days=365 * lookback_years)
    return start, end
