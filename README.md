# SEPA Trend Screener Bot (v1.6)

## 1. 项目简介
本项目是一个基于 Python 开发的**双市场自动化股票筛选系统**，核心逻辑严格遵循传奇交易大师 **Mark Minervini** 的 **SEPA (Specific Entry Point Analysis)** 趋势模板，通过"周线闸门 + 日线核心"两层过滤，自动筛选处于强势增长阶段的标的。

| 市场 | 脚本 | 数据源 | 每日运行（北京时间） |
|:---|:---|:---|:---|
| 美股 | `sepa_screener_bot.py` | finviz + yfinance | 06:00（美股盘后） |
| A股 | `sepa_a_share_bot.py` | akshare（东方财富） | 16:30（A股盘后） |

---

## 2. v1.6 更新内容 (2026-11-14)

> **背景**：2026-09-30 起，美股 Bot 连续 6 周"假成功"——Actions 每天显示绿色 ✅，实际每次运行 5 秒内空转失败，`sepa_history_signals.csv` 自动提交自 2026-09-29 中断。本次修复恢复初筛与数据下载链路。

### 🐛 修复① finviz 新 WAF 拦截（初筛 403 的根因）
* finviz.com 于 2026 年 9 月底升级 WAF 规则，对异常 User-Agent 直接返回 **403 Forbidden**。
* `finvizfinance 1.3.0` 内置 UA 字符串因 Python 换行续行符夹带 **20 个空格**（畸形 UA），实测被 403 拦截；干净的现代浏览器 UA 可正常访问（200）。
* **修复**：替换 `finvizfinance.util.headers` 为现代浏览器请求头（Chrome UA + Accept + Accept-Language）。

### 🐛 修复② finviz 2026 页面改版适配（SSR island 架构）
* 新版 `/screener` 页面表格由前端 JavaScript 渲染，HTML 中不再存在 `<table class="screener_table">` 与 `id="pageSelect"`，库的旧 DOM 解析器拿不到任何行（打印 `No ticker found`）。
* 但完整筛选数据以**纯 JSON 岛**嵌在页面 `<script>` 中（`"data":{"rows":[...],"totalCount":N,"pageSize":20}`）。
* **修复**：重写 `Base.screener_view`，改为逐页解析 JSON 数据岛（`r=` 为 1-based 行偏移），列名与旧版 finvizfinance 完全对齐，`Market Cap` 单位保持美元，下游市值过滤零改动。
* 原 v1.3 的 Ticker 重复首字母补丁随旧 DOM 一起失效，已移除（新解析器直接取 JSON 中的干净 ticker）。

### 🐛 修复③ yfinance 1.6.0 兼容（batch_size 参数已删除）
* v1.4 引入的 `download_batch()` 向 `yf.download()` 传入 `batch_size` 参数，该参数在 `yfinance 1.6.0`（2026-08-26 锁版）中已被移除，导致批量下载每次都抛 `TypeError`，静默回退为 ~136 次逐只请求——既慢又极易触发 Yahoo 限流。
* **修复**：移除该参数，改为自行按 50 只/批分批传入。每轮运行请求数 **~136 次 → 4 次**，大幅降低被 Yahoo 限流的概率。

### 📖 运维经验：如何识别"假成功"
本项目的 workflow 中，脚本异常被 `try/except` 捕获后正常退出（exit code 0），**Actions 永远显示绿色 ✅**。判断运行是否真实，看两点：

| 判断项 | 真实运行 | 假成功（空转） |
|:---|:---|:---|
| 运行时长 | 2~5 分钟以上 | 1 分钟以内 |
| CSV 自动提交 | 仓库有当日 `🤖 自动更新SEPA历史信号` | 提交日期停滞 |

<details>
<summary><b>历史版本更新记录（v1.2 ~ v1.5）</b>（点击展开）</summary>

### v1.5 (2026-09-01)
* **新增 A 股筛选版本**：`sepa_a_share_bot.py`（akshare 数据源）+ `config_a_share.yaml` + 独立 workflow（工作日 16:30）。A 股适配：排除 ST、前复权、XSHG 交易日历、休市通知。
* **移除行业筛选**（美股 + A 股），候选池回归全行业。
* **修复 A 股基准指数接口**：沪深 300 改用 `stock_zh_index_daily_em("sh000300")`（新浪兜底）。
* **A 股请求量优化**：每只股票只请求 1 次 2 年日线，周线由日线重采样；8 线程并发 + 3 次重试。请求量 ~3000 → ~2500，运行时长 40-75 分钟 → 5-12 分钟。

### v1.4 (2026-08-30)
* **yfinance 批量下载** `download_batch()`（v1.6 中修复其参数兼容问题）。
* **推送升级**：Server 酱 → SMTP 邮件推送（QQ 邮箱授权码），`md_to_plain()` 转纯文本，Secrets 变更为 `SMTP_USER` / `SMTP_PASS` / `SMTP_TO`。
* **非交易日跳过**：`exchange_calendars`（XNYS / XSHG）判断，休市自动通知。

### v1.3 (2026-08-26)
* 修复初筛 Ticker 解析错误（finviz 改版导致 `AAOI` → `AAAOI`，v1.6 已随新解析器移除）。
* 修复市值二次校验被 yfinance 限流静默吞掉：改用 finviz 自带 `Market Cap` 列过滤。
* 新增三层排查打点与 `diagnose_screener.py`；`requirements.txt` 锁定版本。

### v1.2 (2026-07-08)
* 修复未定义变量 `NameError`、config 重复段、裸 `except`。
* 移除小时线择时，精简为"周线闸门 → 日线核心"；周线趋势改线性回归斜率检测。
* 新增**"差一步"标的追踪**（near-miss）。
</details>

---

## 3. 策略逻辑 (Strategy Logic)

### 3.1 趋势模板过滤 (Trend Template)
* **均线排列**：MA50 > MA150 > MA200。
* **形态验证**：MA200 持续向上、价格在合理乖离率范围内。

### 3.2 两周期校验系统

**周线闸门（Gate）— 4 个条件：**

| 条件 | 说明 |
|:---|:---|
| MA20 趋势 | 近 12 周线性回归斜率 > 0 |
| MA50 趋势 | 近 12 周线性回归斜率 > 0 |
| 价格排列 | Close > MA20 > MA50 |
| 乖离率 | Close / MA50 ≤ 1.4（可配置） |

**日线核心（Core）— 6 个条件：**

| 条件 | 说明 |
|:---|:---|
| 均线多头排列 | MA50 > MA150 > MA200 |
| 突破确认 | 收盘价 > 前 10 日最高点 |
| 成交量放量 | 当日成交量 ≥ MA20 均量 × 倍数 |
| 流动性 | 20 日最小成交量 ≥ 50 万 |
| 乖离率风控 | 价格 / MA200 ≤ 1.5（可配置） |
| RSI 风控 | RSI14 < 70（可配置） |

### 3.3 差一步追踪（near-miss）
* 周线或日线**只差 1 个条件**未满足的标的单独列出，附失败关卡和实时数值。
* 按"日线优先"排序，邮件推送最多展示 15 只。

### 3.4 数据获取方式
* **美股**：finviz JSON 数据岛初筛（现代浏览器 UA）→ yfinance 批量下载（50 只/批 × 2 周期）。
* **A股**：akshare 实时行情初筛 → 8 线程并发下载 2 年日线，周线由日线重采样推导，基准指数用沪深 300 指数接口。

---

## 4. 系统架构与参数配置
系统采用 **"逻辑-配置分离"** 设计，策略参数分别在 `config.yaml`（美股）和 `config_a_share.yaml`（A股）中定义。

### 4.1 美股参数（config.yaml）

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

### 4.2 A股参数（config_a_share.yaml）

| 模块 | 核心参数 (YAML Key) | 默认值 | 说明 |
|:---|:---|:---|:---|
| 初筛 | `market_cap_min` / `market_cap_max` | 50 / 10000 | 市值范围（亿元） |
| 初筛 | `price_min` | 3 | 最低股价（元） |
| 初筛 | `exclude_st` | true | 排除 ST/*ST 股 |
| 周线 | `weekly_bias_limit` | 1.35 | 周线乖离率上限 |
| 日线 | `daily_bias_limit` | 1.45 | 日线乖离率上限 |
| 日线 | `daily_rsi_limit` | 72 | 日线 RSI 上限 |
| 交易 | `stop_loss_pct` | 0.05 | 止损位（5%，T+1 收紧） |
| 交易 | `break_even_pct` | 0.10 | 保本位（10%） |
| 交易 | `max_positions` | 3 | 每日最大推荐数 |
| 基准 | `benchmark` | 000300 | 沪深 300 指数 |
| 基准 | `vol_mult` | 1.3 | 成交量放大倍数 |

---

## 5. 自动化流程 (CI/CD Workflow)

### 5.1 美股 workflow（`.github/workflows/run_daily.yml`）
1. **定时触发**：UTC 22:00（北京时间次日 06:00，美股盘后）。
2. **环境构建**：安装 `requirements.txt`（锁定版本）。
3. **策略执行**：`python sepa_screener_bot.py`，注入 SMTP 三环境变量。
4. **数据回写**：`sepa_history_signals.csv` 自动 commit 回仓库。
5. **即时推送**：SMTP 邮件（入选 + 差一步 + 休市通知）。

### 5.2 A股 workflow（`.github/workflows/run_a_share.yml`）
1. **定时触发**：UTC 08:30（北京时间 16:30，工作日 1-5）。
2. **策略执行**：`python sepa_a_share_bot.py`，同样注入 SMTP 变量。
3. **数据回写**：`sepa_a_share_signals.csv` 自动 commit。
4. **推送**：SMTP 邮件（A股结果 / 休市通知）。

> ⚠️ **A股 workflow 当前处于禁用状态**（Actions 页面手动禁用，不受 finviz 故障影响——A股走 akshare）。如需启用：Actions 页左侧选中该 workflow → 右侧 `Enable workflow`。两个 workflow 共用同一套 SMTP Secrets。

---

## 6. 快速开始

### 6.1 上传文件清单
将以下文件上传到仓库（网页 **Add file → Upload files** 同名覆盖）：

| 文件 | 说明 |
|:---|:---|
| `sepa_screener_bot.py` | 美股主程序（含 finviz 兼容补丁） |
| `sepa_a_share_bot.py` | A股主程序 |
| `config.yaml` / `config_a_share.yaml` | 双市场配置 |
| `requirements.txt` | 依赖（锁定版本） |
| `.github/workflows/run_daily.yml` | 美股 workflow |
| `.github/workflows/run_a_share.yml` | A股 workflow |
| `README.md` | 本文档 |

### 6.2 配置 GitHub Secrets
仓库 **Settings → Secrets and variables → Actions**，新增三个：

| Secret | 值 |
|:---|:---|
| `SMTP_USER` | QQ 邮箱地址 |
| `SMTP_PASS` | QQ 邮箱**授权码**（设置 → 账户 → 开启 SMTP 服务后生成，非登录密码） |
| `SMTP_TO` | 收件人邮箱（多个用英文逗号分隔） |

同时确认 `Settings → Actions → General` 的 **Workflow permissions** 为 **Read and write permissions**（否则 CSV 自动提交会失败）。

### 6.3 手动触发测试
1. 进入仓库 **Actions** 页。
2. 左侧选择对应 workflow → **Run workflow** → **Run workflow**。
3. **看运行时长判断真伪**：2~5 分钟以上为真实运行；1 分钟以内大概率空转（见 6.4）。
4. 运行完成后检查邮件推送与仓库 CSV 提交。

### 6.4 关键调试与故障排查

**判断"假成功"（v1.6 之前中断 6 周未察觉的教训）：**
* 运行时长 < 1 分钟 = 空转。真实筛选含 finviz 翻页 + 批量下载，至少 2 分钟。
* 日志中出现 `[info] fetching 20/9X rows` 与 `🔍[排查] 环节① finviz 原始返回: 9X 只` = 初筛正常。
* 日志只有 `❌ finviz初筛接口调用失败: ... 403` = UA 补丁失效或被新拦截，需更新 `sepa_screener_bot.py` 中的请求头补丁。

**常见问题：**
* `YFRateLimitError: Too Many Requests`：Yahoo 限流，v1.6 已将请求量降到 4 次/轮；仍被限流时等待 10~30 分钟重试（封禁约 1 小时自动解除）。
* `finvizfinance` 连接超时：网络问题，重试即可。
* 初筛数量异常时查看日志排查打点（环节①②③…），美股可本地跑 `python diagnose_screener.py` 复检（会自动加载与生产一致的补丁）。
* A 股并发下载若触发东财限流，将 `download_a_share_batch(max_workers=8)` 调低至 5。
* 美股/A股休市日不会运行筛选，会收到"今日休市"通知邮件。
* SMTP 发出的邮件在收件箱/垃圾箱。

**版本回滚：**
* 文件页 → History → 打开任意历史版本 → **Raw** 复制 → 铅笔编辑粘贴回去提交，即可回到旧版本。

---

## 7. 免责声明 (Disclaimer)
* 本项目仅作为个人量化研究工具，不构成任何投资建议。
* 金融投资有风险，脚本筛选结果仅供技术参考，请务必独立决策。

---
**Last Updated**: 2026-11-14
