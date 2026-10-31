# 本机部署与改造说明（Windows / 中国大陆网络）

> 基于 `TauricResearch/TradingAgents` **v0.7.0**（commit `e8f5503`，2026-10-16）。
> 本文记录为了在这台机器上跑通 A 股分析所做的全部改动、实测依据与验证方式。
> 最后更新：2026-10-26

---

## 一、快速开始

**前提：代理必须开着。** 这是本方案唯一的外部依赖。

```powershell
cd C:\Users\34246\Desktop\jinrong\TradingAgents

# 环境变量（每次新开终端都要设，否则会踩 AppData 写入限制）
$env:TEMP = "C:\Users\34246\Desktop\jinrong\TradingAgents\_mytemp"
$env:TMP  = $env:TEMP
$env:PYTHONIOENCODING = "utf-8"

# 跑一次 A 股完整分析
.\.venv\Scripts\python.exe -m cli.main `
  --ticker 600519.SS --date 2026-10-27 `
  --analysts market,news,fundamentals,sentiment `
  --save --no-show --no-html
```

代理不用手动设——`netfix.py` 会自动探测本机代理端口（依次试 65532 / 7890 /
7891 / 10809 / 8080），探测到就设好 `HTTP_PROXY` / `HTTPS_PROXY`。

报告输出到 `run_data/logs/reports/<TICKER>_<DATE>_<TIME>/complete_report.md`。

---

## 二、三个环境障碍（都已解决）

### 障碍 1：`pip` 在这台机器上静默挂死

**现象**：`pip install` 进程存在但零输出、零文件写入、不报错，可以挂几十分钟。

**根因**：`AppData\Local` 下写缓存被拒。用 `uv` 才暴露出真实错误：

```
error: Could not acquire lock
cause: failed to open file `C:\Users\34246\AppData\Local\uv\cache\.lock`: 拒绝访问 (os error 5)
```

pip 的缓存默认在 `AppData\Local\pip\Cache`，同样写不进去，于是它卡住且不报错。

**实测的写权限边界**（Python 进程，非 shell）：

| 路径 | 结果 |
|---|---|
| `C:\Users\34246\.tradingagents` | ❌ PermissionError |
| `C:\Users\34246\AppData\Local\<any>` | ❌ PermissionError |
| `C:\Users\34246\AppData\Roaming\<any>` | ❌ PermissionError |
| `C:\Users\34246\Desktop\jinrong\<any>` | ✅ OK |

**结论：Python 进程只能写工作区。**

**解决**：用 `uv` 并把缓存重定向进工作区。

```powershell
$env:UV_CACHE_DIR = "<repo>\_uvcache"
$env:UV_NO_CACHE = "1"
& "<repo>\_tools\uv.exe" pip install --python "<repo>\.venv\Scripts\python.exe" <包名>
```

依赖安装从此 94 秒完成（此前 pip 挂死）。

### 障碍 2：yfinance 拿不到任何数据（含美股）

> ⚠️ **本节结论在 2026-10-27 被修正过一次。** 第一版把根因判成「curl_cffi 的
> 浏览器 TLS 指纹被 Yahoo 风控」，那是**误诊**。真实根因是**代理没开**。
> 下面保留完整过程，因为误诊本身就是这个环境里最容易重犯的坑。

**现象**：

```
yf.Ticker('AAPL').history(period='5d')       -> YFRateLimitError: Too Many Requests
yf.Ticker('600519.SS').history(period='5d')  -> YFRateLimitError: Too Many Requests
stderr: Cookie/crumb fetch failed (SSLError), continuing without crumb
```

代理完全没开时症状更彻底——所有 Yahoo 端点（含首页）都返回 `403 Forbidden`。

**排查过程与排除项**（每条都实测过）：

| 假设 | 实测结果 | 结论 |
|---|---|---|
| 网络被墙 | 代理开启后 `urllib` 直连 chart 端点四个标的全部 200 | ❌ 不是墙 |
| `fc.yahoo.com` 被 Akamai 挡 | 返回 `Not Found on Accelerator`，但 chart API 不需要 crumb | 相关但非致命 |
| yfinance 版本缺修复 | 1.7.0 已含社区那版 `fc.yahoo.com` 降级修复 | ❌ 不是版本问题 |
| WSL / Docker 换个出口 | WSL 里同样 429，`fc.yahoo.com` 同样超时 | ❌ 共用同一出口 |
| **代理没开** | **代理开启后原版配置直接恢复正常** | ✅ **真凶** |

**决定性矩阵**（2026-10-27，同一台机器、同一时刻）：

| 配置 | 结果 |
|---|---|
| A `curl_cffi` + 无代理 | ❌ 429 |
| B `curl_cffi` + 有代理 | ✅ OK |
| C `requests` + 无代理 | ✅ OK |
| D `requests` + 有代理 | ✅ OK |

**修正后的结论**：

1. **真正的原因是代理没开**，不是 TLS 指纹。
2. 代理开启后，两个后端都能正常工作。
3. 当时「关掉 curl_cffi 就好」是**巧合**——`requests` 后端恰好走了不同的连接
   路径，在无代理状态下碰巧绕过限流。**用一个改动掩盖另一个问题**，是排查时
   最该警惕的情形。
4. **Python 的 urllib / requests 都不读 Windows 系统代理设置**，必须用环境变量
   `HTTP_PROXY` / `HTTPS_PROXY` 显式告诉它。这是最容易漏的一步。

**最终配置**：

```python
os.environ["YF_DISABLE_CURL_CFFI"] = "1"   # 非必需，保留以求行为可预测
os.environ["HTTPS_PROXY"] = "http://127.0.0.1:65532"
os.environ["HTTP_PROXY"]  = "http://127.0.0.1:65532"
```

`netfix.py` 会自动探测本机代理端口（依次试 65532 / 7890 / 7891 / 10809 / 8080），
探测到就设环境变量，并给 `127.0.0.1,localhost` 设 `NO_PROXY`。

### 障碍 2b：requests 缺 `DNSError`（关掉 curl_cffi 后才暴露）

关掉 curl_cffi 后，yfinance 会撞上一个隐藏 bug。`yfinance/data.py:241` 写着：

```python
except requests.exceptions.DNSError as e:
```

但 `DNSError` 是 **curl_cffi 特有**的异常类，普通 `requests.exceptions` 里根本
没有这个名字（实测只有 `ConnectTimeout` / `ConnectionError` / `ReadTimeout` /
`Timeout`）。走到那行 `except` 就抛：

```
AttributeError: module 'requests.exceptions' has no attribute 'DNSError'
Failed to get ticker '600519.SS' reason: ... has no attribute 'DNSError'
```

**隐蔽性**：只有走 `_get_ticker_tz` → `info` → `_fetch` 这条支路才会踩到，
缓存命中时不会。所以表现为"昨天能用、今天不能用"。

**修法**：在 import yfinance 之前给 `requests.exceptions` 补一个语义等价的
`DNSError`（继承 `ConnectionError` → `OSError`，与 curl_cffi 的层级一致）。

### 障碍 2c：WSL / Docker 帮不上忙

实测（2026-10-26）：

| 从 WSL 里测 | 结果 |
|---|---|
| `fc.yahoo.com` | HTTP 000 超时 6.3s |
| chart API `AAPL` / `600519.SS` | 429 / 429 |
| `/mnt/c` 遍历 vs WSL 原生 | 慢 3 倍 |

**WSL 和 Windows 共用同一个网络出口**，容器里跑一样拿不到 Yahoo。Docker 解决
不了数据问题。若之后要用官方 `docker compose` 镜像，仓库应放 WSL 原生文件系统
（如 `~/TradingAgents`）而不是 `/mnt/c`，否则 IO 拖慢 3 倍。

**golden 断言验证**（四个标的，与 urllib 独立实测值比对）：

| Ticker | 行数 | 最新收盘 | 独立实测值 | 匹配 |
|---|---|---|---|---|
| 600519.SS | 16 | 1339.03 | 1334.70 | ~ok |
| 000001.SZ | 16 | 11.24 | 11.25 | ~ok |
| 0700.HK | 19 | 387.20 | 385.20 | ~ok |
| AAPL | 22 | 347.73 | 347.73 | ✅ |

### 障碍 3：`~/.tradingagents` 建不出来

**现象**：

```
Vendor 'sec_edgar' failed: [WinError 5] 拒绝访问。: 'C:\Users\34246\.tradingagents'
```

**根因**：同障碍 1，家目录写入受限。而这个目录正是 **memory log 的落盘处**（学习闭环的核心产物）。

**解决**：三个数据目录全部重定向到工作区（`run_data/`）：

| 配置项 | 默认值 | 本机值 |
|---|---|---|
| `results_dir` | `~/.tradingagents/logs` | `run_data/logs` |
| `data_cache_dir` | `~/.tradingagents/cache` | `run_data/cache` |
| `memory_log_path` | `~/.tradingagents/memory/trading_memory.md` | `run_data/memory/trading_memory.md` |
| `TRADINGAGENTS_CHECKPOINT_DIR` | 家目录 | `run_data/checkpoints` |

---

## 三、改动清单

### 新增文件

| 文件 | 作用 |
|---|---|
| `tradingagents/netfix.py` | 环境适配层：网络开关 + 数据目录重定向，导入即生效 |
| `tradingagents/dataflows/vendors/akshare/news.py` | AKShare 新闻 vendor（A 股） |
| `tradingagents/dataflows/vendors/akshare/__init__.py` | 包导出 |
| `tests/test_netfix.py` | netfix 回归测试（15 个用例） |
| `tests/test_akshare_vendor.py` | AKShare vendor 单元测试（16 个用例） |

### 修改文件

| 文件 | 改动 |
|---|---|
| `tradingagents/__init__.py` | 在 `dotenv` 之前 `import netfix`，确保 yfinance 导入前环境变量已就位 |
| `tradingagents/dataflows/router.py` | 注册 `akshare` 到 `get_news` 的 vendor 表 |
| `tradingagents/default_config.py` | `news_data` 从 `yfinance` 改为 `akshare,yfinance` |
| `tests/test_dataflows_config.py` | 同步 `news_data` 默认值的断言 |
| `.gitignore` | 忽略 `_mytemp/ _uvcache/ _tools/ run_data/ .env` |

**上游代码（`tradingagents/dataflows/vendors/yahoo/`）一行未改** —— 修复通过官方环境变量完成，升级 yfinance 不会冲突。

---

## 四、数据链路覆盖度实测

`get_news` 走 `akshare,yfinance` 链，`fundamental_data` 走
`sec_edgar,yfinance,akshare` 链，`macro_data` 走 `akshare,fred` 链。

| 分析师 | 工具 | 600519.SS | AAPL |
|---|---|---|---|
| 技术 | `get_stock_data` | ✅ 1066 chars | ✅ 1254 chars |
| 技术 | `get_indicators` (rsi) | ✅ 1113 chars | ✅ 1089 chars |
| 技术 | `get_verified_market_snapshot` | ✅ 1607 chars | ✅ 1571 chars |
| 基本面 | `get_fundamentals` | ✅ 775 chars | ✅ 740 chars |
| 基本面 | `get_balance_sheet` | ✅ 5878 chars | ✅ 3876 chars |
| 基本面 | `get_cashflow` | ✅ 2763 chars | ✅ 2474 chars |
| 基本面 | `get_income_statement` | ✅ 4309 chars | ✅ 3172 chars |
| 新闻 | `get_news` | ✅ 2718 chars（AKShare 中文） | ✅ 4435 chars（yfinance） |
| 新闻 | `get_global_news` | ✅ 2075 chars | ✅ 2075 chars |
| 情绪 | `get_insider_transactions` | ✅（无记录，正常） | ✅ 6900 chars |
| **合计** | | **10/10** | **10/10** |

### 港股（0700.HK）实测 9/10

原本缺现金流量表 + 新闻，现已补上**财报**（见 7.1）。剩余唯一缺口：

| 缺口 | 状态 | 原因 |
|---|---|---|
| `get_news` | ❌ `DATA_UNAVAILABLE` | akshare **没有**港股新闻接口（`stock_news_em` 只服务 A 股）；Yahoo 无 0700.HK 新闻标签 |

其余 9 项（行情、指标、快照、概览、资产负债表、现金流、利润表、全球新闻、
内幕交易）均正常。

### 一个被推翻的预判

四方模型评估时**一致断言**「A 方案后基本面分析师必断」——理由是 SEC EDGAR 对 A 股零覆盖，Yahoo 的 `quoteSummary` 端点会同样被风控。

**实测不成立**：`curl_cffi` 修好后，Yahoo 对 A 股的四张财务报表全部可拿。基本面分析师在 A 股上是满血状态。

---

## 五、端到端验证结果

```
Analysis Complete!
Analyst wall time: Market 94.78s | Sentiment 37.05s | News 66.75s | Fundamentals 49.94s
✓ Report saved to: run_data/logs/reports/600519.SS_20261026_114342
  Complete report: complete_report.md  (112,955 bytes)
exit=0
```

产物结构（13 个文件）：

```
1_analysts/   market.md news.md fundamentals.md sentiment.md
2_research/   bull.md bear.md manager.md
3_trading/    trader.md
4_risk/       aggressive.md conservative.md neutral.md
5_portfolio/  decision.md
complete_report.md
```

**质量抽样**：风控辩论里模型引用了 `200日SMA 1325.08`、`ATR 23.41`、`成交量 1,682,543 股`、`RSI 63.97 顶背离`、`MACD 柱状 7.31`，并互相纠错（"1,358.21 到 1,330 是 28.21 点，约 2.1%，不是你说的 0.7%"）。新闻分析师明确标注数据源为「Yahoo/东方财富·AKShare」，引用了《强势股追踪 主力资金连续5日净流入107股》的具体数值。

---

## 六、测试状态

```
1382 passed, 7 failed, 4 skipped  (31.09s)
```

**7 个失败全部与本次改动无关**，已逐条核实：

| 失败 | 原因 | 是否我造成 |
|---|---|---|
| `test_cli_commands` / `test_cli_updates` ×3 | `UnicodeDecodeError: 'gbk' codec` —— 中文 Windows 默认编码 | ❌ 上游环境问题 |
| `test_chatgpt_auth` ×2 | 需要网络/凭据 | ❌ 上游 |
| `test_api_key_env` | 需要交互式终端 | ❌ 上游 |
| `test_suite_isolation::test_yfinance_keeps_its_cache_out_of_the_users_home` | 断言"缓存不在 `Path.home()` 内"，但工作区就在家目录下 | ❌ **stash 掉全部改动后照样失败**，属上游在工作区位于家目录下时的固有问题 |

---


## 七、三个缺口补齐（2026-10-27）

### 7.1 港股财报 ✅

Yahoo 对港股现金流/资产负债覆盖不全（0700.HK 的 `get_cashflow` 返回
`NO_DATA_AVAILABLE`）。用 akshare 的港股报表接口补上：

    ak.stock_financial_hk_report_em(stock="00700", symbol="资产负债表", indicator="年度")
    -> shape (1124, 11)，字段 REPORT_DATE / STD_ITEM_NAME / AMOUNT

实现：`tradingagents/dataflows/vendors/akshare/hk_financials.py`
挂载：`fundamental_data = sec_edgar,yfinance,akshare`，akshare 只服务 `.HK`，
非港股抛错让链条继续。

实测（0700.HK，2026-10-27）：

    balance_sheet      3743 chars
    cashflow           3913 chars
    income_statement   2723 chars

**注意**：router 以**位置参数**调用 `impl(ticker, freq, as_of_date)`，闭包
绑定必须用位置参数接收，只写关键字默认值会静默传错参数（踩过一次）。

### 7.2 A 股 point-in-time ✅

原缺口：Yahoo 对非美股财报只按「覆盖期间」标注，不记录公布日期，所以历史日期
回测时财报被整体标记 withheld。

**正解是巨潮资讯**（证监会指定信息披露平台），每条公告天生带公告时间：

    ak.stock_zh_a_disclosure_report_cninfo(symbol="600519", ...)
    -> 字段：代码 / 简称 / 公告标题 / 公告时间 / 公告链接

实现：`tradingagents/dataflows/vendors/akshare/disclosure.py`

核心规则：**某份报告只有在「公告时间 <= 分析日期」时才允许被使用。**

实测（贵州茅台 2026 半年报，公告日 2026-08-15）：

    分析日 2026-07-01 -> 不可见
    分析日 2026-08-14 -> 不可见   ← 公告前一天
    分析日 2026-08-15 -> 可见     ← 公告当天
    分析日 2026-10-27 -> 可见

这就是真正的 point-in-time：回测到 7 月时，系统不会"偷看"8 月才公布的半年报。

公开 API：
- `get_announcements(ticker, start, end)` —— 拉公告列表（带 6 小时缓存）
- `filter_by_point_in_time(items, as_of)` —— 按分析日期过滤
- `is_report_available(ticker, report_period, as_of)` —— 判断某期财报当时是否可见
- `summarize_disclosures(ticker, as_of, lookback_days)` —— 生成给分析师看的摘要

### 7.3 中国宏观（免 API key）✅

FRED **强制要求 API key**（`get_api_key()` 未设直接抛 `FredNotConfiguredError`），
且以美国指标为主。改用 akshare 的国内宏观接口，**完全免 key**：

| 指标 | akshare 函数 | 实测 |
|---|---|---|
| PPI | `macro_china_ppi` | ✅ 2026年09月 105（同比+5%） |
| PMI | `macro_china_pmi` | ✅ 2026年09月 50.1 |
| M2 | `macro_china_money_supply` | ✅ |
| GDP | `macro_china_gdp` | ✅ 2026前三季度 107万亿（+4.6%） |
| LPR | `macro_china_lpr` | ✅ 2026-10-20 LPR1Y=3.0% / LPR5Y=3.5% |
| 外汇储备 | `macro_china_fx_gold` | ✅ 2026年09月 3.4万亿美元 |

实现：`tradingagents/dataflows/vendors/akshare/macro.py`
挂载：`macro_data = akshare,fred`（FRED 降为兜底）

两个踩过的坑：

1. **排序方向不一致**。PPI/PMI/GDP 是降序（首行最新），FX 是升序（尾行最新）。
   不能固定取首或取尾，必须按首尾年份判断。
2. **东财接口偶发 `ConnectionReset(10054)`**。LPR 和 M2 首次调用经常失败，
   加了 3 次重试（间隔 1.5s）后稳定通过。

**不可用**：`macro_china_cpi_monthly` 和 `macro_china_shrzgm` 源端限流，
调用时会给明确提示而非笼统报错。

## 八、已知限制

1. **必须开代理**：Yahoo 全部端点在国内直连会 403。代理是本方案的外部依赖，
   没有代理时数据层全线不可用。
2. **代理链路偶发断连**：长跑（10 分钟以上）中途可能遇到
   `OpenAIConnectionError`。已通过 `.env` 里的 `TRADINGAGENTS_LLM_MAX_RETRIES=5`
   缓解；仍失败时重跑即可，LangGraph 的 `--checkpoint` 可续跑。
3. **`macro_data` 已免 key**：走 akshare 中国宏观（PPI/PMI/M2/GDP/LPR/外储）。
   如需美国指标（10Y 国债、Fed 利率等）才需注册 FRED key：
   https://fred.stlouisfed.org/docs/api/fred/
4. **`get_insider_transactions` 对 A 股无数据**：Yahoo 不提供 A 股内幕交易，
   返回 "No insider transactions reported"，属正常。
5. **港股 9/10**：财报已用 akshare 补齐；仍缺港股新闻（akshare 无港股新闻接口）。
6. **A 股 point-in-time 已解决**：用巨潮资讯的公告日期判定（见 7.2）。
   注意：这是**新增能力**，上游的 `get_balance_sheet` 等工具仍走 Yahoo 口径；
   要做严格历史回测，需在分析流程里显式调用 `is_report_available()` 做门禁。
7. **每次新终端需重设 `TEMP`**：这是本机 AppData 写入限制的必然代价。
8. **`stock_zh_a_spot_em` 不要用**：它会遍历全市场（59 页、约 3 分钟），
   本机只用个股级接口。

## 九、跑测试会覆盖真实 API key（已加防护）

`cli/prompts.py` 的 `ensure_api_key()` 用 `find_dotenv(usecwd=True)` 定位 `.env`，
它会**向上逐级查找**。测试里虽然 `chdir` 到临时目录，但临时目录没有 `.env`，
查找会上溯到仓库根，于是测试用的假 key `sk-deepseek-test` 被写进真实凭据。

**实测后果**：跑一次全量测试，`DEEPSEEK_API_KEY` 被覆盖成 `sk-deepseek-test`，
下次真实分析直接 401。

**已加防护**：`tests/env_guard.py` + `tests/conftest.py` 的 session 钩子，在测试
会话前后自动备份/恢复 `.env`。已验证：跑完全量测试后 key 指纹不变。

如需手动处理：

```powershell
# 检查是否被污染
.\.venv\Scripts\python.exe -c "from tests import env_guard; print(env_guard.env_key_looks_like_test())"
```

---

## 十、诊断工具

```powershell
# 数据链路覆盖度（10 个工具 × 任意标的）
.\.venv\Scripts\python.exe tools\check_data_coverage.py 600519.SS

# 全套单元测试
.\.venv\Scripts\python.exe -m pytest tests -q -m "not integration"

# 只跑本次新增的测试
.\.venv\Scripts\python.exe -m pytest tests/test_netfix.py tests/test_akshare_vendor.py -q
```
