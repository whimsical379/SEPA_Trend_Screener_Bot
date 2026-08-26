# -*- coding: utf-8 -*-
"""
SEPA 初筛故障诊断脚本 (diagnose_screener.py)
=============================================
定位「初筛数量突然减少」的根源，分环节检查：

  [A] finvizfinance 库版本 / 安装路径
  [B] 库内置的 Market Cap 过滤选项是否仍包含硬编码值 '+Large (over $10bln)'
  [C] 带市值过滤调用 finviz screener（与主程序完全一致的条件）
  [D] 去掉市值过滤再次调用，对比数量差
  [E] yfinance fast_info 抽样校验成功率（市值二次校验环节）

用法: python diagnose_screener.py
"""
import sys
import time

# ===================== 加载生产补丁 =====================
# 与 sepa_screener_bot.py 保持一致的 finvizfinance 兼容补丁
# （修复 finviz.com 改版导致的 Ticker 重复首字母 bug），
# 确保诊断结果反映"修复后"的真实行为。
try:
    import sepa_screener_bot as _bot  # noqa: F401
    print("[INFO] 已加载 sepa_screener_bot 兼容补丁（Ticker 解析与生产一致）")
except Exception as e:
    print(f"[WARN] 未加载 sepa_screener_bot 补丁: {e}")

# ===================== [A] 库版本 =====================
def diagnose_library():
    import finvizfinance
    try:
        from importlib.metadata import version
        ver = version("finvizfinance")
    except Exception:
        ver = getattr(finvizfinance, "__version__", "未知")
    print(f"[A] finvizfinance 版本: {ver}")
    print(f"[A] Python 版本: {sys.version.split()[0]}")
    print(f"[A] 库文件路径: {finvizfinance.__file__}")
    print(f"[A] 库文档/主页: https://github.com/lit26/finvizfinance")
    return ver

# ===================== [B] 过滤选项枚举 =====================
def check_filter_options():
    from finvizfinance.screener.overview import Overview
    f = Overview()
    print("\n[B] 检查库内置过滤选项是否仍包含硬编码值...")
    try:
        get_filters = getattr(f, "get_filters", None)
        if not callable(get_filters):
            # 兜底：尝试从类上找
            get_filters = getattr(Overview, "get_filters", None)
        if callable(get_filters):
            all_opts = get_filters()
            mcap_opts = all_opts.get("Market Cap.", {}) if isinstance(all_opts, dict) else {}
            print(f"[B] 内置选项组: {list(all_opts.keys()) if isinstance(all_opts, dict) else type(all_opts)}")
            print(f"[B] 'Market Cap.' 可选值: {mcap_opts}")
            values = mcap_opts.values() if isinstance(mcap_opts, dict) else mcap_opts
            if mcap_opts and '+Large (over $10bln)' not in values:
                print("  ⚠️⚠️ 硬编码 '+Large (over $10bln)' 已不在选项中! 过滤条件可能失效/改名!")
            else:
                print("  ✅ '+Large (over $10bln)' 仍在选项中")
        else:
            print("[B] 该版本库无 get_filters() 方法，跳过选项枚举")
    except Exception as e:
        print(f"[B] 枚举过滤选项失败: {type(e).__name__}: {e}")

# ===================== [C][D] 实际筛选对比 =====================
def run_screener(filters_dict, label, f=None):
    from finvizfinance.screener.overview import Overview
    if f is None:
        f = Overview()
    print(f"\n[{label}] 过滤条件: {filters_dict}")
    t0 = time.time()
    try:
        f.set_filter(filters_dict=filters_dict)
        df = f.screener_view()
        elapsed = time.time() - t0
        if df is None or df.empty:
            print(f"[{label}] ⚠️ 返回空/None! (耗时 {elapsed:.1f}s)")
            return None
        print(f"[{label}] 返回 {len(df)} 只 (耗时 {elapsed:.1f}s)")
        if label == "C":
            print(f"[C] 列名: {list(df.columns)}")
            if "Ticker" in df.columns:
                print(f"[C] Ticker 前10只: {df['Ticker'].astype(str).tolist()[:10]}")
            if 'Industry' in df.columns:
                print(f"[C] 行业分布 Top10:\n{df['Industry'].value_counts().head(10).to_string()}")
        return df
    except Exception as e:
        print(f"[{label}] ❌ screener_view 报错: {type(e).__name__}: {e}")
        return None

# ===================== [E] yfinance 抽样校验 =====================
def sample_yfinance_check(df_full):
    print("\n[E] yfinance fast_info 抽样校验（验证市值二次校验环节）...")
    try:
        import yfinance as yf
    except Exception as e:
        print(f"[E] 导入 yfinance 失败: {type(e).__name__}: {e}")
        return
    if df_full is not None and not df_full.empty and 'Ticker' in df_full.columns:
        sample = df_full['Ticker'].astype(str).tolist()[:5]
    else:
        sample = ['AAPL', 'MSFT', 'NVDA', 'AMZN', 'GOOGL']
    ok = fail = 0
    for tk in sample:
        try:
            info = yf.Ticker(tk).fast_info
            mc = info.get('marketCap', 0)
            ok += 1
            print(f"  ✅ {tk}: marketCap = {mc / 1e6:.0f}M")
        except Exception as e:
            fail += 1
            print(f"  ❌ {tk}: {type(e).__name__}: {e}")
        time.sleep(0.2)
    print(f"[E] 成功率: {ok}/{ok + fail}")
    if fail:
        print("  ⚠️ 注意: yfinance 可能被限流。项目已修复: 市值过滤改用 finviz 自带 Market Cap 列，不再依赖 yfinance。")

# ===================== 主流程 =====================
def main():
    print("=" * 60)
    print("SEPA 初筛故障诊断开始")
    print("=" * 60)

    diagnose_library()

    filters_full = {
        'Market Cap.': '+Large (over $10bln)',
        'Price': 'Over $5',
        'Average Volume': 'Over 2M',
        'Current Volume': 'Over 2M',
        'IPO Date': 'More than a year ago',
        '200-Day Simple Moving Average': 'Price above SMA200',
    }
    filters_no_mcap = {k: v for k, v in filters_full.items() if k != 'Market Cap.'}

    check_filter_options()

    df_full = run_screener(filters_full, "C")
    df_nomcap = run_screener(filters_no_mcap, "D")

    if df_full is not None and df_nomcap is not None:
        print(f"\n[对比] 带市值过滤 {len(df_full)} 只 vs 不带市值过滤 {len(df_nomcap)} 只 "
              f"(差 {len(df_nomcap) - len(df_full)} 只)")
    elif df_full is None and df_nomcap is not None:
        print("\n[对比] ⚠️ 带市值过滤失败，但去掉后成功 → 高度怀疑 Market Cap 过滤项失效/被改版影响")

    sample_yfinance_check(df_full)

    print("\n" + "=" * 60)
    print("诊断结束")
    print("=" * 60)


if __name__ == "__main__":
    main()
