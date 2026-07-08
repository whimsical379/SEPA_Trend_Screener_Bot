# -*- coding: utf-8 -*-
"""股票筛选Bot
SEPA 趋势策略自动化筛选（周线闸门 + 日线核心 + 差一步标的追踪）
"""
import os
import yaml
import pandas as pd
import numpy as np
import yfinance as yf
import requests
from datetime import datetime, timedelta, timezone
from finvizfinance.screener.overview import Overview
import warnings
warnings.filterwarnings('ignore')

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

SERVER_CHAN_SENDKEY = os.getenv("SERVER_CHAN_SENDKEY", "")

# ===================== 微信推送函数 =====================
def send_wechat_msg(content):
    """Server酱微信推送函数"""
    if not SERVER_CHAN_SENDKEY:
        print("⚠️ 未配置Server酱SendKey，跳过微信推送")
        return False
    url = f"https://sctapi.ftqq.com/{SERVER_CHAN_SENDKEY}.send"
    data = {"title": "🔴 SEPA策略每日运行结果", "desp": content}
    try:
        response = requests.post(url, data=data)
        if response.status_code == 200:
            print("✅ 微信推送发送成功")
            return True
        else:
            print(f"❌ 微信推送发送失败，状态码：{response.status_code}")
            return False
    except Exception as e:
        print(f"❌ 微信推送出错：{e}")
        return False

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

        if df_res is None or df_res.empty:
            return []

        if 'Industry' in df_res.columns:
            exclude_pattern = '|'.join(EXCLUDE_INDUSTRIES)
            df_res = df_res[~df_res['Industry'].str.contains(exclude_pattern, case=False, na=False)]

        initial_tickers = df_res['Ticker'].tolist()
        final_tickers = []

        print(f"🔍 正在进行市值二次校验 (目标范围: {MARKET_CAP_MIN}M - {MARKET_CAP_MAX}M)...")
        for ticker in initial_tickers:
            try:
                info = yf.Ticker(ticker).fast_info
                mkt_cap_m = info['marketCap'] / 1e6
                if MARKET_CAP_MIN <= mkt_cap_m <= MARKET_CAP_MAX:
                    final_tickers.append(ticker)
            except Exception:
                continue

        print(f"✅ finviz初筛完成，最终获取到 {len(final_tickers)} 只符合条件的标的")
        return final_tickers
    except Exception as e:
        print(f"❌ finviz初筛接口调用失败: {e}")
        return []

# ===================== 数据获取 =====================
def get_data(symbol: str, interval='1d'):
    """获取K线数据"""
    period = "2y" if interval == '1wk' else "1y"
    df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=True)
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df

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

    push_content = f"## SEPA策略运行时间：{run_time}\n\n"
    tickers = get_finviz_screened_tickers()

    if not tickers:
        push_content += "❌ finviz初筛无符合条件的标的，请检查参数"
        print(push_content)
        send_wechat_msg(push_content)
        return

    push_content += f"✅ finviz初筛完成，共获取到 {len(tickers)} 只候选标的\n\n"
    spy_df = get_data(BENCHMARK)
    buy_signals = []
    near_misses = []  # 差一个条件就能入选

    # 循环筛选
    for ticker in tickers:
        try:
            # 周线闸门（逐条件追踪）
            df_w = get_data(ticker, interval='1wk')
            pass_w, msg_w, failed_w = check_weekly_gate(ticker, df_w)
            if not pass_w:
                if len(failed_w) == 1:
                    near_misses.append({"ID": ticker, "Level": "周线", "Reason": failed_w[0]})
                continue

            # 日线核心（逐条件追踪）
            df_d = get_data(ticker, interval='1d')
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
    send_wechat_msg(push_content)

if __name__ == '__main__':
    run_sepa_bot()