# SEPA Trend Template Screener Bot (v1.1)

## 1. 项目简介
本项目是一个基于 Python 开发的自动化美股筛选系统，核心逻辑严格遵循传奇交易大师 **Mark Minervini** 的 **SEPA (Specific Entry Point Analysis)** 趋势模板。

**v1.1 更新亮点：**
* **配置分离**：引入 `config.yaml`，实现策略参数与核心代码的完全解耦。
* **数据持久化**：自动生成并维护 `sepa_history_signals.csv`，记录每日筛选结果以供复盘。
* **依赖标准化**：通过 `requirements.txt` 统一环境管理。

## 2. 策略逻辑 (Strategy Logic)
系统通过三个维度的过滤确保标的处于强势增长阶段：

### 2.1 趋势模板过滤 (Trend Template)
* **均线排列**：价格 > MA50 > MA150 > MA200。
* **相对强度 (RS)**：个股表现必须在指定周期内跑赢大盘（默认 SPY）。
* **形态验证**：要求 MA200 持续向上至少 1 个月，且价格处于 52 周高点的 25% 范围内。

### 2.2 三周期校验系统
* **周线级别 (Gate)**：验证长期均线一致性，严格控制乖离率（Bias）。
* **日线级别 (Core)**：包含成交量放量（Vol Mult）校验、RSI 风控及高点突破确认。
* **小时线级别 (Timing)**：利用 RSI 超买/低吸区间进行辅助择时。

## 3. 系统架构与参数配置
系统采用 **“逻辑-配置分离”** 设计，所有策略参数均在 `config.yaml` 中定义，无需修改 `.py` 源代码。

| 模块 | 核心参数 (Yaml Key) | 说明 |
| :--- | :--- | :--- |
| **初筛** | `market_cap_min`, `exclude_industries` | 过滤市值门槛与低效率行业 |
| **阈值** | `weekly_bias_limit`, `daily_rsi_limit` | 控制追高风险与超买状态 |
| **交易** | `stop_loss_pct`, `max_positions` | 定义 7% 止损位及每日推荐上限 |
| **基准** | `benchmark`, `vol_mult` | 自定义参考指数与成交量放大倍数 |

## 4. 自动化流程 (CI/CD Workflow)
本项目利用 GitHub Actions 实现全自动化运行闭环：
1. **定时触发**：每日北京时间 06:00 自动启动。
2. **环境构建**：基于 `requirements.txt` 自动安装最新依赖。
3. **策略执行**：加载 `config.yaml` 参数，执行三周期扫描。
4. **数据回写**：**[New]** 筛选结果自动追加至 `sepa_history_signals.csv` 并自动提交（Commit）回仓库。
5. **即时推送**：通过 Server 酱发送微信通知。

## 5. 快速开始
1. **Fork 本仓库**。
2. **环境准备**：本地运行 `pip install -r requirements.txt`。
3. **权限配置**：
   - 在 GitHub **Settings > Actions > General** 中将 **Workflow permissions** 改为 **Read and write permissions**（用于自动保存 CSV）。
   - 在 **Secrets and variables** 中添加 `SERVER_CHAN_SENDKEY`。
4. **自定义策略**：修改 `config.yaml` 中的数值以符合您的交易偏好。

## 6. 免责声明 (Disclaimer)
* 本项目仅作为个人量化研究工具，不构成任何投资建议。
* 金融投资有风险，脚本筛选结果仅供技术参考，请务必独立决策。

---
**Last Updated**: 2026-05-08
