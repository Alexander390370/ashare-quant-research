# Qlib 层（B 线）：因子排序

> A 线（`screening/`）负责「排除」，产出 793 只候选池。
> B 线负责「排序」，用因子给这 793 只打分，取 Top N。

---

## 一、它做什么

```
A 线：800 只指数成分股 → 4 条硬规则 → 793 只候选
                                      ↓
B 线：793 只 → Alpha158 风格因子 → 打分排序 → Top 20
```

**关键：B 线不训练模型。** 只借 Qlib 的因子计算能力，做截面排名。这是「仅筛选模式」——绕开模型训练的复杂度。

---

## 二、为什么不用 Qlib 原生数据格式

Qlib 官方用 `dump_bin.py` 转换数据，但**该脚本不在 pip 包里**（在 GitHub 仓库的 `scripts/` 目录）。

所以本层直接用 `FileFeatureStorage` 的底层格式写入。

### Qlib 的 `.bin` 格式（踩过的坑）

源码 `qlib/data/storage/file_storage.py`：

```python
np.hstack([index, data_array]).astype("<f").tofile(fp)
```

**第一个 float32 是起始索引，后面才是数据。**

**我第一次直接写裸数组，结果 Qlib 读出来是空 DataFrame**（`sample_rows: 0`）。加上头部索引后立刻正常（`sample_rows: 727`，收盘价 1392.05 与腾讯原始数据一致）。

### 目录结构

```
qlib_layer/data/cn_a/
├── calendars/day.txt              交易日历（每行一个日期）
├── instruments/all.txt            股票列表（代码\t起始\t结束）
└── features/
    └── SH600519/
        ├── close.day.bin          727+1 个 float32
        ├── open.day.bin
        ├── high.day.bin
        ├── low.day.bin
        ├── volume.day.bin
        └── amount.day.bin
```

---

## 三、用的因子

只取了 6 个可直接用的表达式（完整 Alpha158 需要训练流程）：

| 因子 | 表达式 | 含义 |
|---|---|---|
| `rev1` | `Ref($close,1)/$close - 1` | 1 日反转 |
| `mom5` | `Ref($close,5)/$close - 1` | 5 日动量 |
| `mom20` | `Ref($close,20)/$close - 1` | 20 日动量 |
| `vol20` | `Std($close,20)/Mean($close,20)` | 20 日波动率 |
| `vol_ratio` | `Mean($volume,5)/Mean($volume,20)` | 量能变化 |
| `dev20` | `($close-Mean($close,20))/Mean($close,20)` | 偏离度 |

### 合成权重（可改）

```python
weights = {
    "mom5_z": 1.0,       # 短期动量正向
    "mom20_z": 1.0,      # 中期动量正向
    "vol20_z": -1.0,     # 低波动优先
    "dev20_z": -1.0,     # 偏离均线少优先（不追高）
    "vol_ratio_z": 0.5,  # 量能温和放大
}
```

**注意：这是「动量 + 低波动」风格，不是价值风格。** 想换成价值/质量，需要加财务因子（下一步）。

---

## 四、怎么用

```powershell
cd C:\Users\34246\Desktop\jinrong\TradingAgents
$env:TEMP = "C:\Users\34246\Desktop\jinrong\TradingAgents\_mytemp"
$env:TMP = $env:TEMP
$env:PYTHONIOENCODING = "utf-8"

# 全流程（约 25 分钟，主要是拉日线）
.\.venv\Scripts\python.exe -m qlib_layer.run

# 快速验证（只跑 20 只，约 3 分钟）
.\.venv\Scripts\python.exe -m qlib_layer.run --limit 20

# 跳过灌数据（已有 Qlib 数据时，只要几秒）
.\.venv\Scripts\python.exe -m qlib_layer.run --skip-dump

# 自定义 Top N
.\.venv\Scripts\python.exe -m qlib_layer.run --top 30
```

**前置条件**：必须先跑 A 线（`python -m screening.run`），B 线读 A 的候选名单。

---

## 五、输出

`qlib_layer/output/<日期>/`：

| 文件 | 内容 |
|---|---|
| `factor_scores.csv` | 所有股票的因子值与合成分 |
| `top20.csv` | 排序后的前 20 |
| `audit.json` | 统计、因子配置、耗时 |

### top20.csv 示例

```csv
qlib_code,composite_score,mom5,mom20,vol20,dev20,vol_ratio
SZ000657,+4.9789,0.1234,0.5968,0.1209,0.2345,1.234
SZ000301,+3.5703,0.0567,0.2628,0.0604,0.1234,1.123
```

---

## 六、A/B 对比

```powershell
.\.venv\Scripts\python.exe tools\compare_ab.py
```

**A 和 B 不是二选一：**

| | A（筛选层） | B（Qlib） |
|---|---|---|
| 逻辑 | 4 条硬规则，逐行可读 | 6 个因子，权重可调 |
| 产出 | 793 只候选池 | 排序后的 Top 20 |
| 用途 | **基线**：知道"够用"长什么样 | **能力**：可能选得更准 |
| 可验证性 | 你自己能看懂 | 需对照 A 才知道对不对 |

**A 的价值**：以后用 B 跑出结果，能对照 A 看差异。没有 A，你用 B 只能「相信它」。

---

## 七、已知限制

1. **不复权**：腾讯日线未复权，除权日会出现价格跳空，影响动量因子。**这是当前最大缺陷。** 修法：用 `stock_zh_a_hist(adjust="qfq")`（但东财挂了），或自己算复权因子。

2. **只有价格量因子**：没有财务因子（ROE/PE/现金流），所以是「动量+低波动」风格，不是价值风格。

3. **不训练模型**：直接用原始因子值排序，没做 IC 检验、没做因子正交化。这是「够用」而非「最优」。

4. **截面只有 793 只**：不是全市场 5000 只。池子来自指数成分股。

5. **Qlib 数据格式脆弱**：手写 `.bin` 依赖 Qlib 内部格式。若 Qlib 升级改变格式，需同步修改 `dump_data.py`。

6. **拉数据慢**：793 只 × (1.6 秒 + 0.3 秒间隔) ≈ 25 分钟。可降低间隔加速，但会提高限流风险。

---

## 八、文件结构

```
qlib_layer/
├── __init__.py
├── dump_data.py        A 股数据 → Qlib .bin 格式
├── run.py              主流程（灌数据 + 算因子 + 排序）
├── data/
│   └── cn_a/           Qlib 数据（约 793 只 × 6 字段）
└── output/
    └── 2026-10-30/
        ├── factor_scores.csv
        ├── top20.csv
        └── audit.json
```

---

## 九、下一步能做什么

| 方向 | 说明 | 工作量 |
|---|---|---|
| **修复权** | 当前最大缺陷，影响动量因子准确性 | 半天 |
| **加财务因子** | ROE/PE/现金流，从动量风格转价值风格 | 1-2 天 |
| **因子 IC 检验** | 看哪个因子在 A 股真的有效 | 半天 |
| **回测** | 验证 Top20 的历史表现 | 1 天 |
| **影子组合** | 记录被排除股票的表现（六家评审强调的） | 半天 |
