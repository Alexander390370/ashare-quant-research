# 筛选层（A 线）：指数成分股 + 硬性过滤

> 这是「简单版」筛选层。它不做因子打分、不预测涨跌，只做一件事：
> **从 5000 只 A 股里，排除掉不能正常交易的，留下一个可复现的候选池。**

---

## 一、它解决什么问题

你的链路里缺一层：

```
财富管理范式（你有）
      ↓
交易方案（你有）
      ↓
【筛选层】← 这里原来是空的
      ↓
深挖层（TradingAgents，已有）
```

**筛选层的作用不是"选出会涨的"，是"排除不该看的"。**

---

## 二、它做什么（四步）

### 第 1 步：拿候选池

用**中证指数官网**的成分股名单（沪深300 + 中证500 = 800 只）。

**为什么用指数成分股：**
- 中证指数公司已经按市值+流动性筛过一轮
- 成分股名单**自带日期** → point-in-time 天然解决
- 实测 8/8 成功，均 1.3-1.7 秒

### 第 2 步：拿股票名称（判 ST）

从交易所拉全 A 代码→名称映射（5577 只，含 197 只 ST）。

### 第 3 步：拿日线

用**腾讯**日线（实测 8/8 成功，均 1.6 秒），用于：
- 算日均成交额（判流动性）
- 判停牌（近期是否全无成交）
- 估上市时长（最早日线日期）

**注意**：东财日线实测 **0/8 不可用**，不要用。

### 第 4 步：跑 4 条硬规则

| 规则 | 判据 | 默认阈值 |
|---|---|---|
| ① 排除 ST | 名称含 ST/*ST/退/PT | — |
| ② 上市时长 | 最早日线距今 | ≥ 12 个月 |
| ③ 流动性 | 近 20 日日均成交额 | ≥ 5000 万 |
| ④ 停牌 | 近 5 日是否全无成交 | — |

**关键设计：规则按「成本从低到高」排序，一旦剔除就短路。** 名称判断无 IO，最先跑。

---

## 三、怎么用

```powershell
cd C:\Users\34246\Desktop\jinrong\TradingAgents
$env:TEMP = "C:\Users\34246\Desktop\jinrong\TradingAgents\_mytemp"
$env:TMP = $env:TEMP
$env:PYTHONIOENCODING = "utf-8"

# 全量跑（800 只，约 25 分钟）
.\.venv\Scripts\python.exe -m screening.run

# 快速验证（只跑前 10 只，约 1 分钟）
.\.venv\Scripts\python.exe -m screening.run --limit 10

# 指定日期
.\.venv\Scripts\python.exe -m screening.run --date 2026-10-30
```

---

## 四、输出什么

全部在 `screening/output/<日期>/` 下：

| 文件 | 内容 |
|---|---|
| `candidates.csv` | 通过的股票 + 关键指标 |
| `rejected.csv` | 被剔除的股票 + **剔除原因**（审计用） |
| `audit.json` | 配置快照、每层统计、数据源信息、耗时 |

### candidates.csv 示例

```csv
code,name,avg_amount_yi,listed_months_est,first_bar,pass_reason,analysis_date
000001,平安银行,14.42,>12,2023-10-31,通过全部规则,2026-10-30
000002,万  科Ａ,15.35,>12,2023-10-31,通过全部规则,2026-10-30
```

### audit.json 关键字段

```json
{
  "analysis_date": "2026-10-30",
  "config_fingerprint": "9390ef6396939679",
  "data_window": {"start": "2023-10-31", "end": "2026-10-30"},
  "data_sources": {"daily_prices": "tencent", "daily_success": 800, "daily_failed": 0},
  "summary": {
    "total": 800,
    "passed": 650,
    "rejected": 150,
    "by_rule": {"exclude_st": 20, "min_avg_amount": 130}
  }
}
```

---

## 五、可复现性（这层的核心验收标准）

**同一天跑两次，输出必须完全一致。**

保证机制：
1. **配置快照**存进 `audit.json`（含指纹）
2. **数据窗口**明确记录
3. **数据源**明确记录
4. **零随机性**——没有采样、没有随机种子

**验证方法：**

```powershell
# 跑两次
.\.venv\Scripts\python.exe -m screening.run
copy screening\output\2026-10-30\candidates.csv a1.csv
.\.venv\Scripts\python.exe -m screening.run
# 对比 a1.csv 与新的 candidates.csv，应该完全一致
```

---

## 六、它不做什么（诚实边界）

| ❌ 不做 | 为什么 | 谁来做 |
|---|---|---|
| 因子打分 | 需要因子假设，那是下一步 | Qlib（B 线） |
| 预测涨跌 | 做不到，也没必要 | 没人能稳定做到 |
| 回测 | 这层只产出名单 | 下一步 |
| 自动喂给深挖层 | 600 只跑不完（10 分钟/只 = 100 小时） | 你人工挑 5-10 只 |

**这层的验收标准只有一条：同一天跑两次，输出一样。**

---

## 七、与 Qlib（B 线）的关系

**不是二选一，是互相验证。**

| | A（本层） | B（Qlib） |
|---|---|---|
| 逻辑 | 4 条硬规则，逐行可读 | 462 因子，黑盒 |
| 产出 | 600 只候选池 | 排序后的 Top N |
| 用途 | **基线**：知道"够用"长什么样 | **能力**：可能选得更准 |
| 验证 | 你自己能看懂 | 需要对照 A 才知道对不对 |

**A 的价值在于：以后用 B 跑出结果，能对照 A 看差异。**

没有 A，你用 B 只能"相信它"，不能"验证它"。

---

## 八、已知限制

1. **上市日期是近似值**：用腾讯日线最早日期，不是真实上市日。但对"上市不满 12 个月"这个判断足够（若最早日期在窗口起点，说明上市早于窗口）。

2. **东财不可用**：`stock_zh_a_hist` 实测 0/8，已改用腾讯。若腾讯也挂了，可切新浪（`data_sources.daily_prices: sina`）。

3. **不含因子**：这层只做排除，不做排序。想要排序，等 B 线。

4. **800 只跑 25 分钟**：主要是日线拉取（每只 1.6 秒 + 0.3 秒间隔）。可通过降低 `request_interval_seconds` 加速，但会提高限流风险。

5. **ST 判断依赖名称**：若交易所改名（如撤销 ST），需重新拉名称。

---

## 九、文件结构

```
screening/
├── __init__.py
├── config.yaml          所有阈值在这里，改这里不改代码
├── fetch.py             数据获取（带重试）
├── filters.py           4 条硬规则（纯逻辑，可单测）
├── run.py               主流程
└── output/
    └── 2026-10-30/
        ├── candidates.csv
        ├── rejected.csv
        └── audit.json
```

配套工具：

```
tools/
├── check_data_sources.py    数据源体检（7 项）
├── stress_data_sources.py   数据源压力测试（每源 8 次）
├── akshare_stable.py        akshare 稳定调用层（重试）
└── compare_ab.py            A/B 交叉验证
```

测试：

```
tests/test_screening_filters.py    22 个单测，覆盖全部规则
```
