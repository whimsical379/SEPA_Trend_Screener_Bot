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
# 修复①（2026-09-30 起生效的 finviz WAF）：
#   finvizfinance 1.3.0 的 UA 字符串因 Python 换行续行符带有 20 个空格（畸形 UA），
#   finviz 新 WAF 对其直接返回 403 Forbidden，导致初筛失败、Bot 空转。
#   补丁：替换为干净的现代浏览器请求头。
# 修复②（finviz 2026 年中页面改版）：
#   新版 /screener 改为 SSR island 架构，表格由前端渲染，HTML 中不再存在
#   <table class="screener_table"> 与 id="pageSelect"，旧 DOM 解析全部失效；
#   但完整筛选数据以纯 JSON 岛嵌在页面的 <script> 中（"data":{"rows":[...],
#   "totalCount":N,"pageSize":20}）。补丁：重写 screener_view，改为逐页解析 JSON 岛。
#   Market Cap 单位沿用旧 number_covert 语义（美元），保持下游市值过滤逻辑不变。
# 注：原 finvizfinance issue #158 的 Ticker 解析补丁已随 DOM 一起失效，不再需要。
def _install_finviz_ticker_patch():
    import json as _json
    from finvizfinance import util as _futil
    from finvizfinance.screener import base as _base

    # --- 修复①：浏览器请求头 ---
    _futil.headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def _extract_island_json(soup):
        """从页面 <script> 中提取筛选数据 JSON 岛，失败返回 None"""
        for s in soup.find_all("script"):
            txt = s.get_text() or ""
            if '"rows":[' not in txt:
                continue
            start = txt.find("{")
            try:
                return _json.loads(txt[start:])
            except _json.JSONDecodeError:
                continue
        return None

    def _rows_to_frame(rows, row_offset=0):
        """JSON rows → DataFrame（列名与旧版 finvizfinance 对齐，Market Cap 单位=美元）"""
        frame = []
        for i, r in enumerate(rows):
            t = r.get("text", {}) or {}
            n = r.get("numbers", {}) or {}
            frame.append({
                "No.": row_offset + i + 1,
                "Ticker": t.get("ticker", ""),
                "Company": t.get("company", ""),
                "Sector": t.get("sector", ""),
                "Industry": t.get("industry", ""),
                "Country": t.get("country", ""),
                "Market Cap": (n.get("marketCap") or 0) * 1e6,  # JSON单位=百万美元 → 美元
                "P/E": n.get("PE"),
                "Price": n.get("price"),
                "Change": n.get("change"),
                "Volume": n.get("volume"),
            })
        return pd.DataFrame(frame)

    def _patched_screener_view(self, order="Ticker", limit=100000, select_page=None,
                               verbose=1, ascend=True, columns=None, sleep_sec=1):
        from time import sleep as _sleep
        from finvizfinance.constants import order_dict
        from finvizfinance.util import web_scrap as _web_scrap

        if order not in order_dict:
            order_keys = list(order_dict.keys())
            raise ValueError(
                "Invalid order '{}'. Possible order: {}".format(order, order_keys)
            )
        self.request_params["o"] = ("" if ascend else "-") + order_dict[order]

        if select_page:
            self.request_params["r"] = (select_page - 1) * self.size + 1
            soup = _web_scrap(self.url, self.request_params)
            data = _extract_island_json(soup)
            if data is None:
                print("No ticker found.")
                return None
            warnings.warn("Limit parameter is ignored when page is selected.")
            return _rows_to_frame(data["data"].get("rows", []),
                                  (select_page - 1) * self.size)

        # 逐页拉取（pageSize=20，r 为 1-based 行偏移）
        all_rows = []
        total = None
        page = 0
        while True:
            if page == 0:
                self.request_params.pop("r", None)
            else:
                self.request_params["r"] = page * self.size + 1
            soup = _web_scrap(self.url, self.request_params)
            data = _extract_island_json(soup)
            if data is None:
                break
            meta = data.get("data", {})
            total = meta.get("totalCount", 0)
            rows = meta.get("rows", [])
            all_rows.extend(rows)
            page += 1
            if verbose == 1 and total:
                print(f"[info] fetching {min(page * self.size, total)}/{total} rows")
            if not rows or len(all_rows) >= total:
                break
            if limit != -1 and len(all_rows) >= limit:
                break
            _sleep(sleep_sec)

        if not all_rows:
            print("No ticker found.")
            return None
        if limit != -1:
            all_rows = all_rows[:limit]
        return _rows_to_frame(all_rows)

    _base.Base.screener_view = _patched_screener_view


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
        EXCLUDE_INDUSTRIES = cfg['filters'].get('exclude_industries', [])

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

# ===================== 策略版本戳 =====================
# 随每条信号写入 CSV（StrategyVersion 列）。筛选逻辑发生大改版时必须递增，
# 便于未来统计时按"策略代际"分组，避免新旧逻辑数据混用导致失真。
STRATEGY_VERSION = "v1.6"

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


# ===================== HTML 邮件模板（v1.6：QQ邮箱支持 HTML，表格布局保证客户端兼容） =====================
def build_email_html(title, body_html):
    """邮件外壳：深色标题栏 + 白色内容区，纯内联样式（客户端兼容）"""
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        '<body style="margin:0;padding:0;background:#f4f5f7;">'
        '<div style="max-width:640px;margin:0 auto;padding:16px;'
        'font-family:-apple-system,\'Segoe UI\',\'Microsoft YaHei\',Arial,sans-serif;">'
        '<div style="background:#1f2937;border-radius:12px 12px 0 0;padding:18px 24px;">'
        f'<div style="color:#ffffff;font-size:18px;font-weight:bold;">{title}</div>'
        f'<div style="color:#9ca3af;font-size:12px;margin-top:4px;">SEPA Trend Screener Bot · {STRATEGY_VERSION}</div>'
        '</div>'
        f'<div style="background:#ffffff;border-radius:0 0 12px 12px;padding:20px 24px;">{body_html}</div>'
        '<p style="color:#9ca3af;font-size:11px;text-align:center;margin-top:12px;">'
        '本邮件由 SEPA Trend Screener Bot 自动发送 · 仅供研究参考，不构成投资建议</p>'
        '</div></body></html>'
    )


def stock_card_html(idx, sig):
    """完整入选标的卡片：代码+公司/行业、三列价格、关键指标"""
    company = sig.get("Company") or ""
    sector = sig.get("Sector") or ""
    sub = f"{company}{' · ' + sector if sector else ''}"
    metric_line = ""
    if sig.get("RSI14") is not None:
        metric_line = (
            f'RSI14 <b>{sig["RSI14"]}</b> · 乖离MA200 <b>{sig["Bias200"]}</b>'
            f' · 量比 <b>{sig["VolRatio"]}x</b><br>'
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border:1px solid #e5e7eb;border-radius:10px;margin-bottom:14px;border-collapse:separate;">'
        '<tr><td style="padding:12px 16px 4px 16px;">'
        f'<span style="font-size:20px;font-weight:bold;color:#111827;">#{idx} {sig["ID"]}</span> '
        f'<span style="font-size:12px;color:#6b7280;">{sub}</span>'
        '</td></tr>'
        '<tr><td style="padding:8px 16px 14px 16px;">'
        '<table width="100%" cellpadding="0" cellspacing="0"><tr>'
        '<td width="33%" style="text-align:center;background:#f9fafb;border-radius:8px;padding:10px 0;">'
        '<div style="font-size:11px;color:#6b7280;">触发价</div>'
        f'<div style="font-size:17px;font-weight:bold;color:#111827;">{sig["Price"]}</div></td>'
        '<td width="34%" style="text-align:center;background:#fef2f2;border-radius:8px;padding:10px 0;">'
        '<div style="font-size:11px;color:#6b7280;">止损位</div>'
        f'<div style="font-size:17px;font-weight:bold;color:#b91c1c;">{sig["SL"]}</div></td>'
        '<td width="33%" style="text-align:center;background:#ecfdf5;border-radius:8px;padding:10px 0;">'
        '<div style="font-size:11px;color:#6b7280;">保本位</div>'
        f'<div style="font-size:17px;font-weight:bold;color:#047857;">{sig["BE"]}</div></td>'
        '</tr></table>'
        '<div style="font-size:12px;color:#4b5563;margin-top:10px;line-height:1.6;">'
        + metric_line +
        f'<span style="color:#9ca3af;">{sig["Msg"]}</span></div>'
        '</td></tr></table>'
    )


def heartbeat_body_html(n_candidates, run_time, note=""):
    """无入选日的极简心跳邮件：证明 Bot 活着"""
    note_html = f'<p style="font-size:12px;color:#9ca3af;">{note}</p>' if note else ""
    return (
        '<p style="font-size:14px;color:#374151;">今日筛选已完成，'
        '<b>暂无符合全部条件的完整入选标的</b>。</p>'
        f'<p style="font-size:13px;color:#6b7280;">初筛候选 {n_candidates} 只 · 运行时间 {run_time}（北京时间）</p>'
        + note_html +
        '<p style="font-size:12px;color:#9ca3af;">本邮件为活性心跳通知：收到即代表 Bot 运行正常。</p>'
    )


def review_table_html(rows, summary_line):
    """信号复盘表格：日期/代码/触发价/现价/盈亏/区间高低/状态"""
    status_color = {"已触止损": "#b91c1c", "已达保本": "#047857", "持有中": "#1d4ed8",
                    "数据缺失": "#6b7280"}
    header_cells = "".join(
        f'<th style="background:#1f2937;color:#ffffff;padding:8px 10px;font-size:12px;text-align:left;">{h}</th>'
        for h in ["信号日", "代码", "公司", "触发价", "现价", "浮动盈亏", "区间最高", "区间最低", "状态"]
    )
    body_rows = ""
    for r in rows:
        pnl_color = "#047857" if r["PnL"] > 0 else ("#b91c1c" if r["PnL"] < 0 else "#6b7280")
        st_color = status_color.get(r["Status"], "#374151")
        body_rows += (
            '<tr>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #f3f4f6;font-size:12px;color:#374151;">{r["Date"]}</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #f3f4f6;font-size:12px;font-weight:bold;color:#111827;">{r["ID"]}</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #f3f4f6;font-size:12px;color:#6b7280;">{r["Company"]}</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #f3f4f6;font-size:12px;color:#374151;">{r["Price"]}</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #f3f4f6;font-size:12px;color:#374151;">{r["Cur"]}</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #f3f4f6;font-size:12px;font-weight:bold;color:{pnl_color};">{r["PnL_str"]}</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #f3f4f6;font-size:12px;color:#374151;">{r["High"]}</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #f3f4f6;font-size:12px;color:#374151;">{r["Low"]}</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #f3f4f6;font-size:12px;font-weight:bold;color:{st_color};">{r["Status"]}</td>'
            '</tr>'
        )
    return (
        f'<p style="font-size:13px;color:#374151;">{summary_line}</p>'
        '<table width="100%" cellpadding="0" cellspacing="0" '
        'style="border:1px solid #e5e7eb;border-radius:8px;border-collapse:separate;overflow:hidden;">'
        f'<tr>{header_cells}</tr>{body_rows}</table>'
    )


def send_email_msg(title, content, html=False):
    """SMTP 邮件推送（QQ邮箱，支持多收件人；html=True 时正文为 HTML）"""
    to_list = [x.strip() for x in SMTP_TO.split(",") if x.strip()]
    if not (SMTP_USER and SMTP_PASS and to_list):
        print("⚠️ 未配置SMTP参数(SMTP_USER/SMTP_PASS/SMTP_TO)，跳过邮件推送")
        return False
    msg = MIMEText(content, "html" if html else "plain", "utf-8")
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
    """finviz自动初筛 + 市值二次校验
    返回: (tickers列表, finviz原始DataFrame) —— DataFrame 含 Company/Sector，供邮件展示
    """
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
            return [], None
        print(f"🔍[排查] 环节① finviz 原始返回: {len(df_res)} 只 | 列名: {list(df_res.columns)}")

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
            print(f"🔍[排查] 环节② 市值过滤(finviz Market Cap列): {before} → {len(final_tickers)} 只 "
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
            print(f"🔍[排查] 环节② 市值二次校验后: {len(final_tickers)} 只 (失败/跳过 {skipped} 只)")
        return final_tickers, df_res
    except Exception as e:
        print(f"❌ finviz初筛接口调用失败: {type(e).__name__}: {e}")
        return [], None

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
            # yfinance 1.6.0 移除了 batch_size 参数，改为按列表传入自行分批
            df = yf.download(batch, period=period, interval=interval,
                             group_by='ticker', progress=False,
                             auto_adjust=True, threads=True)
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

# ===================== 非交易日信号绩效复盘（v1.6 新增） =====================
def _get_recent_sessions(n=5):
    """获取最近 n 个美股交易日日期（不含今天），返回 ['YYYY-MM-DD', ...]"""
    import exchange_calendars as xcals
    cal = xcals.get_calendar("XNYS")
    ny_today = datetime.now(ZoneInfo("America/New_York")).date()
    sessions = cal.sessions_in_range(ny_today - timedelta(days=n * 3 + 7), ny_today - timedelta(days=1))
    return [s.strftime('%Y-%m-%d') for s in sessions][-n:]


def run_signal_review(lookback=5):
    """非交易日运行：读取信号 CSV 中最近 n 个交易日的入选信号，
    拉取行情统计后续表现（浮动盈亏 / 是否触止损或保本），发送复盘邮件。
    """
    print(f"===== 非交易日：执行近{lookback}个交易日信号复盘 =====")
    date_str = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d')

    # --- 读取信号 CSV（仅 v1.6 新 schema；缺失或无数据则发心跳） ---
    csv_file = 'sepa_history_signals.csv'
    if not os.path.exists(csv_file):
        print("⚠️ 信号 CSV 不存在，跳过复盘")
        subject = f"📊 SEPA信号复盘 {date_str[5:]} | 暂无信号数据"
        body = heartbeat_body_html(0, datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S'),
                                   note="新版本信号库尚未积累数据，复盘功能将在产生入选信号后自动生效。")
        send_email_msg(subject, build_email_html(subject, body), html=True)
        return

    try:
        df_sig = pd.read_csv(csv_file)
    except Exception as e:
        print(f"⚠️ 信号 CSV 读取失败: {type(e).__name__}: {e}，跳过复盘")
        return

    required_cols = {'Date', 'ID', 'Price', 'SL', 'BE'}
    if df_sig.empty or not required_cols.issubset(df_sig.columns):
        print("⚠️ 信号 CSV 为空或缺少必需列，跳过复盘")
        subject = f"📊 SEPA信号复盘 {date_str[5:]} | 暂无信号数据"
        body = heartbeat_body_html(0, date_str, note="信号库暂无可复盘的数据。")
        send_email_msg(subject, build_email_html(subject, body), html=True)
        return

    # --- 最近 n 个交易日窗口 ---
    try:
        window = _get_recent_sessions(lookback)
    except Exception as e:
        print(f"⚠️ 交易日历获取失败({type(e).__name__}: {e})，回退按自然日窗口")
        d1 = (datetime.now(ZoneInfo("America/New_York")) - timedelta(days=lookback * 2 + 2)).strftime('%Y-%m-%d')
        d2 = (datetime.now(ZoneInfo("America/New_York")) - timedelta(days=1)).strftime('%Y-%m-%d')
        window = [d1, d2]

    sig = df_sig[df_sig['Date'].astype(str).isin(window)].copy()
    print(f"📅 复盘窗口: {window[0]} ~ {window[-1]} | 命中信号 {len(sig)} 条")

    if sig.empty:
        subject = f"📊 SEPA信号复盘 {date_str[5:]} | 近5个交易日无入选"
        body = heartbeat_body_html(0, date_str, note=f"复盘窗口 {window[0]} ~ {window[-1]} 内无入选信号，Bot 运行正常。")
        send_email_msg(subject, build_email_html(subject, body), html=True)
        return

    # --- 拉取信号标的行情并逐条统计 ---
    tickers = sig['ID'].astype(str).unique().tolist()
    try:
        data = yf.download(tickers, start=window[0], interval='1d',
                           group_by='ticker', progress=False, auto_adjust=True, threads=True)
    except Exception as e:
        print(f"❌ 复盘行情下载失败: {type(e).__name__}: {e}")
        data = None

    rows = []
    n_stop = n_be = n_hold = n_miss = 0
    for _, r in sig.iterrows():
        try:
            tk = str(r['ID'])
            sub = None
            if data is not None and not data.empty:
                try:
                    sub = data[tk].dropna()
                except Exception:
                    # 单标的返回可能是单层列索引
                    if tk in (data.columns.get_level_values(0) if isinstance(data.columns, pd.MultiIndex) else []):
                        sub = data.dropna()
            if sub is None or sub.empty:
                rows.append({"Date": r['Date'], "ID": tk, "Company": r.get('Company', ''),
                             "Price": r['Price'], "Cur": "-", "PnL": 0, "PnL_str": "-",
                             "High": "-", "Low": "-", "Status": "数据缺失"})
                n_miss += 1
                continue

            sub = sub.loc[str(r['Date']):]
            if sub.empty:
                rows.append({"Date": r['Date'], "ID": tk, "Company": r.get('Company', ''),
                             "Price": r['Price'], "Cur": "-", "PnL": 0, "PnL_str": "-",
                             "High": "-", "Low": "-", "Status": "数据缺失"})
                n_miss += 1
                continue

            price, sl, be = float(r['Price']), float(r['SL']), float(r['BE'])
            cur = float(sub['Close'].iloc[-1])
            high = float(sub['High'].max())
            low = float(sub['Low'].min())
            pnl = cur / price - 1

            if low <= sl:
                status = "已触止损"; n_stop += 1
            elif high >= be:
                status = "已达保本"; n_be += 1
            else:
                status = "持有中"; n_hold += 1

            rows.append({"Date": r['Date'], "ID": tk, "Company": r.get('Company', ''),
                         "Price": price, "Cur": round(cur, 2), "PnL": pnl,
                         "PnL_str": f"{pnl:+.1%}", "High": round(high, 2),
                         "Low": round(low, 2), "Status": status})
        except Exception as e:
            print(f"⚠️ 复盘 {r.get('ID', '?')} 出错: {type(e).__name__}: {e}")
            n_miss += 1

    # --- 输出与推送 ---
    print("\n" + "=" * 15 + f" 📊 近{lookback}个交易日信号复盘 " + "=" * 15)
    for row in rows:
        print(f"  {row['Date']} {row['ID']:<6} 触发{row['Price']} → 现价{row['Cur']} "
              f"{row['PnL_str']} | {row['Status']}")

    summary = (f"复盘窗口 {window[0]} ~ {window[-1]}，共 {len(rows)} 条信号："
               f"✅持有中 {n_hold} · 🔒已达保本 {n_be} · 🛑已触止损 {n_stop}"
               + (f" · ⚠️数据缺失 {n_miss}" if n_miss else ""))
    subject = f"📊 SEPA信号复盘 {date_str[5:]} | {len(rows)}条信号"
    body = review_table_html(rows, summary)
    send_email_msg(subject, build_email_html(subject, body), html=True)


# ===================== 主运行函数 =====================
def run_sepa_bot():
    tz_utc_8 = timezone(timedelta(hours=8))
    run_time = datetime.now(tz_utc_8).strftime('%Y-%m-%d %H:%M:%S')
    print(f"===== SEPA策略运行时间：{run_time} =====")

    # ===== 非交易日：近5个交易日信号绩效复盘（v1.6） =====
    if not is_us_trading_day():
        run_signal_review()
        return

    date_str = run_time[5:10]  # MM-DD，用于邮件标题

    tickers, screener_df = get_finviz_screened_tickers()

    if not tickers:
        subject = f"📉 SEPA美股筛选 {date_str} | 初筛无结果"
        print("❌ finviz初筛无符合条件的标的，已发送心跳通知")
        send_email_msg(subject, build_email_html(subject, heartbeat_body_html(0, run_time,
                       note="finviz 初筛无符合条件的标的或调用失败，请检查运行日志。")), html=True)
        return

    n_candidates = len(tickers)
    print(f"✅ finviz初筛完成，共获取到 {n_candidates} 只候选标的")
    company_map, sector_map = {}, {}
    if screener_df is not None and 'Company' in screener_df.columns:
        company_map = dict(zip(screener_df['Ticker'].astype(str),
                               screener_df['Company'].astype(str)))
    if screener_df is not None and 'Sector' in screener_df.columns:
        sector_map = dict(zip(screener_df['Ticker'].astype(str),
                              screener_df['Sector'].astype(str)))

    buy_signals = []
    near_misses = []  # 差一个条件就能入选（仅控制台记录，不再进邮件）

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

            # 完全通过（附带当时的关键指标数值，供日后复盘"当时为什么选它"）
            p = last_row['Close']
            buy_signals.append({
                "ID": ticker,
                "Company": company_map.get(ticker, ""),
                "Sector": sector_map.get(ticker, ""),
                "Price": round(p, 2),
                "SL": round(p * (1 - STOP_LOSS_PCT), 2),
                "BE": round(p * (1 + BREAK_EVEN_PCT), 2),
                "MA50": round(last_row['MA50'], 2),
                "MA150": round(last_row['MA150'], 2),
                "MA200": round(last_row['MA200'], 2),
                "RSI14": round(last_row['RSI14'], 1),
                "Bias200": round(p / last_row['MA200'], 3),
                "VolRatio": round(last_row['Volume'] / last_row['VOL_MA20'], 2),
                "Msg": f"{msg_w} | {msg_d}",
                "StrategyVersion": STRATEGY_VERSION,
            })
        except Exception as e:
            print(f"[跳过] {ticker}: 数据处理出错 {e}")
            continue

    # ===================== 输出结果 =====================
    print("\n" + "=" * 15 + " 🔴 三周期SEPA明日入场推荐 " + "=" * 15)

    # --- 完整入选标的（邮件只推完整标的，v1.6） ---
    if not buy_signals:
        print("→ 暂无符合标的")
        subject = f"📉 SEPA美股筛选 {date_str} | 今日无入选"
        body = heartbeat_body_html(n_candidates, run_time)
    else:
        for i in buy_signals[:MAX_POSITIONS]:
            print(f"• {i['ID']} | 触发价: {i['Price']} | 7%止损: {i['SL']} | 14%保本位: {i['BE']}")
            print(f"  └─ 状态: {i['Msg']}")
        subject = f"📈 SEPA美股筛选 {date_str} | 入选 {min(len(buy_signals), MAX_POSITIONS)} 只"
        body = "".join(stock_card_html(k, s) for k, s in enumerate(buy_signals[:MAX_POSITIONS], 1))
        body += (f'<p style="font-size:12px;color:#9ca3af;margin-top:8px;">'
                 f'初筛候选 {n_candidates} 只 · 运行时间 {run_time}（北京时间）</p>')

    # --- 差一步标的（仅控制台记录） ---
    if near_misses:
        print(f"\n--- 差一步就入选（共{len(near_misses)}只，仅控制台记录）---")
        near_misses.sort(key=lambda x: (0 if x['Level'] == '日线' else 1, x['ID']))
        for nm in near_misses:
            print(f"  ⚡ {nm['ID']} | {nm['Level']} | {nm['Reason']}")

    # --- CSV 持久化（v1.6 新 schema：带版本戳与关键指标） ---
    if buy_signals:
        df_to_save = pd.DataFrame(buy_signals[:MAX_POSITIONS])
        df_to_save['Date'] = datetime.now(tz_utc_8).strftime('%Y-%m-%d')
        csv_file = 'sepa_history_signals.csv'
        header = not os.path.exists(csv_file)
        df_to_save.to_csv(csv_file, mode='a', index=False, header=header, encoding='utf-8-sig')
        print(f"💾 已将 {len(df_to_save)} 条信号保存至 {csv_file} (StrategyVersion={STRATEGY_VERSION})")

    # 最终推送（HTML 邮件：完整标的 / 心跳）
    send_email_msg(subject, build_email_html(subject, body), html=True)

if __name__ == '__main__':
    run_sepa_bot()