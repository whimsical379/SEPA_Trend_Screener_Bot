# -*- coding: utf-8 -*-
"""股票筛选Bot
SEPA 趋势策略自动化筛选（周线闸门 + 日线核心 + 差一步标的追踪）
"""
import os
import smtplib
import yaml
import pandas as pd
import numpy as np
import yfinance as yf
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from finvizfinance.screener.overview import Overview
import warnings
warnings.filterwarnings('ignore')

# ===================== finvizfinance 兼容补丁 =====================
# 修复 finviz.com 2026 年改版引发的 Ticker 重复首字母 bug（finvizfinance issue #158）
# 新版页面在 ticker 单元格内新增了 logo 首字母 <span>（图片加载失败时的字母回退），
# 库用 col.get_text() 会把 "A" + "AAOI" 拼成 "AAAOI"，导致后续 yfinance 查询全部失败。
# 补丁：Ticker 列优先从 <a class="tab-link"> 的 href="stock?t=XXX" 提取真实代码。
def _install_finviz_ticker_patch():
    import re as _re
    from finvizfinance.screener import base as _base
    from finvizfinance.util import number_covert as _nc

    def _patched_get_table(self, rows, df, num_col_index, table_header, limit=-1):
        rows = rows[1:]
        if limit != -1:
            rows = rows[0:limit]
        frame = []
        for row in rows:
            cols = row.findAll("td")[1:]
            info_dict = {}
            for i, col in enumerate(cols):
                header = table_header[i] if i < len(table_header) else f"col{i}"
                if header == "Ticker":
                    # 修复: 从 href="stock?t=XXX" 提取真实 ticker
                    a = col.find("a", class_="tab-link") or col.find("a")
                    if a and a.get("href"):
                        m = _re.search(r"[?&]t=([^&]+)", a["href"])
                        if m:
                            info_dict[header] = m.group(1)
                            continue
                    info_dict[header] = col.get_text(strip=True)
                elif i not in num_col_index:
                    info_dict[header] = col.text
                else:
                    info_dict[header] = _nc(col.text)
            frame.append(info_dict)
        if len(df) == 0:
            return pd.DataFrame(frame)
        return pd.concat([df, pd.DataFrame(frame)], ignore_index=True)

    _base.Base._get_table = _patched_get_table


_install_finviz_ticker_patch()


# ===================== 动态配置加载 =====================
def load_config(config_path="config.yaml"):
    if not os.path.exists(config_path):
        return None
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

cfg = load_config()

# --- 默认值，防止NameError ---
BENCHMARK = "SPY"
VOL_MULT = 1.0
MARKET_CAP_MIN, MARKET_CAP_MAX = 5000, 200000
MAX_POSITIONS = 3
STOP_LOSS_PCT, BREAK_EVEN_PCT = 0.07, 0.14

if cfg:
    try:
        MARKET_CAP_MIN = cfg['filters']['market_cap_min']
        MARKET_CAP_MAX = cfg['filters']['market_cap_max']
        EXCLUDE_INDUSTRIES = cfg['filters']['exclude_industries']

        WEEKLY_BIAS_LIMIT = cfg['thresholds']['weekly_bias_limit']
        DAILY_BIAS_LIMIT = cfg['thresholds']['daily_bias_limit']
        DAILY_RSI_LIMIT = cfg['thresholds']['daily_rsi_limit']

        MAX_POSITIONS = cfg['timing']['max_positions']
        STOP_LOSS_PCT = cfg['timing']['stop_loss_pct']
        BREAK_EVEN_PCT = cfg['timing']['break_even_pct']

        BENCHMARK = cfg['timing'].get('benchmark', "SPY")
        VOL_MULT = cfg['timing'].get('vol_mult', 1.0)

        print("✅ 策略参数已从 config.yaml 成功加载")
    except KeyError as e:
        print(f"⚠️ 配置文件格式错误，缺少键值: {e}。将使用程序内置默认值。")
else:
    print("⚠️ 未找到配置文件，正在使用内置默认参数运行...")

# ===================== SMTP 邮件推送配置 =====================
SMTP_USER = os.getenv("SMTP_USER", "")   # QQ邮箱地址
SMTP_PASS = os.getenv("SMTP_PASS", "")   # QQ邮箱授权码（非登录密码）
SMTP_TO   = os.getenv("SMTP_TO", "")     # 收件人邮箱，多个用逗号分隔

# ===================== 邮件推送函数 =====================
def md_to_plain(md_text):
    """简单 Markdown → 纯文本（适配邮件正文）"""
    lines = []
    for raw in md_text.splitlines():
        s = raw.strip()
        if not s:
            lines.append("")
        elif s.startswith("####"):
            lines.append("\n[ " + s.lstrip("#").strip() + " ]")
            lines.append("-" * 40)
        elif s.startswith("###"):
            lines.append("\n" + s.lstrip("#").strip().upper())
            lines.append("=" * 40)
        elif s.startswith("##"):
            lines.append("\n" + s.lstrip("#").strip())
            lines.append("=" * 40)
        elif s.startswith("|"):
            cells = [c.strip() for c in s.split("|") if c.strip()]
            if all(set(c) <= set(":-") for c in cells):  # 表格分隔行
                continue
            lines.append("  " + " | ".join(cells))
        elif s.startswith("- ") or s.startswith("* "):
            lines.append("  • " + s[2:])
        else:
            lines.append(s)
    return "\n".join(lines)


def send_email_msg(title, content):
    """SMTP 邮件推送（QQ邮箱，支持多收件人）"""
    to_list = [x.strip() for x in SMTP_TO.split(",") if x.strip()]
    if not (SMTP_USER and SMTP_PASS and to_list):
        print("⚠️ 未配置SMTP参数(SMTP_USER/SMTP_PASS/SMTP_TO)，跳过邮件推送")
        return False
    msg = MIMEText(content, "plain", "utf-8")
    # 注意: From/To 必须保持明文邮箱地址（勿用 Header 编码，否则 QQ 邮箱 550 拒收）
    msg["From"] = formataddr(("SEPA Bot", SMTP_USER))
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = Header(title, "utf-8")
    try:
        server = smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30)
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, to_list, msg.as_string())
        server.quit()
        print("✅ 邮件推送发送成功 →", to_list)
        return True
    except Exception as e:
        print(f"❌ 邮件推送出错: {type(e).__name__}: {e}")
        return False

# ===================== 交易日判断 =====================
def is_us_trading_day():
    """判断美东当前日期是否为美股交易日（XNYS 纽交所日历）"""
    try:
        import exchange_calendars as xcals
        xnys = xcals.get_calendar("XNYS")
        return xnys.is_session(datetime.now(ZoneInfo("America/New_York")).date())
    except Exception as e:
        print(f"⚠️ 交易日历加载失败({type(e).__name__}: {e})，默认视为交易日")
        return True

# ===================== 初筛函数 =====================
def get_finviz_screened_tickers():
    """finviz自动初筛 + 行业排除 + 市值二次校验"""
    try:
        f_screener = Overview()
        filters_dict = {
            'Market Cap.': '+Large (over $10bln)',
            'Price': 'Over $5',
            'Average Volume': 'Over 2M',
            'Current Volume': 'Over 2M',
            'IPO Date': 'More than a year ago',
            '200-Day Simple Moving Average': 'Price above SMA200'
        }
        f_screener.set_filter(filters_dict=filters_dict)
        df_res = f_screener.screener_view()

        # [排查打点] 环节①：finviz 原始返回
        if df_res is None or df_res.empty:
            print("❌[排查] 环节① finviz 返回空/None: df_res =", df_res)
            return []
        print(f"🔍[排查] 环节① finviz 原始返回: {len(df_res)} 只 | 列名: {list(df_res.columns)}")

        if 'Industry' in df_res.columns:
            exclude_pattern = '|'.join(EXCLUDE_INDUSTRIES)
            df_res = df_res[~df_res['Industry'].str.contains(exclude_pattern, case=False, na=False)]

        # [排查打点] 环节②：行业排除后
        print(f"🔍[排查] 环节② 行业排除后: {len(df_res)} 只")

        initial_tickers = df_res['Ticker'].tolist()

        # ===== 市值二次过滤 =====
        # 修复: 原逻辑用 yfinance 逐只 fast_info 校验，但 Yahoo 对脚本/IP 限流严重
        # (YFRateLimitError: Too Many Requests)，导致候选标的被 except 静默丢弃而骤减。
        # finviz screener 本身返回 Market Cap 列（float64，美元），直接用该列过滤即可，
        # 既规避限流又大幅提速。
        final_tickers = []
        if 'Market Cap' in df_res.columns:
            mc_m = df_res['Market Cap'] / 1e6  # 转成百万美元
            before = len(df_res)
            df_res = df_res[(mc_m >= MARKET_CAP_MIN) & (mc_m <= MARKET_CAP_MAX)]
            final_tickers = df_res['Ticker'].tolist()
            print(f"🔍[排查] 环节③ 市值过滤(finviz Market Cap列): {before} → {len(final_tickers)} 只 "
                  f"(范围 {MARKET_CAP_MIN}-{MARKET_CAP_MAX}M)")
        else:
            # 兜底: finviz 结果无 Market Cap 列时回退到 yfinance 校验（注意可能被限流）
            print(f"⚠️ finviz 结果缺少 Market Cap 列，回退 yfinance 校验 (目标: {MARKET_CAP_MIN}-{MARKET_CAP_MAX}M)...")
            skipped = 0
            for ticker in initial_tickers:
                try:
                    info = yf.Ticker(ticker).fast_info
                    mkt_cap_m = info['marketCap'] / 1e6
                    if MARKET_CAP_MIN <= mkt_cap_m <= MARKET_CAP_MAX:
                        final_tickers.append(ticker)
                except Exception as e:
                    skipped += 1
                    print(f"⚠️[排查] {ticker} 市值校验失败: {type(e).__name__}: {e}")
            print(f"🔍[排查] 环节③ 市值二次校验后: {len(final_tickers)} 只 (失败/跳过 {skipped} 只)")
        return final_tickers
    except Exception as e:
        print(f"❌ finviz初筛接口调用失败: {type(e).__name__}: {e}")
        return []

# ===================== 数据获取 =====================
def get_data(symbol: str, interval='1d'):
    """获取单只K线数据（用于基准指数与批量下载失败回退）"""
    period = "2y" if interval == '1wk' else "1y"
    df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=True)
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def download_batch(tickers, interval='1d', batch_size=50):
    """批量下载K线，返回 {ticker: DataFrame}

    按 batch_size 分批请求（yfinance 内部再细分为小批），请求数从 ~N 次降到 ~N/batch_size 次，
    显著降低被 Yahoo 限流的概率。批请求失败时自动回退为单只下载，保证数据完整性。
    """
    if not tickers:
        return {}
    period = "2y" if interval == '1wk' else "1y"
    result = {}
    n_batch = (len(tickers) + batch_size - 1) // batch_size
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        idx = i // batch_size + 1
        try:
            df = yf.download(batch, period=period, interval=interval,
                             group_by='ticker', batch_size=batch_size,
                             progress=False, auto_adjust=True, threads=True)
            if df is None or df.empty:
                print(f"⚠️[批量下载] 第{idx}/{n_batch}批({len(batch)}只)返回为空，跳过")
                continue
            for tk in batch:
                try:
                    sub = df[tk].dropna()  # 去掉批量对齐产生的缺失行
                    if not sub.empty:
                        result[tk] = sub
                except Exception:
                    continue
        except Exception as e:
            print(f"⚠️[批量下载] 第{idx}/{n_batch}批失败({type(e).__name__}: {e})，回退单只下载")
            for tk in batch:
                sub = get_data(tk, interval=interval)
                if sub is not None and not sub.empty:
                    result[tk] = sub
    return result

# ===================== 周线闸门（返回逐条件失败明细） =====================
def check_weekly_gate(ticker, df_w):
    """
    周线SEPA闸门函数
    返回: (pass_bool, summary_msg, failed_conditions_list)
    failed_conditions_list 含具体失败原因及数值
    """
    failed = []

    if len(df_w) < 52:
        return False, "周线数据不足一年", ["周线数据<52根"]

    df_w = df_w.copy()
    df_w['MA20'] = df_w['Close'].rolling(20).mean()
    df_w['MA50'] = df_w['Close'].rolling(50).mean()
    curr = df_w.iloc[-1]
    prev_12_ma20 = df_w['MA20'].iloc[-12:]
    prev_12_ma50 = df_w['MA50'].iloc[-12:]

    # 条件1+2: MA20 / MA50 12周趋势（线性回归斜率 > 0）
    x = np.arange(12)
    slope_ma20 = np.polyfit(x, prev_12_ma20.values, 1)[0]
    slope_ma50 = np.polyfit(x, prev_12_ma50.values, 1)[0]
    if slope_ma20 <= 0:
        failed.append(f"MA20近12周趋势未向上(斜率{slope_ma20:.4f})")
    if slope_ma50 <= 0:
        failed.append(f"MA50近12周趋势未向上(斜率{slope_ma50:.4f})")
    # 条件3: 价格排列 Close > MA20 > MA50
    if not (curr['Close'] > curr['MA20'] > curr['MA50']):
        failed.append(f"价格未站稳均线(C:{curr['Close']:.2f} MA20:{curr['MA20']:.2f} MA50:{curr['MA50']:.2f})")
    # 条件4: 乖离率
    bias = curr['Close'] / curr['MA50']
    if bias > WEEKLY_BIAS_LIMIT:
        failed.append(f"周线超买(乖离率{bias:.2f}>{WEEKLY_BIAS_LIMIT})")

    if failed:
        return False, f"周线未通过({len(failed)}项不满足)", failed

    return True, "周线通过", []

# ===================== 日线核心（返回逐条件失败明细） =====================
def check_daily_core(ticker, df, spy_df):
    """
    日线SEPA核心选股函数
    返回: (pass_bool, summary_msg, last_row_or_None, failed_conditions_list)
    failed_conditions_list 含具体失败原因及当前数值
    """
    failed = []

    if len(df) < 250:
        return False, "日线历史数据不足", None, ["日线数据<250日"]

    df = df.copy()
    df['MA50'] = df['Close'].rolling(50).mean()
    df['MA150'] = df['Close'].rolling(150).mean()
    df['MA200'] = df['Close'].rolling(200).mean()
    df['VOL_MA20'] = df['Volume'].rolling(20).mean()
    df['MIN_VOL20'] = df['Volume'].rolling(20).min()
    df['HIGH10_PREV'] = df['High'].shift(1).rolling(10).max()

    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    df['RSI14'] = 100 - (100 / (1 + (gain / loss)))

    common = df.index.intersection(spy_df.index)
    if len(common) < 2:
        return False, "指数数据对齐失败", None, ["指数数据对齐失败(共同交易日<2)"]

    t = df.loc[common].iloc[-2]

    # 条件1: 均线多头排列
    if not (t['MA50'] > t['MA150'] > t['MA200']):
        failed.append(f"均线未多头排列(MA50:{t['MA50']:.2f} MA150:{t['MA150']:.2f} MA200:{t['MA200']:.2f})")

    # 条件2: 突破前10日高点
    if t['Close'] <= t['HIGH10_PREV']:
        failed.append(f"未突破前10日高点(收盘{t['Close']:.2f} vs 高点{t['HIGH10_PREV']:.2f})")

    # 条件5: 成交量放量
    if t['Volume'] < t['VOL_MA20'] * VOL_MULT:
        failed.append(f"成交量未放量(当前{t['Volume']:.0f} < 均量×{VOL_MULT}={t['VOL_MA20']*VOL_MULT:.0f})")

    # 条件6: 流动性
    if t['MIN_VOL20'] < 500000:
        failed.append(f"流动性不足(20日最低量{t['MIN_VOL20']:.0f}<50万)")

    # 条件7: 乖离率风控
    bias_daily = t['Close'] / t['MA200']
    if bias_daily > DAILY_BIAS_LIMIT:
        failed.append(f"日线乖离过高({bias_daily:.2f}>{DAILY_BIAS_LIMIT})")

    # 条件8: RSI 风控
    if t['RSI14'] >= DAILY_RSI_LIMIT:
        failed.append(f"日线RSI过高({t['RSI14']:.1f}>={DAILY_RSI_LIMIT})")

    if failed:
        return False, f"日线未通过({len(failed)}项不满足)", t, failed

    return True, "日线通过", t, []

# ===================== 主运行函数 =====================
def run_sepa_bot():
    tz_utc_8 = timezone(timedelta(hours=8))
    run_time = datetime.now(tz_utc_8).strftime('%Y-%m-%d %H:%M:%S')
    print(f"===== SEPA策略运行时间：{run_time} =====")

    # ===== 非交易日跳过 =====
    if not is_us_trading_day():
        msg = (f"## SEPA策略今日休市\n\n"
               f"- 运行时间：{run_time}（北京时间）\n"
               f"- 原因：美股今日非交易日\n"
               f"- 结果：今日不进行筛选，明日自动恢复正常运行。")
        print(msg)
        send_email_msg("📭 SEPA策略：今日美股休市", md_to_plain(msg))
        return

    push_content = f"## SEPA策略运行时间：{run_time}\n\n"
    tickers = get_finviz_screened_tickers()

    if not tickers:
        push_content += "❌ finviz初筛无符合条件的标的，请检查参数"
        print(push_content)
        send_email_msg("🔴 SEPA策略每日运行结果", md_to_plain(push_content))
        return

    push_content += f"✅ finviz初筛完成，共获取到 {len(tickers)} 只候选标的\n\n"
    buy_signals = []
    near_misses = []  # 差一个条件就能入选

    # ===== 批量下载（周线/日线各一次，替代逐只请求）=====
    print("📥 批量下载周线数据（2y）...")
    weekly_data = download_batch(tickers, interval='1wk')
    print(f"✅ 周线数据就绪：{len(weekly_data)}/{len(tickers)} 只")
    print("📥 批量下载日线数据（1y）...")
    daily_data = download_batch(tickers, interval='1d')
    print(f"✅ 日线数据就绪：{len(daily_data)}/{len(tickers)} 只")
    spy_df = get_data(BENCHMARK)  # 基准指数单独下载

    # 循环筛选
    for ticker in tickers:
        try:
            # 周线闸门（逐条件追踪）
            df_w = weekly_data.get(ticker)
            if df_w is None:
                print(f"[跳过] {ticker}: 周线数据缺失")
                continue
            pass_w, msg_w, failed_w = check_weekly_gate(ticker, df_w)
            if not pass_w:
                if len(failed_w) == 1:
                    near_misses.append({"ID": ticker, "Level": "周线", "Reason": failed_w[0]})
                continue

            # 日线核心（逐条件追踪）
            df_d = daily_data.get(ticker)
            if df_d is None:
                print(f"[跳过] {ticker}: 日线数据缺失")
                continue
            pass_d, msg_d, last_row, failed_d = check_daily_core(ticker, df_d, spy_df)
            if not pass_d:
                if len(failed_d) == 1:
                    near_misses.append({"ID": ticker, "Level": "日线", "Reason": failed_d[0]})
                continue

            # 完全通过
            p = last_row['Close']
            buy_signals.append({
                "ID": ticker,
                "Price": round(p, 2),
                "SL": round(p * (1 - STOP_LOSS_PCT), 2),
                "BE": round(p * (1 + BREAK_EVEN_PCT), 2),
                "Msg": f"{msg_w} | {msg_d}"
            })
        except Exception as e:
            print(f"[跳过] {ticker}: 数据处理出错 {e}")
            continue

    # ===================== 输出结果 =====================
    print("\n" + "=" * 15 + " 🔴 三周期SEPA明日入场推荐 " + "=" * 15)

    # --- 完整入选标的 ---
    if not buy_signals:
        push_content += "### 最终结果：暂无符合三周期SEPA标准的标的\n"
        print("→ 暂无符合标的")
    else:
        push_content += f"### 完整入选标的（共{len(buy_signals)}只，展示前{MAX_POSITIONS}只）：\n\n"
        for i in buy_signals[:MAX_POSITIONS]:
            print(f"• {i['ID']} | 触发价: {i['Price']} | 7%止损: {i['SL']} | 14%保本位: {i['BE']}")
            print(f"  └─ 状态: {i['Msg']}")
            push_content += (
                f"#### {i['ID']}\n"
                f"- 触发价：{i['Price']} 美元\n"
                f"- 7%止损位：{i['SL']} 美元\n"
                f"- 14%保本位：{i['BE']} 美元\n"
                f"- 周期验证：{i['Msg']}\n\n"
            )

    # --- 差一步标的 ---
    if near_misses:
        # 最多展示15只，避免微信推送超长
        show_count = min(len(near_misses), 15)
        push_content += f"### 差一步就入选（共{len(near_misses)}只，展示{show_count}只）：\n\n"
        print(f"\n--- 差一步就入选（共{len(near_misses)}只）---")

        # 按层级排序：日线优先（更接近入选）
        near_misses.sort(key=lambda x: (0 if x['Level'] == '日线' else 1, x['ID']))

        push_content += "| 代码 | 关卡 | 差的条件 |\n"
        push_content += "|:---|:---|:---|\n"
        for nm in near_misses[:show_count]:
            reason_short = nm['Reason'][:60] + ("..." if len(nm['Reason']) > 60 else "")
            print(f"  ⚡ {nm['ID']} | {nm['Level']} | {nm['Reason']}")
            push_content += f"| {nm['ID']} | {nm['Level']} | {reason_short} |\n"
        push_content += "\n"

    # --- CSV 持久化 ---
    if buy_signals:
        df_to_save = pd.DataFrame(buy_signals[:MAX_POSITIONS])
        df_to_save['Date'] = datetime.now(tz_utc_8).strftime('%Y-%m-%d')
        csv_file = 'sepa_history_signals.csv'
        header = not os.path.exists(csv_file)
        df_to_save.to_csv(csv_file, mode='a', index=False, header=header, encoding='utf-8-sig')
        print(f"💾 已将 {len(df_to_save)} 条信号保存至 {csv_file}")

    # 最终推送
    send_email_msg("🔴 SEPA策略每日运行结果", md_to_plain(push_content))

if __name__ == '__main__':
    run_sepa_bot()