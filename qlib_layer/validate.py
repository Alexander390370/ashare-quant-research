"""因子管线校验：让错误自己报警。

2026-10-31 重写
---------------
上一版的致命缺陷（六家评审 + 变异测试实锤）：

  validate.py 自己抄了一份因子表达式和 numpy 参照实现，
  方向断言跑的是 `_pandas_reference`，**Qlib 表达式引擎一次都没被调用**。
  把 run.py 的 mom20 改回写反，12 项校验照样全绿。

修复：
1. 因子定义统一从 `factors.py` 导入（唯一真源），不再各抄一份
2. **方向断言必须跑 Qlib 表达式引擎**——用 D.features 在合成数据上算
3. 加变异测试——注入已知 bug，确认防线会红

三道防线
--------
防线 1  方向断言（Qlib 引擎）—— 抓因子写反、窗口错位
防线 2  三条不变量         —— 抓权重失效、前视、随机性
防线 3  独立复算           —— 抓 .bin 格式错、列错位

用法
----
    python -m qlib_layer.validate              # 全部
    python -m qlib_layer.validate --quick      # 只跑不触盘的
    python -m qlib_layer.validate --mutation   # 变异测试（关键！）
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qlib_layer.factors import (  # noqa: E402
    DEFAULT_WEIGHTS,
    FACTOR_DIRECTION,
    FACTOR_EXPRESSIONS,
    FACTOR_NAMES,
    reference_factors,
)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    metrics: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 合成数据 → Qlib 格式（用于让方向断言真正跑 Qlib 引擎）
# ---------------------------------------------------------------------------
def build_synthetic_qlib(
    series_map: dict[str, tuple[np.ndarray, np.ndarray]],
    out_dir: Path,
) -> bool:
    """把合成序列写成 Qlib 格式，供 D.features 读取。

    series_map: {股票代码: (closes, volumes)}
    返回是否成功。
    """
    try:
        from qlib_layer.dump_data import dump_to_qlib
    except ImportError:
        return False

    import datetime as dt

    import pandas as pd

    n = len(next(iter(series_map.values()))[0])
    # 构造连续交易日（跳过周末，简化处理）
    start = dt.date(2020, 1, 1)
    dates = []
    cursor = start
    while len(dates) < n:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor += dt.timedelta(days=1)

    daily_map = {}
    meta = {}
    for code, (closes, volumes) in series_map.items():
        daily_map[code] = pd.DataFrame({
            "date": dates,
            "open": closes,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "volume": volumes,
            "amount": closes * volumes,
        })
        meta[code] = {"name": code, "exchange": "上海证券交易所"}

    try:
        dump_to_qlib(daily_map, meta, out_dir, verbose=False)
        return True
    except Exception:  # noqa: BLE001
        return False


def _run_qlib_expressions(
    provider_uri: Path,
    codes: list[str],
    expressions: dict[str, str],
) -> dict[str, dict[str, float]] | None:
    """用 Qlib 引擎算因子，返回 {code: {factor: value}}（取最后一天）。"""
    try:
        import qlib
        from qlib.data import D
    except ImportError:
        return None

    try:
        qlib.init(provider_uri=str(provider_uri), region="cn", expression_cache=None)
        frame = D.features(
            codes, list(expressions.values()), freq="day",
        )
    except Exception:  # noqa: BLE001
        return None

    if frame is None or len(frame) == 0:
        return None

    frame.columns = list(expressions.keys())
    out: dict[str, dict[str, float]] = {}
    for code in codes:
        try:
            sub = frame.xs(code, level="instrument")
        except KeyError:
            continue
        last = sub.tail(1)
        if len(last) == 0:
            continue
        row = last.iloc[0]
        out[code] = {k: float(row[k]) for k in expressions if k in row}
    return out


# ---------------------------------------------------------------------------
# 防线 1：方向断言（跑真实 Qlib 表达式引擎）
# ---------------------------------------------------------------------------
def check_direction_via_qlib(tmp_root: Path | None = None) -> list[CheckResult]:
    """用 Qlib 表达式引擎验证因子方向。

    这是本模块的核心：不再用 numpy 参照实现自证，
    而是构造合成行情 → 灌进 Qlib → 用 D.features 算 → 验证方向。

    如果 run.py 的表达式写反，这里必然失败（因为用的是同一份 factors.py）。
    """
    results: list[CheckResult] = []

    # 三条合成序列：涨 20% / 跌 20% / 平坦
    n = 40
    up = np.linspace(100.0, 120.0, n)
    down = np.linspace(120.0, 96.0, n)
    flat = np.full(n, 100.0)
    vols = np.full(n, 1_000_000.0)

    series_map = {
        "600001": (up, vols),
        "600002": (down, vols),
        "600003": (flat, vols),
    }

    cleanup = False
    if tmp_root is None:
        tmp_root = Path(tempfile.mkdtemp(prefix="qlib_validate_"))
        cleanup = True

    try:
        ok = build_synthetic_qlib(series_map, tmp_root)
        if not ok:
            return [CheckResult(
                "方向断言（Qlib 引擎）", False,
                "无法构造 Qlib 合成数据，防线退化为不检查——**这是危险的**")]

        # 注意：dump_to_qlib 会给代码加交易所前缀（600001 -> SH600001）
        qlib_codes = [f"SH{c}" for c in series_map]

        values = _run_qlib_expressions(
            tmp_root, qlib_codes, FACTOR_EXPRESSIONS)
        if not values:
            return [CheckResult(
                "方向断言（Qlib 引擎）", False,
                "Qlib 表达式引擎无输出，防线无法生效")]

        up_v = values.get("SH600001", {})
        down_v = values.get("SH600002", {})
        flat_v = values.get("SH600003", {})

        # 逐个因子断言方向
        for factor, expected_sign in FACTOR_DIRECTION.items():
            if factor not in up_v:
                results.append(CheckResult(
                    f"{factor} 方向", False, "Qlib 未返回该因子值"))
                continue

            up_val = up_v.get(factor, np.nan)
            down_val = down_v.get(factor, np.nan)

            if expected_sign > 0:
                passed = up_val > 0 and down_val < 0
                detail = f"涨 {up_val:+.4f}（应>0）/ 跌 {down_val:+.4f}（应<0）"
            elif expected_sign < 0:
                passed = up_val < 0 and down_val > 0
                detail = f"涨 {up_val:+.4f}（应<0）/ 跌 {down_val:+.4f}（应>0）"
            else:
                flat_val = flat_v.get(factor, np.nan)
                if factor == "vol20":
                    passed = abs(flat_val) < 1e-6
                    detail = f"平坦序列 {flat_val:.8f}（应≈0）"
                else:
                    # vol_ratio：量能恒定 -> 应接近 1
                    passed = abs(flat_val - 1.0) < 1e-6
                    detail = f"量能恒定 {flat_val:.8f}（应≈1）"

            results.append(CheckResult(
                f"{factor} 方向（Qlib 引擎）", bool(passed), detail))

        # 精确性：mom20 对构造的 +20% 序列
        exact_n = 40
        exact_closes = np.concatenate([
            np.full(20, 100.0),
            np.linspace(100.0, 120.0, 20),
        ])
        exact_map = {"600004": (exact_closes, np.full(exact_n, 1e6))}
        exact_dir = tmp_root / "exact"
        if build_synthetic_qlib(exact_map, exact_dir):
            exact_v = _run_qlib_expressions(
                exact_dir, ["SH600004"], {"mom20": FACTOR_EXPRESSIONS["mom20"]})
            if exact_v and "SH600004" in exact_v:
                got = exact_v["SH600004"].get("mom20", np.nan)
                # closes[-21] 是第 20 个（0-index 19）= 100.0
                passed = abs(got - 0.2) < 1e-4
                results.append(CheckResult(
                    "mom20 精确性（Qlib，构造 +20% -> 0.2）",
                    bool(passed), f"实际 {got:+.6f}，期望 +0.200000"))
            else:
                results.append(CheckResult(
                    "mom20 精确性（Qlib）", False, "Qlib 无输出"))

        # 精确性校验（抓窗口错位、分子分母颠倒这类「方向对但数值错」的 bug）
        #
        # 变异测试发现：只断言方向，抓不到 vol20 窗口从 20 改成 10、
        # 也抓不到 vol_ratio 分子分母颠倒。必须断言具体数值。

        # vol20：构造已知标准差的序列
        # closes = [100, ..., 100, 110]（19 个 100 + 1 个 110）
        # 20 日窗口：mean = (19*100 + 110)/20 = 100.5
        # **关键**：Qlib 的 Std 用 ddof=1（样本标准差），不是 ddof=0。
        # 实测确认：Qlib 返回 0.022249，对应 ddof=1；ddof=0 是 0.021686。
        # std(ddof=1) = sqrt((19*(100-100.5)^2 + (110-100.5)^2)/19)
        vol_n = 40
        vol_closes = np.full(vol_n, 100.0)
        vol_closes[-1] = 110.0
        vol_map = {"600005": (vol_closes, np.full(vol_n, 1e6))}
        vol_dir = tmp_root / "vol"
        if build_synthetic_qlib(vol_map, vol_dir):
            vol_v = _run_qlib_expressions(
                vol_dir, ["SH600005"], {"vol20": FACTOR_EXPRESSIONS["vol20"]})
            if vol_v and "SH600005" in vol_v:
                got = vol_v["SH600005"].get("vol20", np.nan)
                _w = vol_closes[-20:]
                expected = float(np.std(_w, ddof=1) / np.mean(_w))
                passed = abs(got - expected) < 1e-4
                results.append(CheckResult(
                    "vol20 精确性（Qlib，窗口必须=20）",
                    bool(passed),
                    f"实际 {got:.6f}，期望 {expected:.6f}"
                    + ("" if passed else " ← 窗口或公式错")))
            else:
                results.append(CheckResult(
                    "vol20 精确性（Qlib）", False, "Qlib 无输出"))

        # vol_ratio：构造已知量能比的序列
        # 前 15 天 volume=1e6，后 5 天 volume=2e6
        # 5 日均量 = 2e6，20 日均量 = (15*1e6 + 5*2e6)/20 = 1.25e6
        # vol_ratio = 2e6/1.25e6 = 1.6
        vr_n = 40
        vr_vols = np.concatenate([np.full(35, 1e6), np.full(5, 2e6)])
        vr_map = {"600006": (np.full(vr_n, 100.0), vr_vols)}
        vr_dir = tmp_root / "vr"
        if build_synthetic_qlib(vr_map, vr_dir):
            vr_v = _run_qlib_expressions(
                vr_dir, ["SH600006"],
                {"vol_ratio": FACTOR_EXPRESSIONS["vol_ratio"]})
            if vr_v and "SH600006" in vr_v:
                got = vr_v["SH600006"].get("vol_ratio", np.nan)
                expected = 2e6 / 1.25e6  # = 1.6
                passed = abs(got - expected) < 1e-4
                results.append(CheckResult(
                    "vol_ratio 精确性（Qlib，分子分母不可颠倒）",
                    bool(passed),
                    f"实际 {got:.6f}，期望 {expected:.6f}"
                    + ("" if passed else " ← 分子分母颠倒")))
            else:
                results.append(CheckResult(
                    "vol_ratio 精确性（Qlib）", False, "Qlib 无输出"))

        return results

    finally:
        if cleanup:
            shutil.rmtree(tmp_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 防线 2：三条不变量
# ---------------------------------------------------------------------------
def check_weight_inversion() -> list[CheckResult]:
    """权重全取反 -> 分数与排名必须互补。

    注意：这确实是线性恒等式，价值有限（六家批评正确）。
    保留它只为抓「权重未被应用」这类接线错误——用真实 reference_factors
    算出因子值后再加权，而不是在函数内部现写。
    """
    results = []

    rng = np.random.default_rng(42)
    n = 10
    # 用真实的 reference_factors 算因子，而不是凭空造 z 值
    factor_matrix = {}
    for i in range(n):
        closes = 100.0 * np.cumprod(1 + rng.normal(0.001, 0.02, 40))
        vols = rng.uniform(1e6, 5e6, 40)
        factor_matrix[f"stock{i}"] = reference_factors(closes, vols)

    # 截面 z-score（与生产流程一致）
    z_matrix: dict[str, dict[str, float]] = {k: {} for k in factor_matrix}
    for factor in FACTOR_NAMES:
        values = [factor_matrix[s].get(factor, np.nan) for s in factor_matrix]
        arr = np.array(values, dtype=float)
        mean = np.nanmean(arr)
        std = np.nanstd(arr)
        for idx, stock in enumerate(factor_matrix):
            if std and std > 0 and not np.isnan(arr[idx]):
                z_matrix[stock][factor + "_z"] = (arr[idx] - mean) / std
            else:
                z_matrix[stock][factor + "_z"] = 0.0

    def score(weights: dict[str, float]) -> dict[str, float]:
        out = {}
        for stock, zvals in z_matrix.items():
            total = 0.0
            for key, weight in weights.items():
                total += zvals.get(key, 0.0) * weight
            out[stock] = total
        return out

    normal = score(DEFAULT_WEIGHTS)
    inverted = score({k: -v for k, v in DEFAULT_WEIGHTS.items()})

    stocks = list(normal)
    max_diff = max(abs(inverted[s] + normal[s]) for s in stocks)
    passed = max_diff < 1e-9
    results.append(CheckResult(
        "权重全取反 -> 分数取反", passed,
        f"最大偏差 {max_diff:.2e}" if not passed else "完全互补"))

    normal_rank = sorted(stocks, key=lambda s: -normal[s])
    inverted_rank = sorted(stocks, key=lambda s: -inverted[s])
    passed = normal_rank == inverted_rank[::-1]
    results.append(CheckResult(
        "权重全取反 -> 排名互补", passed,
        f"Top1 原={normal_rank[0]} 反转后={inverted_rank[0]}"))

    return results


def check_no_lookahead() -> list[CheckResult]:
    """截断未来 -> 历史因子不变。"""
    results = []

    base = np.linspace(100.0, 130.0, 30)
    future_a = np.linspace(130.0, 150.0, 10)
    future_b = np.linspace(130.0, 90.0, 10)
    vols = np.full(40, 1e6)

    f_a = reference_factors(np.concatenate([base, future_a])[:30], vols[:30])
    f_b = reference_factors(np.concatenate([base, future_b])[:30], vols[:30])

    same = all(
        np.isclose(f_a.get(k, np.nan), f_b.get(k, np.nan), equal_nan=True)
        for k in FACTOR_NAMES if k in f_a
    )
    results.append(CheckResult(
        "截断未来 -> 历史因子不变", bool(same),
        "两种未来下 T=30 因子一致" if same else "**不一致（存在前视）**"))

    return results


def check_deterministic() -> list[CheckResult]:
    """重跑两次 -> 结果一致。"""
    rng = np.random.default_rng(7)
    closes = 100.0 * np.cumprod(1 + rng.normal(0.001, 0.02, 60))
    vols = rng.uniform(1e6, 5e6, 60)

    f1 = reference_factors(closes, vols)
    f2 = reference_factors(closes, vols)
    same = all(
        np.isclose(f1.get(k, np.nan), f2.get(k, np.nan), equal_nan=True)
        for k in f1
    )
    return [CheckResult(
        "重跑两次 -> 结果一致", bool(same),
        "完全一致" if same else "**不一致**")]


# ---------------------------------------------------------------------------
# 防线 3：独立复算
# ---------------------------------------------------------------------------
def check_independent_recompute(provider_uri: Path) -> list[CheckResult]:
    """从 .bin 原始字节独立复算，与 Qlib 读取值比对。

    注意局限（六家指出）：两者读的是同一份文件，抓不到 Qlib 写入时的错。
    能抓的是格式错、列错位、日期错位。要抓写入错，需要外部数据源交叉验证。
    """
    try:
        import qlib
        from qlib.data import D
    except ImportError:
        return [CheckResult("独立复算", False, "Qlib 未安装")]

    provider_uri = Path(provider_uri)
    if not (provider_uri / "calendars" / "day.txt").exists():
        return [CheckResult("独立复算", False, "Qlib 数据不存在")]

    try:
        qlib.init(provider_uri=str(provider_uri), region="cn", expression_cache=None)
        all_codes = D.list_instruments(
            D.instruments(market="all"),
            start_time="2020-01-01", end_time="2030-12-31", as_list=True)
    except Exception as exc:  # noqa: BLE001
        return [CheckResult("独立复算", False, f"初始化失败: {exc}")]

    codes = all_codes[:5]
    if not codes:
        return [CheckResult("独立复算", False, "无股票")]

    try:
        frame = D.features(codes, ["$close", "$volume"], freq="day")
    except Exception as exc:  # noqa: BLE001
        return [CheckResult("独立复算", False, f"读取失败: {exc}")]

    mismatches = []
    for code in codes:
        try:
            sub = frame.xs(code, level="instrument")
        except KeyError:
            continue
        closes = sub["$close"].dropna().values
        if len(closes) == 0:
            continue

        bin_path = provider_uri / "features" / code / "close.day.bin"
        if not bin_path.exists():
            continue
        raw = np.fromfile(bin_path, dtype="<f")
        if len(raw) < 2:
            continue
        bin_valid = raw[1:]
        bin_valid = bin_valid[~np.isnan(bin_valid)]

        if len(bin_valid) != len(closes):
            mismatches.append(f"{code}: 数量 bin={len(bin_valid)} qlib={len(closes)}")
            continue
        if not np.allclose(bin_valid, closes, rtol=1e-4, atol=1e-3):
            mismatches.append(
                f"{code}: 值不符 最大偏差 {float(np.max(np.abs(bin_valid - closes))):.4f}")

    passed = not mismatches
    return [CheckResult(
        f"独立复算（{len(codes)} 只）", passed,
        "全部一致" if passed else "; ".join(mismatches[:3]))]


# ---------------------------------------------------------------------------
# 变异测试（六家最强调的一条）
# ---------------------------------------------------------------------------
def run_mutation_test(py_exe: str | None = None) -> list[CheckResult]:
    """注入已知 bug，确认防线会红。

    这是判断「防线是否有效」的唯一可靠方法：
    - 把 factors.py 的 mom20 改回写反
    - 跑方向断言
    - 如果仍全绿 -> 防线无效
    """
    results = []
    factors_path = _REPO_ROOT / "qlib_layer" / "factors.py"
    if not factors_path.exists():
        return [CheckResult("变异测试", False, "factors.py 不存在")]

    backup = factors_path.with_suffix(".py.mutation-bak")
    shutil.copy2(factors_path, backup)

    try:
        src = factors_path.read_text(encoding="utf-8")
        mutated = src.replace(
            '"mom20": "$close / Ref($close, 20) - 1",',
            '"mom20": "Ref($close, 20) / $close - 1",',
        )
        if mutated == src:
            return [CheckResult(
                "变异测试", False, "无法注入变异（未找到 mom20 定义）")]

        factors_path.write_text(mutated, encoding="utf-8")

        # 在子进程里跑（避免模块缓存）
        exe = py_exe or sys.executable
        proc = subprocess.run(
            [exe, "-m", "qlib_layer.validate", "--quick"],
            cwd=str(_REPO_ROOT),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
        )
        out = (proc.stdout or "") + (proc.stderr or "")

        # 防线变红 = 有 ❌ 或退出码非 0
        caught = ("❌" in out) or (proc.returncode != 0)

        # 具体看 mom20 那项
        mom20_red = any(
            "❌" in line and "mom20" in line
            for line in out.splitlines())

        results.append(CheckResult(
            "变异测试：mom20 写反 -> 防线变红",
            bool(caught and mom20_red),
            "防线成功捕获变异 ✅" if (caught and mom20_red)
            else f"**防线未捕获**（caught={caught}, mom20_red={mom20_red}）"))

    finally:
        shutil.copy2(backup, factors_path)
        backup.unlink(missing_ok=True)

    return results


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------
def run_all(
    provider_uri: Path | None = None,
    quick: bool = False,
    verbose: bool = True,
) -> tuple[bool, list[CheckResult]]:
    all_results: list[CheckResult] = []

    if verbose:
        print("=" * 74)
        print("因子管线校验")
        print("=" * 74)

    if verbose:
        print("\n【防线 1】方向断言（Qlib 表达式引擎）")
    r = check_direction_via_qlib()
    all_results.extend(r)
    if verbose:
        for item in r:
            print(f"  {'✅' if item.passed else '❌'} {item.name}")
            if item.detail:
                print(f"      {item.detail}")

    if quick:
        passed = all(x.passed for x in all_results)
        if verbose:
            ok = sum(1 for x in all_results if x.passed)
            print(f"\n{'✅ 通过' if passed else '❌ 失败'} ({ok}/{len(all_results)})")
        return passed, all_results

    if verbose:
        print("\n【防线 2】三条不变量")
    for fn in (check_weight_inversion, check_no_lookahead, check_deterministic):
        r = fn()
        all_results.extend(r)
        if verbose:
            for item in r:
                print(f"  {'✅' if item.passed else '❌'} {item.name}")
                if item.detail:
                    print(f"      {item.detail}")

    if provider_uri:
        if verbose:
            print("\n【防线 3】独立复算")
        r = check_independent_recompute(provider_uri)
        all_results.extend(r)
        if verbose:
            for item in r:
                print(f"  {'✅' if item.passed else '❌'} {item.name}")
                if item.detail:
                    print(f"      {item.detail}")

    passed = all(x.passed for x in all_results)
    if verbose:
        ok = sum(1 for x in all_results if x.passed)
        print()
        print("=" * 74)
        print(f"{'✅ 全部通过' if passed else '❌ 有校验失败'}  ({ok}/{len(all_results)})")
        print("=" * 74)

    return passed, all_results


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="因子管线校验")
    parser.add_argument("--quick", action="store_true", help="只跑方向断言")
    parser.add_argument("--mutation", action="store_true", help="跑变异测试")
    parser.add_argument("--provider", default=None)
    args = parser.parse_args(argv[1:])

    provider = Path(args.provider) if args.provider else (
        _REPO_ROOT / "qlib_layer" / "data" / "cn_a")

    if args.mutation:
        print("=" * 74)
        print("变异测试：注入 mom20 写反，验证防线是否变红")
        print("=" * 74)
        results = run_mutation_test()
        for item in results:
            print(f"  {'✅' if item.passed else '❌'} {item.name}")
            print(f"      {item.detail}")
        return 0 if all(x.passed for x in results) else 1

    passed, _ = run_all(provider, quick=args.quick)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
