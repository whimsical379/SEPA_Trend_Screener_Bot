# SEPA Trend Screener Bot (v1.3)

## 1. 项目简介
本项目是一个基于 Python 开发的自动化美股筛选系统，核心逻辑严格遵循传奇交易大师 **Mark Minervini** 的 **SEPA (Specific Entry Point Analysis)** 趋势模板。

### v1.3 更新内容 (2026-08-26)

**Bug 修复：**
* **修复初筛 Ticker 解析错误（候选标的骤减的根因）**：finviz.com 2026 年改版后，ticker 单元格内新增了 logo 首字母占位 `<span>`，`finvizfinance` 库解析时用 `get_text()` 拼接导致 Ticker 变成重复首字母（如 `AAOI` → `AAAOI`，对应上游 issue #158），这些非法代码在 yfinance 查询全部失败。已加入兼容补丁，从链接 `href="stock?t=XXX"` 提取真实代码。
* **修复市值二次校验被 yfinance 限流静默吞掉**：原逻辑逐只调用 `yf.Ticker().fast_info`，Yahoo 对脚本/IP 限流（`YFRateLimitError: Too Many Requests`）时被 `except: continue` 静默丢弃，导致初筛结果骤减甚至归零。

**功能优化：**
* **市值过滤改用 finviz 自带 `Market Cap` 列**：不再逐只请求 yfinance，既规避限流又大幅提速（初筛从逐只网络请求变为单次 DataFrame 过滤）。
* **初筛流程增加三层排查打点**：`环节① finviz 原始返回 → 环节② 行业排除 → 环节③ 市值过滤`，每步打印数量，数量异常时一眼定位缩水环节。
* **新增 `diagnose_screener.py` 独立诊断脚本**：一键复检 finvizfinance 版本、过滤选项、筛选返回数量、yfinance 可用性，方便后续排查。
* **`requirements.txt` 锁定依赖版本**：固定为本地实测验证的组合（`finvizfinance==1.3.0` 等），避免 GitHub Actions 每次安装最新版引入行为漂移。


### v1.2 更新内容 (2026-07-08)

**Bug 修复：**
* 修复 `DAILY_BIAS_LIMIT`、`HOURLY_RSI_LOW`、`HOURLY_RSI_HIGH` 等未定义变量导致的运行时 `NameError`。
* 修复 `config.yaml` 中 `timing` 段重复导致的参数混乱。
* 将裸 `except:` 替换为 `except Exception:`，避免吞掉系统级异常。

**功能优化：**
* **移除小时线择时**：小时线功能因变量缺失完全不可用，已清理相关代码和输出引用。筛选流程精简为"周线闸门 → 日线核心"两层关键过滤。
* **周线趋势判断改进**：将 `is_monotonic_increasing`（严格单调递增，假负率极高）改为线性回归斜率检测（`np.polyfit`）。只需 MA20/MA50 近 12 周整体趋势向上即可通过，大幅降低误杀率。
* **简化日线 RS 条件**：移除 RS 20日 和 RS 250日 的双重相对强度校验，避免误杀处于反转初期的强势候选标的。

**新增功能：**
* **"差一步"标的追踪**：筛选过程中，周线或日线仅差 1 个条件未满足的标的会被单独列出，附具体失败关卡和原因（如"RSI 72.3 ≥ 70"、"乖离率 1.52 > 1.50"），方便交易者第二天重点关注。
* 筛选函数改为逐条件返回失败明细，每条失败原因含实时数值，便于复盘和人工复核。

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
| 成交量放量 | 当日成交量 ≥ MA20 均量 × 倍数（默认 1.0） |
| 流动性 | 20 日最小成交量 ≥ 50 万 |
| 乖离率风控 | 价格 / MA200 ≤ 1.5（可配置） |
| RSI 风控 | RSI14 < 70（可配置） |

## 3. 系统架构与参数配置
系统采用 **"逻辑-配置分离"** 设计，所有策略参数均在 `config.yaml` 中定义。

| 模块 | 核心参数 (YAML Key) | 默认值 | 说明 |
|:---|:---|:---|:---|
| 初筛 | `market_cap_min` / `market_cap_max` | 5000 / 200000 (M) | 市值范围（百万美元） |
| 初筛 | `exclude_industries` | Banks, Insurance, ... | 排除低效率行业 |
| 周线 | `weekly_bias_limit` | 1.40 | 周线乖离率上限 |
| 日线 | `daily_bias_limit` | 1.50 | 日线乖离率上限 |
| 日线 | `daily_rsi_limit` | 70 | 日线 RSI 上限 |
| 交易 | `stop_loss_pct` | 0.07 | 止损位（7%） |
| 交易 | `max_positions` | 3 | 每日最大推荐数 |
| 基准 | `benchmark` | SPY | 参考指数 |
| 基准 | `vol_mult` | 1.0 | 成交量放大倍数 |

## 4. 自动化流程 (CI/CD Workflow)
本项目利用 GitHub Actions 实现全自动化运行闭环：
1. **定时触发**：每日北京时间 06:00 自动启动。
2. **环境构建**：基于 `requirements.txt` 自动安装锁定版本的依赖。
3. **策略执行**：加载 `config.yaml` 参数，执行两周期扫描 + "差一步"追踪。
4. **数据回写**：筛选结果自动追加至 `sepa_history_signals.csv` 并自动提交（Commit）回仓库。
5. **即时推送**：通过 Server 酱发送微信通知（含完整入选 + 差一步标的）。

## 5. 快速开始

### 5.1 GitHub 上传与测试流程

**步骤 1：准备 GitHub 仓库**
```bash
# 如果你还没有 fork 原仓库，先去 https://github.com/whimsical379/SEPA_Trend_Screener_Bot 点击 Fork
# 然后 clone 你的 fork 到本地
git clone https://github.com/<你的用户名>/SEPA_Trend_Screener_Bot.git
cd SEPA_Trend_Screener_Bot
```

**步骤 2：替换文件**
将修改后的文件覆盖到仓库目录：
- `sepa_screener_bot.py`
- `diagnose_screener.py`（v1.3 新增）
- `requirements.txt`（v1.3 已锁定版本）
- `config.yaml`
- `README.md`

**步骤 3：提交并推送**
```bash
git add sepa_screener_bot.py diagnose_screener.py requirements.txt config.yaml README.md
git commit -m "v1.3: 修复finviz改版导致的ticker解析bug, 市值过滤改用finviz列规避yfinance限流"
git push origin main
```

**步骤 4：配置 GitHub Actions（首次使用需创建）**

在仓库中创建 `.github/workflows/sepa_daily.yml`：
```yaml
name: SEPA Daily Screener
on:
  schedule:
    - cron: '0 22 * * *'   # UTC 22:00 = 北京时间次日 06:00
  workflow_dispatch:         # 允许手动触发测试

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: python sepa_screener_bot.py
        env:
          SERVER_CHAN_SENDKEY: ${{ secrets.SERVER_CHAN_SENDKEY }}
      - uses: stefanzweifel/git-auto-commit-action@v5
        with:
          commit_message: "auto: 更新 SEPA 筛选结果 [skip ci]"
          file_pattern: 'sepa_history_signals.csv'
```

**步骤 5：配置 GitHub Secrets**
1. 进入仓库 **Settings → Secrets and variables → Actions**
2. 点击 **New repository secret**
3. Name: `SERVER_CHAN_SENDKEY`，Value: 你的 Server 酱 SendKey
4. 进入 **Settings → Actions → General**，将 **Workflow permissions** 改为 **Read and write permissions**

**步骤 6：手动触发测试**
1. 进入仓库 **Actions** 标签页
2. 左侧选择 **SEPA Daily Screener**
3. 点击 **Run workflow** → **Run workflow**
4. 等待运行完成，检查日志和微信推送结果

### 5.2 关键调试说明
* 首次测试建议用 `workflow_dispatch` 手动触发，不要等定时任务。
* 如果出现 `finvizfinance` 连接超时，是网络问题，重试即可。
* 推送内容过长被微信截断是正常的（Server 酱单条限制约 5KB），near-miss 已限制展示 15 只。
* 初筛数量异常骤减时，查看日志中的三层排查打点（环节①/②/③），或本地运行 `python diagnose_screener.py` 复检。
* `requirements.txt` 已在 v1.3 锁定依赖版本；上游 `finvizfinance` 修复 issue #158 后建议先本地验证再升级。

## 6. 免责声明 (Disclaimer)
* 本项目仅作为个人量化研究工具，不构成任何投资建议。
* 金融投资有风险，脚本筛选结果仅供技术参考，请务必独立决策。

---
**Last Updated**: 2026-08-26
