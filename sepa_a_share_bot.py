# -*- coding: utf-8 -*-
"""A股筛选Bot
SEPA 趋势策略自动化筛选（周线闸门 + 日线核心 + 差一步标的追踪）
数据源：akshare（东方财富）
"""
import os
import smtplib
import yaml
import pandas as pd
import numpy as np
import akshare as ak
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
import warnings
warnings.filterwarnings('ignore')

# ===================== 动态配置加载 =====================
def load_config(config_path="config_a_share.yaml"):
    if not os.path.exists(config_path):
        return None
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

cfg = load_config()

# --- 默认值，防止NameError ---
BENCHMARK = "000300"  # 沪深 300
VOL_MULT = 1.5
MARKET_CAP_MIN, MARKET_CAP_MAX = 100, 50000  # 亿元
MAX_POSITIONS = 5
STOP_LOSS_PCT, BREAK_EVEN_PCT = 0.05, 0.10
WEEKLY_BIAS_LIMIT = 1.5
DAILY_BIAS_LIMIT = 1.6
DAILY_RSI_LIMIT = 75
EXCLUDE_INDUSTRIES = ["银行", "保险"]
EXCLUDE_ST = True

if cfg:
    try:
        MARKET_CAP_MIN = cfg['filters']['market_cap_min']
        MARKET_CAP_MAX = cfg['filters']['market_cap_max']
        EXCLUDE_INDUSTRIES = cfg['filters'].get('exclude_industries', [])
        EXCLUDE_ST = cfg['filters'].get('exclude_st', True)

        WEEKLY_BIAS_LIMIT = cfg['thresholds']['weekly_bias_limit']
        DAILY_BIAS_LIMIT = cfg['thresholds']['daily_bias_limit']
        DAILY_RSI_LIMIT = cfg['thresholds']['daily_rsi_limit']

        MAX_POSITIONS = cfg['timing']['max_positions']
        STOP_LOSS_PCT = cfg['timing']['stop_loss_pct']
        BREAK_EVEN_PCT = cfg['timing']['break_even_pct']

        BENCHMARK = cfg['timing'].get('benchmark', "000300")
        VOL_MULT = cfg['timing'].get('vol_mult', 1.5)

        print("✅ A股策略参数已从 config_a_share.yaml 成功加载")
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
    msg["From"] = formataddr(("SEPA A股 Bot", SMTP_USER))
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
def is_a_share_trading_day():
    """判断今天是否为A股交易日（XSHG 上交所日历）"""
    try:
        import exchange_calendars as xcals
        xshg = xcals.get_calendar("XSHG")
        return xshg.is_session(datetime.now(ZoneInfo("Asia/Shanghai")).date())
    except Exception as e:
        print(f"⚠️ 交易日历加载失败({type(e).__name__}: {e})，默认视为交易日")
        return True

# ===================== 初筛函数 =====================
def get_a_share_screened_tickers():
    """akshare A股初筛 + ST排除 + 市值/成交量过滤"""
    try:
        # 获取全部A股实时行情
        print("📊 正在获取A股实时行情...")
        df_all = ak.stock_zh_a_spot_em()
        
        if df_all is None or df_all.empty:
            print("❌ akshare 返回空数据")
            return []
        
        print(f"🔍[排查] 环节① akshare 原始返回: {len(df_all)} 只 | 列名: {list(df_all.columns)}")
        
        # 排除 ST 股
        if EXCLUDE_ST and '名称' in df_all.columns:
            df_all = df_all[~df_all['名称'].str.contains('ST', case=False, na=False)]
            print(f"🔍[排查] 环节② 排除ST后: {len(df_all)} 只")
        
        # 排除特定行业
        if '行业' in df_all.columns and EXCLUDE_INDUSTRIES:
            exclude_pattern = '|'.join(EXCLUDE_INDUSTRIES)
            df_all = df_all[~df_all['行业'].str.contains(exclude_pattern, case=False, na=False)]
            print(f"🔍[排查] 环节③ 排除行业后: {len(df_all)} 只")
        
        # 市值过滤（单位：亿元）
        if '总市值' in df_all.columns:
            mc_yi = df_all['总市值'] / 1e8  # 转为亿元
            before = len(df_all)
            df_all = df_all[(mc_yi >= MARKET_CAP_MIN) & (mc_yi <= MARKET_CAP_MAX)]
            print(f"🔍[排查] 环节④ 市值过滤后: {before} → {len(df_all)} 只 (范围 {MARKET_CAP_MIN}-{MARKET_CAP_MAX}亿)")
        
        # 成交量过滤（成交额 > 1亿元）
        if '成交额' in df_all.columns:
            df_all = df_all[df_all['成交额'] > 1e8]
            print(f"🔍[排查] 环节⑤ 成交额过滤后: {len(df_all)} 只")
        
        # 股价过滤
        if '最新价' in df_all.columns:
            price_min = 3  # 最低股价
            df_all = df_all[df_all['最新价'] >= price_min]
            print(f"🔍[排查] 环节⑥ 股价过滤后: {len(df_all)} 只 (最低 {price_min}元)")
        
        # 提取股票代码
        tickers = df_all['代码'].tolist()
        print(f"✅ 初筛完成，共获取到 {len(tickers)} 只候选标的")
        return tickers
        
    except Exception as e:
        print(f"❌ A股初筛接口调用失败: {type(e).__name__}: {e}")
        return []

# ===================== 数据获取 =====================
def get_data_a_share(symbol: str, interval='daily'):
    """获取A股单只K线数据（akshare）"""
    try:
        end_date = datetime.now().strftime('%Y%m%d')
        
        if interval == 'weekly':
            # 周线需要2年数据
            start_date = (datetime.now() - timedelta(days=730)).strftime('%Y%m%d')
            period = 'weekly'
        else:
            # 日线需要1年数据
            start_date = (datetime.now() - timedelta(days=365)).strftime('%Y%m%d')
            period = 'daily'
        
        df = ak.stock_zh_a_hist(
            symbol=symbol,
            period=period,
            start_date=start_date,
            end_date=end_date,
            adjust="qfq"  # 前复权
        )
        
        if df.empty:
            return df
        
        # 列名映射（akshare返回中文列名）
        column_mapping = {
            '日期': 'Date',
            '开盘': 'Open',
            '收盘': 'Close',
            '最高': 'High',
            '最低': 'Low',
            '成交量': 'Volume',
            '成交额': 'Amount',
            '振幅': 'Amplitude',
            '涨跌幅': 'Change_pct',
            '涨跌额': 'Change',
            '换手率': 'Turnover'
        }
        
        df = df.rename(columns=column_mapping)
        
        # 转换日期为索引
        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'])
            df = df.set_index('Date')
        
        return df
        
    except Exception as e:
        print(f"⚠️ 获取 {symbol} 数据失败: {type(e).__name__}: {e}")
        return pd.DataFrame()

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
    failed_conditions_list 含具体失败原因及数值
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
    tz_cn = timezone(timedelta(hours=8))
    run_time = datetime.now(tz_cn).strftime('%Y-%m-%d %H:%M:%S')
    print(f"===== A股SEPA策略运行时间：{run_time} =====")

    # ===== 非交易日跳过 =====
    if not is_a_share_trading_day():
        msg = (f"## A股SEPA策略今日休市\n\n"
               f"- 运行时间：{run_time}（北京时间）\n"
               f"- 原因：A股今日非交易日\n"
               f"- 结果：今日不进行筛选，明日自动恢复正常运行。")
        print(msg)
        send_email_msg("📭 A股SEPA策略：今日休市", md_to_plain(msg))
        return

    push_content = f"## A股SEPA策略运行时间：{run_time}\n\n"
    tickers = get_a_share_screened_tickers()

    if not tickers:
        push_content += "❌ A股初筛无符合条件的标的，请检查参数"
        print(push_content)
        send_email_msg("🔴 A股SEPA策略每日运行结果", md_to_plain(push_content))
        return

    push_content += f"✅ A股初筛完成，共获取到 {len(tickers)} 只候选标的\n\n"
    spy_df = get_data_a_share(BENCHMARK)
    buy_signals = []
    near_misses = []  # 差一个条件就能入选

    # 循环筛选（A股暂时不批量下载，逐只获取）
    print(f"📥 开始逐只获取数据并筛选...")
    for i, ticker in enumerate(tickers):
        if i % 50 == 0 and i > 0:
            print(f"  进度: {i}/{len(tickers)}")
        
        try:
            # 周线闸门（逐条件追踪）
            df_w = get_data_a_share(ticker, interval='weekly')
            if df_w.empty:
                print(f"[跳过] {ticker}: 周线数据为空")
                continue
            
            pass_w, msg_w, failed_w = check_weekly_gate(ticker, df_w)
            if not pass_w:
                if len(failed_w) == 1:
                    near_misses.append({"ID": ticker, "Level": "周线", "Reason": failed_w[0]})
                continue

            # 日线核心（逐条件追踪）
            df_d = get_data_a_share(ticker, interval='daily')
            if df_d.empty:
                print(f"[跳过] {ticker}: 日线数据为空")
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
    print("\n" + "=" * 15 + " 🔴 A股三周期SEPA明日入场推荐 " + "=" * 15)

    # --- 完整入选标的 ---
    if not buy_signals:
        push_content += "### 最终结果：暂无符合三周期SEPA标准的标的\n"
        print("→ 暂无符合标的")
    else:
        push_content += f"### 完整入选标的（共{len(buy_signals)}只，展示前{MAX_POSITIONS}只）：\n\n"
        for i in buy_signals[:MAX_POSITIONS]:
            print(f"• {i['ID']} | 触发价: {i['Price']} | 5%止损: {i['SL']} | 10%保本位: {i['BE']}")
            print(f"  └─ 状态: {i['Msg']}")
            push_content += (
                f"#### {i['ID']}\n"
                f"- 触发价：{i['Price']} 元\n"
                f"- 5%止损位：{i['SL']} 元\n"
                f"- 10%保本位：{i['BE']} 元\n"
                f"- 周期验证：{i['Msg']}\n\n"
            )

    # --- 差一步标的 ---
    if near_misses:
        # 最多展示15只
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
        df_to_save['Date'] = datetime.now(tz_cn).strftime('%Y-%m-%d')
        csv_file = 'sepa_a_share_signals.csv'
        header = not os.path.exists(csv_file)
        df_to_save.to_csv(csv_file, mode='a', index=False, header=header, encoding='utf-8-sig')
        print(f"💾 已将 {len(df_to_save)} 条信号保存至 {csv_file}")

    # 最终推送
    send_email_msg("🔴 A股SEPA策略每日运行结果", md_to_plain(push_content))

if __name__ == '__main__':
    run_sepa_bot()
