# SEPA Trend Screener Bot (v1.5)

## 1. 项目简介
本项目是一个基于 Python 开发的**双市场自动化股票筛选系统**，核心逻辑严格遵循传奇交易大师 **Mark Minervini** 的 **SEPA (Specific Entry Point Analysis)** 趋势模板，通过"周线闸门 + 日线核心"两层过滤，自动筛选处于强势增长阶段的标的。

| 市场 | 脚本 | 数据源 | 每日运行（北京时间） |
|:---|:---|:---|:---|
| 美股 | `sepa_screener_bot.py` | finviz + yfinance | 06:00（美股盘后） |
| A股 | `sepa_a_share_bot.py` | akshare（东方财富） | 16:30（A股盘后） |

### v1.5 更新内容 (2026-09-01)

**新增 A 股筛选版本：**
* 新增 `sepa_a_share_bot.py`：完整 A 股 SEPA 筛选流程（周线闸门 + 日线核心 + 差一步追踪），数据源 akshare。
* 新增 `config_a_share.yaml`：A 股专属策略参数。
* 新增 `.github/workflows/run_a_share.yml`：A 股独立定时任务（工作日 16:30）。
* A 股适配：排除 ST 股、前复权数据、沪深交易所交易日历（XSHG）、休市通知。

**功能变更：**
* **移除行业筛选**（美股 + A股）：初筛不再排除特定行业，候选池回归全行业覆盖。`config.yaml` / `config_a_share.yaml` 中的 `exclude_industries` 已注释停用。
* **修复 A 股基准指数接口**：沪深 300 改用指数接口 `stock_zh_index_daily_em("sh000300")`（新浪接口兜底），修复此前"指数数据为空 → 全部标的指数对齐失败 → 0 入选"的问题。
* **A 股请求量优化**：
  * 每只股票只请求 **1 次 2 年日线**，周线由日线重采样推导（`daily_to_weekly`）；
  * **8 线程并发下载**（`download_a_share_batch`）+ 3 次失败重试 + 随机延时；
  * 请求量 ~3000 次 → **~2500 次**，运行时长 40-75 分钟 → **5-12 分钟**。
* `requirements.txt` 新增 `akshare` 依赖。

**美股与 A 股参数对比：**

| 参数 | 美股 | A股 | 调整原因 |
|:---|:---|:---|:---|
| 市值范围 | $50亿 ~ $2000亿 | ¥50亿 ~ ¥10000亿 | A股市值分布不同 |
| 止损位 | 7% | 5% | T+1 风险，收紧止损 |
| 保本位 | 14% | 10% | 同步收紧 |
| RSI 上限 | 70 | 72 | A股散户多，RSI 易钝化 |
| 放量倍数 | 1.0x | 1.3x | A股量能信号更重要 |
| 最大推荐数 | 3 | 3 | 保持一致 |
| 基准指数 | SPY | 沪深 300 | 对应市场基准 |
| 运行时间 | 北京 06:00 | 北京 16:30 | 各自收盘后数据更新 |

### v1.4 更新内容 (2026-08-30)

**性能优化：**
* **yfinance 批量下载**：新增 `download_batch()`，按 50 只分批批量下载周线/日线数据，请求次数从 ~400 次降到 ~8 次，降低限流概率并大幅提速；单批失败自动回退单只下载。

**推送升级：**
* **Server 酱微信推送 → SMTP 邮件推送**：改用 QQ 邮箱 SMTP（`smtplib` 标准库），正文不受 5KB 限制，支持多收件人。
* 新增 `md_to_plain()` 将 Markdown 转为对齐纯文本。
* GitHub Secrets 变更：删除 `SERVER_CHAN_SENDKEY`，新增 `SMTP_USER` / `SMTP_PASS`（QQ 邮箱授权码）/ `SMTP_TO`。

**新增功能：**
* **非交易日跳过运行**：`exchange_calendars` 日历（美股 XNYS / A股 XSHG）判断交易日，休市自动跳过并发通知。
* `requirements.txt` 新增 `exchange_calendars`。

### v1.3 更新内容 (2026-08-26)

**Bug 修复：**
* **修复初筛 Ticker 解析错误（候选标的骤减的根因）**：finviz.com 2026 年改版后，ticker 单元格内新增 logo 首字母占位 `<span>`，`finvizfinance` 库解析导致 Ticker 重复首字母（如 `AAOI` → `AAAOI`，上游 issue #158）。已加入兼容补丁，从链接 `href="stock?t=XXX"` 提取真实代码。
* **修复市值二次校验被 yfinance 限流静默吞掉**：`YFRateLimitError` 时原 `except: continue` 会静默丢弃候选，改为直接使用 finviz 自带 `Market Cap` 列过滤。

**功能优化：**
* 初筛流程增加**三层排查打点**（原始返回 / 市值过滤），数量异常时一眼定位。
* 新增 `diagnose_screener.py` 独立诊断脚本。
* `requirements.txt` 锁定依赖版本，避免 Actions 每次装最新版引入漂移。

### v1.2 更新内容 (2026-07-08)

**Bug 修复：**
* 修复 `DAILY_BIAS_LIMIT`、`HOURLY_RSI_LOW` 等未定义变量导致的 `NameError`。
* 修复 `config.yaml` 中 `timing` 段重复导致的参数混乱。
* 裸 `except:` 替换为 `except Exception:`。

**功能优化：**
* 移除不可用的小时线择时，流程精简为"周线闸门 → 日线核心"。
* 周线趋势判断改为线性回归斜率检测（`np.polyfit`），降低误杀率。
* 简化日线 RS 条件，避免误杀反转初期标的。

**新增功能：**
* **"差一步"标的追踪**：周线或日线仅差 1 个条件未满足的标的单独列出，附具体失败原因与数值。
* 筛选函数逐条件返回失败明细，便于复盘复核。

## 2. 策略逻辑 (Strategy Logic)
系统通过两个维度的过滤确保标的处于强势增长阶段：

### 2.1 趋势模板过滤 (Trend Template)
* **均线排列**：MA50 > MA150 > MA200。
* **形态验证**：MA200 持续向上、价格在合理乖离率范围内。

### 2.2 两周期校验系统

**周线闸门（Gate）— 4 个条件：**

| 条件 | 说明 |
|------|------|
| MA20 趋势 | 近 12 周线性回归斜率 > 0 |
| MA50 趋势 | 近 12 周线性回归斜率 > 0 |
| 价格排列 | Close > MA20 > MA50 |
| 乖离率 | Close / MA50 ≤ 1.4（可配置） |

**日线核心（Core）— 6 个条件：**

| 条件 | 说明 |
|------|------|
| 均线多头排列 | MA50 > MA150 > MA200 |
| 突破确认 | 收盘价 > 前 10 日最高点 |
| 成交量放量 | 当日成交量 ≥ MA20 均量 × 倍数 |
| 流动性 | 20 日最小成交量 ≥ 50 万 |
| 乖离率风控 | 价格 / MA200 ≤ 1.5（可配置） |
| RSI 风控 | RSI14 < 70（可配置） |

### 2.3 差一步追踪（near-miss）
* 周线或日线**只差 1 个条件**未满足的标的单独列出，附失败关卡和实时数值。
* 按"日线优先"排序，邮件推送最多展示 15 只。

### 2.4 数据获取方式
* **美股**：finviz 初筛 → yfinance 批量下载（50 只/批，`download_batch`）。
* **A股**：akshare 实时行情初筛 → 8 线程并发下载 2 年日线（`download_a_share_batch`），周线由日线重采样推导（`daily_to_weekly`），基准指数用沪深 300 指数接口（`get_index_data_a_share`）。


## 3. 系统架构与参数配置
系统采用 **"逻辑-配置分离"** 设计，策略参数分别在 `config.yaml`（美股）和 `config_a_share.yaml`（A股）中定义。

### 3.1 美股参数（config.yaml）

| 模块 | 核心参数 (YAML Key) | 默认值 | 说明 |
|:---|:---|:---|:---|
| 初筛 | `market_cap_min` / `market_cap_max` | 5000 / 200000 (M) | 市值范围（百万美元） |
| 周线 | `weekly_bias_limit` | 1.40 | 周线乖离率上限 |
| 日线 | `daily_bias_limit` | 1.50 | 日线乖离率上限 |
| 日线 | `daily_rsi_limit` | 70 | 日线 RSI 上限 |
| 交易 | `stop_loss_pct` | 0.07 | 止损位（7%） |
| 交易 | `break_even_pct` | 0.14 | 保本位（14%） |
| 交易 | `max_positions` | 3 | 每日最大推荐数 |
| 基准 | `benchmark` | SPY | 参考指数 |
| 基准 | `vol_mult` | 1.0 | 成交量放大倍数 |

> v1.5 起 `exclude_industries`（行业筛选）已停用。

### 3.2 A股参数（config_a_share.yaml）

| 模块 | 核心参数 (YAML Key) | 默认值 | 说明 |
|:---|:---|:---|:---|
| 初筛 | `market_cap_min` / `market_cap_max` | 50 / 10000 | 市值范围（亿元） |
| 初筛 | `price_min` | 3 | 最低股价（元） |
| 初筛 | `min_turnover` | 1e8 | 最低成交额（元） |
| 初筛 | `exclude_st` | true | 排除 ST/*ST 股 |
| 周线 | `weekly_bias_limit` | 1.35 | 周线乖离率上限 |
| 日线 | `daily_bias_limit` | 1.45 | 日线乖离率上限 |
| 日线 | `daily_rsi_limit` | 72 | 日线 RSI 上限 |
| 交易 | `stop_loss_pct` | 0.05 | 止损位（5%，T+1 收紧） |
| 交易 | `break_even_pct` | 0.10 | 保本位（10%） |
| 交易 | `max_positions` | 3 | 每日最大推荐数 |
| 基准 | `benchmark` | 000300 | 沪深 300 指数 |
| 基准 | `vol_mult` | 1.3 | 成交量放大倍数 |

## 4. 自动化流程 (CI/CD Workflow)

### 4.1 美股 workflow（`.github/workflows/run_daily.yml`）
1. **定时触发**：UTC 22:00（北京时间次日 06:00，美股盘后）。
2. **环境构建**：安装 `requirements.txt`（锁定版本）。
3. **策略执行**：`python sepa_screener_bot.py`，注入 SMTP 三环境变量。
4. **数据回写**：`sepa_history_signals.csv` 自动 commit 回仓库。
5. **即时推送**：SMTP 邮件（入选 + 差一步 + 休市通知）。

### 4.2 A股 workflow（`.github/workflows/run_a_share.yml`）
1. **定时触发**：UTC 08:30（北京时间 16:30，工作日 `1-5`）。
2. **策略执行**：`python sepa_a_share_bot.py`，同样注入 SMTP 变量。
3. **数据回写**：`sepa_a_share_signals.csv` 自动 commit。
4. **推送**：SMTP 邮件（A股结果 / 休市通知）。

> 两个 workflow 共用同一套 SMTP Secrets，邮件主题自动区分美股 / A股。


## 5. 快速开始

### 5.1 上传文件清单
将以下文件上传到仓库（网页 **Add file → Upload files** 同名覆盖）：

| 文件 | 说明 |
|:---|:---|
| `sepa_screener_bot.py` | 美股主程序 |
| `sepa_a_share_bot.py` | A股主程序 |
| `config.yaml` / `config_a_share.yaml` | 双市场配置 |
| `requirements.txt` | 依赖（含 akshare / exchange_calendars） |
| `.github/workflows/run_daily.yml` | 美股 workflow |
| `.github/workflows/run_a_share.yml` | A股 workflow |
| `README.md` | 本文档 |

### 5.2 配置 GitHub Secrets
仓库 **Settings → Secrets and variables → Actions**，新增三个：

| Secret | 值 |
|:---|:---|
| `SMTP_USER` | QQ 邮箱地址 |
| `SMTP_PASS` | QQ 邮箱**授权码**（设置 → 账户 → 开启 SMTP 服务后生成，非登录密码） |
| `SMTP_TO` | 收件人邮箱（多个用英文逗号分隔） |

同时确认 `Settings → Actions → General` 的 **Workflow permissions** 为 **Read and write permissions**。

### 5.3 手动触发测试
1. 进入仓库 **Actions** 页。
2. 左侧选择 **SEPA A股每日筛选** 或 **SEPA股票筛选策略每日运行**。
3. 点击 **Run workflow** → **Run workflow**。
4. 等待运行完成，检查日志和邮件推送结果。

### 5.4 关键调试说明
* 首次测试建议用 `workflow_dispatch` 手动触发。
* `finvizfinance` 连接超时是网络问题，重试即可。
* 初筛数量异常时查看日志排查打点（环节①②③…），美股可本地跑 `python diagnose_screener.py` 复检。
* A股并发下载若触发东财限流（大量重试日志），将 `download_a_share_batch(max_workers=8)` 调低至 5。
* 美股/A股休市日不会运行筛选，会收到"今日休市"通知邮件。
* SMTP 发出的邮件在收件箱/垃圾箱（"已发送"仅存网页端/客户端发信）。

## 6. 免责声明 (Disclaimer)
* 本项目仅作为个人量化研究工具，不构成任何投资建议。
* 金融投资有风险，脚本筛选结果仅供技术参考，请务必独立决策。

---
**Last Updated**: 2026-09-01

