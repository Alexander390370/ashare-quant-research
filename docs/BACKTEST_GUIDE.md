# 回测使用说明

> 本机环境（Windows + 代理）下 `TradingAgents` 的回测怎么用、它到底在算什么、
> 以及哪些地方**不能**当真实业绩看。

---

## 一、它算什么

回测不是"跑一遍策略看收益"。它的机制是：

```
对每个 (标的, 分析日期) 单元格：
  1. 用**那一天**的信息跑完整 agent 链，产出一个评级
     （Buy / Overweight / Hold / Underweight / Sell）
  2. 记录这个决策
  3. 等持有期过去后，结算真实收益
     - 入场价 = **决策日之后第一个交易日的收盘价**
       （因为分析会读到当天收盘后的新闻）
     - 计算 raw return 与 **alpha**（相对该标的所属区域的基准指数）
  4. 按评级分组统计：
     - count        样本数
     - hit_rate     方向命中率（Hold 不计，因为它不表态方向）
     - mean_alpha   平均超额收益
```

**关键点：它评的是"决策质量"，不是"策略收益"。** 输出是一张按评级分组的
alpha 表，告诉你"当它说 Buy 时，之后真的跑赢基准了吗"。

---

## 二、怎么用

### 最小验证（先跑这个）

```powershell
cd C:\Users\34246\Desktop\jinrong\TradingAgents
$env:TEMP = "C:\Users\34246\Desktop\jinrong\TradingAgents\_mytemp"
$env:TMP = $env:TEMP
$env:PYTHONIOENCODING = "utf-8"

.\.venv\Scripts\python.exe -m cli.main backtest 600519.SS `
  --start 2026-09-01 --end 2026-09-15 --every 14 --analysts market
```

单标的、2 个日期、只跑技术分析师 —— 这是成本最低的验证。

### 正式回测

```powershell
.\.venv\Scripts\python.exe -m cli.main backtest 600519.SS,000001.SZ `
  --start 2026-06-01 --end 2026-09-01 --every 7 `
  --analysts market,news,fundamentals
```

### 断点续跑

```powershell
# 第一次跑完会打印 run_id，中断后用 --run-id 接着跑
.\.venv\Scripts\python.exe -m cli.main backtest 600519.SS `
  --start 2026-06-01 --end 2026-09-01 --every 7 --run-id <上次的run_id>
```

已完成的单元格会跳过，不会重复烧 token。

### 参数说明

| 参数 | 含义 | 建议 |
|---|---|---|
| `tickers` | 逗号分隔的标的 | 先用 1 个 |
| `--start` / `--end` | 分析日期区间 | 先 2 周 |
| `--every` | 每隔几天取一个分析日 | 7（周频）；日频太贵 |
| `--analysts` | market/sentiment/news/fundamentals | 先只 `market` |
| `--asset-type` | stock / crypto | 默认 stock |
| `--portfolio` | JSON 持仓文件 | 可选，让风控对着真实仓位 |
| `--run-id` | 续跑已有 sweep | 中断时用 |

---

## 三、成本：这是最重要的一节

**每个单元格 = 一次完整 agent 链 = 数十次 LLM 调用。**

本机实测（2026-10-28，600519.SS，只跑 market 分析师）：

```
1 个单元格 ≈ 27 分钟
```

推算完整链（四个分析师 + 多空辩论 + 风控辩论）：

| 配置 | 单元格数 | 粗估耗时 |
|---|---|---|
| 1 标的 × 2 周 × 只 market | 2 | ~1 小时 |
| 1 标的 × 1 月 × 全分析师 | 4 | 数小时 |
| 1 标的 × 3 月 × 周频 × 全分析师 | 13 | **一整天以上** |
| 3 标的 × 3 月 × 周频 | 39 | **数天** |

**所以：先小后大。** 用 `--analysts market` 和 `--every 14` 验证链路通不通，
再逐步扩大。不要一上来就跑大网格。

---

## 四、输出在哪

```
run_data/logs/backtest/<时间戳>/
├── trading_memory.md              决策记录 + 反思（含 alpha 结算）
├── full_states_log_<日期>.json    每个单元格的完整状态
└── trading_memory.md.lock         并发锁
```

`run_data/research/` 下会有汇总结果（跑完才生成）。

**注意：回测不写你的日常 memory log**（`run_data/memory/trading_memory.md`），
它用自己的一份，所以回测的决策不会污染实盘分析的记忆。

---

## 五、读懂输出

`trading_memory.md` 每条记录的格式：

```markdown
**Rating**: Underweight
**Price Target**: 1265.38
**Time Horizon**: 1-3个月

REFLECTION:
The 5-day alpha of -0.8% (raw -0.5%) mildly supports the Underweight call,
but this window is far too short to validate a 1-3 month thesis...
```

**REFLECTION 是最值钱的部分。** 它会：
- 结算真实 alpha
- 判断这个决策"被验证了"还是"还没被验证"
- 指出下次该怎么改进（例：*"state up front that a directional rating needs
  the trigger levels to actually trade before it can be scored"*）

汇总表按评级分组给你 `hit_rate` 和 `mean_alpha`。

---

## 六、必须知道的限制

### 6.1 样本量太小就没有统计意义

2 个单元格、13 个单元格都不足以判断"这套方法有效"。要得出可信结论，通常需要
**几十到上百个样本**。以本机成本算，那是数天到数周的计算量。

**诚实的说法：这套工具目前适合"学流程、看单个决策质量"，不适合"验证策略"。**

### 6.2 A 股的 point-in-time 还没接进回测

这是当前最重要的限制。上游的 `get_balance_sheet` / `get_income_statement`
走 Yahoo 口径，**Yahoo 对 A 股财报不记录公布日期**，所以历史日期回测时财报
会被整体标记 withheld，或者用错数据。

我们新增的 `is_report_available()`（基于巨潮资讯公告日期）**目前没有被 agent
自动调用**。要做严格回测，需要在分析流程里显式加这道门禁。

**这意味着：现在的 A 股基本面回测结果不可信。** 只跑 `--analysts market`
（纯技术面，用 OHLCV）是可信的。

### 6.3 LLM 非确定性

同一标的、同一日期，两次回测结果可能不同。原因：
- 模型采样本身不确定（即使 temperature=0）
- 实时数据会变（新闻、社交情绪每次拉到的内容不同）

上游 README 自己写：*"Treat the framework as a research scaffold for studying
multi-agent analysis, not as a strategy with a fixed, replicable return."*

### 6.4 A 股成交假设失真

入场价用"决策日后第一个交易日收盘价"。但 A 股有 T+1 和 ±10% 涨跌停——
涨停日那个价格现实中买不进。所以 A 股回测的 alpha 会**系统性高估**。

### 6.5 代理必须开着

回测全程要拉数据。代理断了会中断。中断后用 `--run-id` 续跑。

---

## 七、推荐的用法

### 学流程（推荐先做这个）

```powershell
# 单标的、两周、只技术面
.\.venv\Scripts\python.exe -m cli.main backtest 600519.SS `
  --start 2026-09-01 --end 2026-09-15 --every 14 --analysts market
```

看 `trading_memory.md` 里的 REFLECTION，理解"一个决策怎么被事后评价"。

### 看单个决策质量

不做网格，直接跑单次分析，然后人工对照后续走势：

```powershell
.\.venv\Scripts\python.exe -m cli.main --ticker 600519.SS --date 2026-09-01 `
  --analysts market --save --no-show
```

### 不要做的

- ❌ 不要一上来跑几十个单元格的大网格（几天时间 + 大量 token）
- ❌ 不要用 A 股基本面回测结果下结论（point-in-time 未接）
- ❌ 不要把回测 alpha 当业绩预期（样本小、有前视偏差、成交假设失真）

---

## 八、代码层面怎么调用

```python
from tradingagents.backtest import iter_grid, run_backtest, summarize

dates = iter_grid("2026-06-01", "2026-08-01", every_n_days=7)
result = run_backtest(
    ["600519.SS"],
    dates,
    config,
    selected_analysts=["market"],   # 先只跑技术面
)
print(summarize(result).render())
```

`summarize()` 返回 `BacktestSummary`，含每个评级的 `RatingScore`
（`count` / `hit_rate` / `mean_alpha`）以及 alpha 的测量窗口说明。
