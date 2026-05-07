# -*- coding: utf-8 -*-
"""股票筛选Bot
SEPA 三周期趋势策略自动化筛选
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

# --- 核心修正：给所有变量提供初始默认值，防止NameError ---
BENCHMARK = "SPY"
VOL_MULT = 1.0
MARKET_CAP_MIN, MARKET_CAP_MAX = 5000, 200000
MAX_POSITIONS = 3
STOP_LOSS_PCT, BREAK_EVEN_PCT = 0.07, 0.14

if cfg:
    try:
        # 从配置文件映射参数
        MARKET_CAP_MIN = cfg['filters']['market_cap_min']
        MARKET_CAP_MAX = cfg['filters']['market_cap_max']
        PRICE_MIN = cfg['filters'].get('price_min', 5)
        MA200_RISING_DAYS = cfg['filters'].get('ma200_rising_days', 43)
        EXCLUDE_INDUSTRIES = cfg['filters']['exclude_industries']

        WEEKLY_BIAS_LIMIT = cfg['thresholds']['weekly_bias_limit']
        DAILY_RSI_LIMIT = cfg['thresholds']['daily_rsi_limit']

        MAX_POSITIONS = cfg['timing']['max_positions']
        STOP_LOSS_PCT = cfg['timing']['stop_loss_pct']
        BREAK_EVEN_PCT = cfg['timing']['break_even_pct']
        
        # 显式提取这两个报错的变量
        BENCHMARK = cfg['timing'].get('benchmark', "SPY")
        VOL_MULT = cfg['timing'].get('vol_mult', 1.0)
        
        print("✅ 策略参数已从 config.yaml 成功加载")
    except KeyError as e:
        print(f"⚠️ 配置文件格式错误，缺少键值: {e}。将使用程序内置默认值。")
else:
    print("⚠️ 未找到配置文件，正在使用内置默认参数运行...")

# 保持从环境变量读取密钥
SERVER_CHAN_SENDKEY = os.getenv("SERVER_CHAN_SENDKEY", "")

# ===================== 微信推送函数 =====================
def send_wechat_msg(content):
    """Server酱微信推送函数，传入推送内容，自动发送到微信"""
    if not SERVER_CHAN_SENDKEY:
        print("⚠️ 未配置Server酱SendKey，跳过微信推送")
        return False
    url = f"https://sctapi.ftqq.com/{SERVER_CHAN_SENDKEY}.send"
    data = {
        "title": "🔴 SEPA策略每日运行结果",
        "desp": content
    }
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

# ===================== 策略核心函数 =====================
def get_finviz_screened_tickers():
    """封装finviz自动初筛函数，API参数适配+Python二次校验"""
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

        # 排除特定行业
        if 'Industry' in df_res.columns:
            df_res = df_res[~df_res['Industry'].str.contains('|'.join(EXCLUDE_INDUSTRIES), case=False, na=False)]
        
        initial_tickers = df_res['Ticker'].tolist()
        final_tickers = []

        # 市值二次精准校验
        print(f"🔍 正在进行市值二次校验 (目标范围: {MARKET_CAP_MIN}M - {MARKET_CAP_MAX}M)...")
        for ticker in initial_tickers:
            try:
                info = yf.Ticker(ticker).fast_info
                mkt_cap_m = info['marketCap'] / 1e6
                if MARKET_CAP_MIN <= mkt_cap_m <= MARKET_CAP_MAX:
                    final_tickers.append(ticker)
            except:
                continue
            
        print(f"✅ finviz初筛完成，最终获取到 {len(final_tickers)} 只符合条件的标的")
        return final_tickers
    except Exception as e:
        print(f"❌ finviz初筛接口调用失败: {e}")
        return []

def get_data(symbol: str, interval='1d'):
    """获取K线数据的核心函数"""
    period = "2y" if interval == '1wk' else "1y"
    if interval == '1h':
        period = "2y"
    df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=True)
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df

def check_weekly_gate(ticker, df_w):
    """周线SEPA闸门函数"""
    if len(df_w) < 52:
        return False, "周线数据不足一年"
    df_w['MA20'] = df_w['Close'].rolling(20).mean()
    df_w['MA50'] = df_w['Close'].rolling(50).mean()
    curr = df_w.iloc[-1]
    prev_12 = df_w['MA20'].iloc[-12:]
    prev_12_50 = df_w['MA50'].iloc[-12:]

    if not (prev_12.is_monotonic_increasing and prev_12_50.is_monotonic_increasing):
        return False, "周线均线未持续向上"
    if not (curr['Close'] > curr['MA20'] > curr['MA50']):
        return False, "周线价格未站稳均线或排列错误"
    if curr['Close'] / curr['MA50'] > WEEKLY_BIAS_LIMIT:
        return False, f"周线超买(乖离率>{WEEKLY_BIAS_LIMIT})"
    
    return True, "周线通过"

def check_daily_core(ticker, df, spy_df):
    """日线SEPA核心选股函数"""
    if len(df) < 250:
        return False, "日线历史数据不足", None
    # 计算核心指标
    df['MA50'] = df['Close'].rolling(50).mean()
    df['MA150'] = df['Close'].rolling(150).mean()
    df['MA200'] = df['Close'].rolling(200).mean()
    df['VOL_MA20'] = df['Volume'].rolling(20).mean()
    df['MIN_VOL20'] = df['Volume'].rolling(20).min()
    df['HIGH10_PREV'] = df['High'].shift(1).rolling(10).max()
    # RSI计算
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    df['RSI14'] = 100 - (100 / (1 + (gain / loss)))

    # 对齐大盘数据，严格使用前一交易日完整数据
    common = df.index.intersection(spy_df.index)
    if len(common) < 2:
        return False, "指数数据对齐失败", None
    t = df.loc[common].iloc[-2]

    # 核心筛选条件
    if not (t['MA50'] > t['MA150'] > t['MA200']):
        return False, "日线均线未完全多头排列", None
    # 双重相对强度校验
    rs20_stock = df['Close'].pct_change(20).loc[common].iloc[-2]
    rs20_spy = spy_df['Close'].pct_change(20).loc[common].iloc[-2]
    rs250_stock = df['Close'].pct_change(250).loc[common].iloc[-2]
    rs250_spy = spy_df['Close'].pct_change(250).loc[common].iloc[-2]
    if rs20_stock <= rs20_spy or rs250_stock <= rs250_spy:
        return False, "相对强度(RS)未跑赢大盘", None
    # 突破校验
    if t['Close'] <= t['HIGH10_PREV']:
        return False, "未有效突破前10日高点", None
    # 成交量校验
    if t['Volume'] < t['VOL_MA20'] * VOL_MULT or t['MIN_VOL20'] < 500000:
        return False, "成交量未放量或流动性不足", None
    # 超买风控
    if t['Close'] / t['MA200'] > DAILY_BIAS_LIMIT or t['RSI14'] >= DAILY_RSI_LIMIT:
        return False, "日线过度超买", None

    return True, "日线通过", t

def get_hourly_timing(ticker):
    """小时线择时提示函数"""
    try:
        df_h = get_data(ticker, interval='1h')
        if len(df_h) < 60:
            return "小时线数据不足"
        df_h['MA20'] = df_h['Close'].rolling(20).mean()
        df_h['MA60'] = df_h['Close'].rolling(60).mean()
        # 小时线RSI计算
        delta = df_h['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rsi = (100 - (100 / (1 + gain/loss))).iloc[-1]
        curr = df_h.iloc[-1]

        if curr['MA20'] > curr['MA60'] and HOURLY_RSI_LOW <= rsi <= HOURLY_RSI_HIGH and curr['Close'] > curr['MA20']:
            return "✅ 小时线择时信号：当前是低吸机会"
        elif rsi > HOURLY_RSI_OVERBOUGHT:
            return "⚠️ 小时线择时信号：短期严重超买"
        else:
            return "ℹ️ 小时线择时信号：中性"
    except:
        return "小时线择时计算出错"

# ===================== 主运行函数 =====================
def run_sepa_bot():
    tz_utc_8 = timezone(timedelta(hours=8))
    run_time = datetime.now(tz_utc_8).strftime('%Y-%m-%d %H:%M:%S')
    print(f"===== SEPA策略运行时间：{run_time} =====")
    
    # 初始化推送内容
    push_content = f"## SEPA策略运行时间：{run_time}\n\n"
    tickers = get_finviz_screened_tickers()

    # 初筛无标的的情况
    if not tickers:
        push_content += "❌ finviz初筛无符合条件的标的，请检查参数"
        print(push_content)
        send_wechat_msg(push_content)
        return

    push_content += f"✅ finviz初筛完成，共获取到 {len(tickers)} 只候选标的\n\n"
    spy_df = get_data(BENCHMARK)
    buy_signals = []

    # 三周期循环筛选
    for ticker in tickers:
        try:
            # 周线闸门
            df_w = get_data(ticker, interval='1wk')
            pass_w, msg_w = check_weekly_gate(ticker, df_w)
            if not pass_w:
                continue
            # 日线核心筛选
            df_d = get_data(ticker, interval='1d')
            pass_d, msg_d, last_row = check_daily_core(ticker, df_d, spy_df)
            if not pass_d:
                continue
            # 小时线择时
            timing_msg = get_hourly_timing(ticker)
            p = last_row['Close']
            buy_signals.append({
                "ID": ticker, 
                "Price": round(p, 2), 
                "SL": round(p*(1-STOP_LOSS_PCT), 2),
                "BE": round(p*(1+BREAK_EVEN_PCT), 2), 
                "Msg": f"{msg_w} | {msg_d}", 
                "Timing": timing_msg
            })
        except Exception as e:
            print(f"[跳过] {ticker}: 数据处理出错 {e}")
            continue

    # 控制台打印+推送内容拼接
    print("\n" + "="*15 + " 🔴 三周期SEPA明日入场推荐 " + "="*15)
    if not buy_signals:
        push_content += "### 最终结果：暂无符合三周期SEPA标准的标的\n"
        print("→ 暂无符合标的")
    else:
        push_content += "### 最终符合条件的标的（最多3只）：\n\n"
        for i in buy_signals[:MAX_POSITIONS]:
            # 控制台打印
            print(f"• {i['ID']} | 触发价: {i['Price']} | 7%止损: {i['SL']} | 14%保本位: {i['BE']}")
            print(f"  ├─ 状态: {i['Msg']}\n  └─ 建议: {i['Timing']}")
            # 微信推送内容拼接
            push_content += f"#### 标的代码：{i['ID']}\n"
            push_content += f"- 触发价：{i['Price']}美元\n"
            push_content += f"- 7%止损位：{i['SL']}美元\n"
            push_content += f"- 14%保本位：{i['BE']}美元\n"
            push_content += f"- 周期验证：{i['Msg']}\n"
            push_content += f"- 择时建议：{i['Timing']}\n\n"

    if buy_signals:
        # 将结果转换为 DataFrame
        df_to_save = pd.DataFrame(buy_signals[:MAX_POSITIONS])
        # 增加记录时间列
        df_to_save['Date'] = datetime.now(tz_utc_8).strftime('%Y-%m-%d')
        
        # 2. 保存到 CSV 文件（以追加模式写入）
        csv_file = 'sepa_history_signals.csv'
        # 如果文件不存在，则写入表头；如果存在，则只追加数据
        header = not os.path.exists(csv_file)
        df_to_save.to_csv(csv_file, mode='a', index=False, header=header, encoding='utf-8-sig')
        print(f"💾 已将 {len(df_to_save)} 条信号保存至 {csv_file}")

    # 最终发送微信推送
    send_wechat_msg(push_content)

if __name__ == '__main__':
    run_sepa_bot()
