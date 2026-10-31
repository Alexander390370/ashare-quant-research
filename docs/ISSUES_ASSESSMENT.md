# 四个已知问题的可行性评估

> 结论先行：**3 个能解，1 个只能缓解不能根除。**
> 按"投入产出比"排序，附具体改法与实测依据。

---

## 问题 ①：A 股 point-in-time 未接入 agent

### 能解吗：✅ 能，且接口已就绪

### 现状

`tradingagents/dataflows/vendors/akshare/disclosure.py` 里的
`is_report_available(ticker, report_period, as_of)` 已经能正确判断
「某期财报在某个分析日期是否已公布」，实测通过：

```
分析日 2026-08-14 -> 不可见（半年报公布前一天）
分析日 2026-08-15 -> 可见
```

但它**没有被 `get_balance_sheet` 等工具调用**，所以 agent 拿财报时仍走
Yahoo 口径，不受公告日期约束。

### 怎么接

在 `tradingagents/agents/tools.py` 的四个基本面工具入口加一道门禁：

```python
def get_balance_sheet(ticker, freq="quarterly", curr_date=None, trade_date=""):
    as_of = as_of(curr_date, trade_date)
    # A 股：先查公告日期，未公布则明确告知而非用错数据
    if is_a_share(ticker) and as_of:
        if not _statement_is_published(ticker, "balance_sheet", as_of):
            return WITHHELD_NOTICE.format(...)
    return route_to_vendor("get_balance_sheet", ticker, freq, as_of)
```

需要新增一个 `_statement_is_published()`：把「分析日期 as_of」换算成
「最近一期已公布的报告期」。巨潮的公告标题里能解析出报告期类型
（半年报/年报/季报），`infer_period()` 已实现。

### 风险

- 每个基本面工具会多一次巨潮请求（有 6 小时缓存，影响可控）
- 行为改变：某些历史日期会返回 "withheld" 而不是数据 —— 这**正是想要的效果**
- 上游若改动 `tools.py`，补丁需要跟进

### 工作量

小。约 60 行代码 + 10 个单测。**建议做。**

---

## 问题 ②：样本量太小没有统计意义

### 能解吗：⚠️ 部分能 —— 靠算力堆，但成本是硬约束

### 现状

本机实测：**1 个单元格（只跑 market 分析师）≈ 27 分钟**。

| 目标样本 | 只技术面 | 全分析师 |
|---|---|---|
| n=20 | ~9 小时 | 数天 |
| n=50 | ~23 小时 | 一周以上 |
| n=100 | ~45 小时 | 两周以上 |

### 三条降本路径

**路径 A：只跑技术面（立即可用）**

```powershell
--analysts market
```

技术面只需要 OHLCV，一个分析师，成本最低。缺点是丢掉了基本面/新闻维度。

**路径 B：换便宜模型（推荐）**

`.env` 里把 `quick_think_llm` 换成更便宜的：

```ini
TRADINGAGENTS_QUICK_THINK_LLM=deepseek-flash
TRADINGAGENTS_DEEP_THINK_LLM=deepseek-v4-pro
```

analysts / researchers / trader 走 quick 档，managers 走 deep 档。这是上游
本来就设计的双档机制。实测这把 key 只有这两个模型，所以 quick 档已经是
最便宜的选择；若换更便宜的 provider（如方舟 turbo），成本还能降。

**路径 C：减少辩论轮次（已配）**

```ini
TRADINGAGENTS_MAX_DEBATE_ROUNDS=1
TRADINGAGENTS_MAX_RISK_ROUNDS=1
```

已经是 1 轮，没有再降空间（0 轮会破坏流程）。

### 诚实的判断

**n=50 在本机是一周量级的计算。** 这不是代码问题，是"用 LLM 做决策"的固有
成本。如果目标是统计上可信的策略验证，**LLM 多智能体不是合适的工具**——
该用传统量化方法（见最后一节）。

### 可做的改良

写一个**批量网格脚本**，让它挂机跑：

```powershell
# 夜间跑，白天看结果，中断可续
.\.venv\Scripts\python.exe tools\run_backtest_grid.py `
  --tickers 600519.SS,000001.SZ,600036.SS `
  --start 2026-06-01 --end 2026-09-01 --every 7 `
  --analysts market --run-id night01
```

这个脚本可以帮你写。**但堆样本量的收益，可能不如直接上传统量化。**

---

## 问题 ③：A 股成交假设失真（涨停买不进）

### 能解吗：✅ 能，且这是最值得修的一个

### 问题本质

回测入场价 = "决策日之后第一个交易日的收盘价"。但 A 股：
- **T+1**：当天买不能当天卖
- **±10% 涨跌停**：涨停日这个价格**根本买不到**
- 科创板/创业板 ±20%，ST ±5%

所以如果决策日次日涨停，回测假设"以收盘价买入"，现实中你排队都买不到 →
**alpha 系统性高估**。

### 怎么解

**方案 1：可成交性过滤（推荐，简单有效）**

在结算层加规则：如果入场日 `收盘价 == 涨停价`（或涨幅 >= 9.9%），
则该单元格标记为 `unfillable`，**不计入 alpha 统计**。

需要的字段：当日开盘/收盘/最高/最低 + 前收盘。涨停价 = 前收盘 × 1.1（四舍五入到分）。

`akshare` 的 `stock_zh_a_hist` 返回这些字段（需 `adjust=""` 拿不复权价，
因为涨跌停是按不复权价算的）。

**方案 2：改用次日开盘价（更保守）**

如果开盘就涨停，直接标记不可成交。开盘价更接近真实成交价。

**方案 3：加入滑点模型**

在成交价上加固定滑点（如 0.1%~0.3%）+ 冲击成本。这是标准量化做法。

### 数据可得性

实测（2026-10-28）：

```
ak.stock_zh_a_hist(symbol="600519", adjust="")  -> 需要，但东财接口当前限流
ak.stock_zt_pool_em(date=...)                   -> 涨停股池，当前限流
```

**东财接口在本机不稳定**（`ConnectionReset 10054`）。可考虑：
- 加 3 次重试（宏观模块已验证有效）
- 或换新浪接口 `stock_zh_a_daily`（不复权，含涨跌幅）

### 工作量

中等。约 100 行（可成交性判断 + 结算层改造）+ 15 个单测。
**建议做**——这是让 A 股回测数字可信的关键一步。

---

## 问题 ④：LLM 非确定性

### 能解吗：❌ 不能完全解，只能缓解

### 为什么不能根除

三个独立的不确定性源：

1. **模型采样**：即使 `temperature=0`，provider 也不保证逐字节一致；
   推理模型（deepseek-v4-pro 是思考型）波动更大，因为**推理过程本身在采样**
2. **实时数据漂移**：新闻、社交情绪每次拉到的内容不同
3. **不归档 text feeds**：上游 README 明说 *"text feeds are not archived"*，
   所以连"当时看到了什么"都无法复现

第 3 点是**设计取舍**，不是 bug——归档全部文本会让仓库和磁盘爆炸。

### 能做的缓解

**缓解 1：降低 temperature**

```ini
TRADINGAGENTS_TEMPERATURE=0
```

但注意：**思考型模型大多忽略 temperature**（上游 README 明确写了）。要用这个
得换非思考模型。

**缓解 2：多采样取共识**

同一单元格跑 3 次，取评级众数。成本 ×3，但能显著降低单次波动。

**缓解 3：归档关键输入（自己实现）**

在 `get_news` / `get_fundamentals` 返回时，把结果落到
`run_data/snapshots/<ticker>/<date>/`，回测时优先读快照。

这能解决"数据漂移"，但解决不了"模型采样"。工作量中等。

### 诚实的判断

**这条不要试图根除。** 正确的心态是：把 LLM 决策当"一次采样"，用多次采样
的分布看稳定性，而不是追求可复现的单点结果。

上游作者自己的定位就是：*"a research scaffold for studying multi-agent
analysis, not a strategy with a fixed, replicable return."*

---

## 汇总

| 问题 | 能解 | 工作量 | 建议 |
|---|---|---|---|
| ① point-in-time 接入 | ✅ | 小（60 行） | **做** |
| ② 样本量 | ⚠️ 靠算力 | 脚本小、算力贵 | 先小规模验证 |
| ③ 涨停买不进 | ✅ | 中（100 行） | **做** |
| ④ LLM 非确定性 | ❌ 只能缓解 | — | 换心态，不追求复现 |

**优先级：③ > ① > ② > ④**

③ 最直接影响数字可信度（现在 alpha 是系统性高估的），
① 决定基本面维度能不能用，
② 是成本问题不是正确性问题，
④ 是品类固有限制。
