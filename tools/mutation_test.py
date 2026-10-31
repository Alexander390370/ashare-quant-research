"""完整变异测试：注入多种 bug，验证防线是否全面有效。

六家评审的核心要求：只有变异测试能证明防线有效。

注入的 bug 类型：
1. mom20 方向写反（上次真实事故）
2. mom5 方向写反
3. rev1 符号写反
4. vol20 窗口错位（20 -> 10）
5. dev20 符号写反
6. 权重符号写反

每一个都应该让防线变红。如果某个 bug 注入后防线仍全绿，说明防线有漏洞。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# 变异清单：(描述, 原文, 替换后)
MUTATIONS = [
    (
        "mom20 方向写反",
        '"mom20": "$close / Ref($close, 20) - 1",',
        '"mom20": "Ref($close, 20) / $close - 1",',
    ),
    (
        "mom5 方向写反",
        '"mom5": "$close / Ref($close, 5) - 1",',
        '"mom5": "Ref($close, 5) / $close - 1",',
    ),
    (
        "rev1 符号写反",
        '"rev1": "Ref($close, 1) / $close - 1",',
        '"rev1": "$close / Ref($close, 1) - 1",',
    ),
    (
        "vol20 窗口错位（20->10）",
        '"vol20": "Std($close, 20) / Mean($close, 20)",',
        '"vol20": "Std($close, 10) / Mean($close, 10)",',
    ),
    (
        "dev20 符号写反",
        '"dev20": "($close - Mean($close, 20)) / Mean($close, 20)",',
        '"dev20": "(Mean($close, 20) - $close) / Mean($close, 20)",',
    ),
    (
        "vol_ratio 分子分母颠倒",
        '"vol_ratio": "Mean($volume, 5) / Mean($volume, 20)",',
        '"vol_ratio": "Mean($volume, 20) / Mean($volume, 5)",',
    ),
]


def run_validate(quick: bool = True, timeout: int = 180) -> tuple[bool, str]:
    """跑防线，返回 (是否全绿, 输出)。"""
    cmd = [sys.executable, "-m", "qlib_layer.validate"]
    if quick:
        cmd.append("--quick")
    try:
        proc = subprocess.run(
            cmd, cwd=str(_REPO_ROOT),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        # 全绿判定：无 ❌ 且退出码 0
        all_green = ("❌" not in out) and (proc.returncode == 0)
        return all_green, out
    except subprocess.TimeoutExpired:
        return False, "超时"


def main() -> int:
    factors_path = _REPO_ROOT / "qlib_layer" / "factors.py"
    if not factors_path.exists():
        print("factors.py 不存在", file=sys.stderr)
        return 1

    backup = factors_path.with_suffix(".py.fullmut-bak")
    shutil.copy2(factors_path, backup)
    original = factors_path.read_text(encoding="utf-8")

    print("=" * 78)
    print("完整变异测试：注入多种 bug，验证防线有效性")
    print("=" * 78)

    # 先确认基线全绿
    print("\n[基线] 未注入变异时，防线应全绿")
    all_green, out = run_validate()
    if not all_green:
        print("  ❌ 基线就不绿，无法进行变异测试")
        for line in out.splitlines():
            if "❌" in line:
                print(f"      {line.strip()}")
        shutil.copy2(backup, factors_path)
        backup.unlink(missing_ok=True)
        return 1
    print("  ✅ 基线全绿")

    results = []
    try:
        for idx, (desc, old, new) in enumerate(MUTATIONS, 1):
            print(f"\n[{idx}/{len(MUTATIONS)}] 注入：{desc}")
            if old not in original:
                print("  ⚠️  未找到目标代码，跳过")
                results.append((desc, False, "未找到目标代码"))
                continue

            mutated = original.replace(old, new, 1)
            factors_path.write_text(mutated, encoding="utf-8")

            all_green, out = run_validate()

            if all_green:
                print("  🔴 **防线未捕获**（仍全绿）—— 防线有漏洞")
                results.append((desc, False, "防线未变红"))
            else:
                # 找出哪项变红了
                red = [line.strip() for line in out.splitlines() if "❌" in line]
                print(f"  ✅ 防线捕获（{len(red)} 项变红）")
                for line in red[:2]:
                    print(f"      {line}")
                results.append((desc, True, f"{len(red)} 项变红"))

            # 恢复
            factors_path.write_text(original, encoding="utf-8")

    finally:
        shutil.copy2(backup, factors_path)
        backup.unlink(missing_ok=True)
        print("\n已恢复 factors.py")

    # 汇总
    print()
    print("=" * 78)
    print("汇总")
    print("=" * 78)
    caught = sum(1 for _, ok, _ in results if ok)
    for desc, ok, detail in results:
        print(f"  {'✅' if ok else '🔴'} {desc:24s} {detail}")
    print()
    print(f"  {caught}/{len(results)} 个变异被捕获")
    if caught == len(results):
        print("  ✅ 防线对全部已知错误类型有效")
    else:
        print("  ⚠️  防线存在盲区，需要补强")
    print("=" * 78)

    return 0 if caught == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
