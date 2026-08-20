"""
策略生成模块 - 结合技术面数据校验LLM给出的建议，并计算仓位建议
增加：
- 更鲁棒的股票代码规范化和重试
- 生成最近7天K线图（Base64 PNG），供邮件展示

修复记录：
- _generate_kline_base64：mpf.plot 之后补上 plt.close()，避免循环处理多只股票时
  matplotlib figure 不断累积导致内存泄漏
- _generate_kline_base64：绘图窗口长度改为至少覆盖 mav 最长周期+1，避免 days=7 天
  数据画不出 mav=(5,10) 里的10日均线而报错（原来该异常被静默吞掉，日志级别也从
  debug 提到 warning，方便定位）
- get_stock_technical_snapshot：volume_ratio 计算增加 pd.notna 判断，避免 avg_volume
  为 NaN 时把 NaN 混入结果字典
- _normalize_code 附近的交易所后缀猜测：补充北交所（43/83/87/88 开头）分支，并把
  绕弯的 `any(... for _ in [x])` 写法简化成直接的 in 判断
- _try_fetch_hist：修正文档描述，耗尽重试后返回的是空 DataFrame 而非 None

功能优化（第二轮）：
- 新增 assess_effective_risk_level：技术面警告会实际影响仓位建议，不再只看 LLM 自报的风险等级
- risk_check 新增：均线死叉、ST/*ST 标的、涨幅追涨停、换手率过低 四类检查
- _try_fetch_hist 拉取行情时限定 start_date 时间窗口，不再每次都拉全量历史
- enrich_stock_strategies 改为线程池并发处理（用 map 保持输出顺序与输入一致）
- _generate_kline_base64 的 mplfinance 画图部分加锁，兼容并发调用
- 熔断跌幅阈值提取为常量 CIRCUIT_BREAKER_DROP_PCT
"""
import akshare as ak
import pandas as pd
import sys
import os
import time
import io
import base64
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import matplotlib.pyplot as plt
import mplfinance as mpf

# 添加 src 到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import logger

MAX_FETCH_RETRIES = 3
FETCH_RETRY_DELAY = 1.5
MA_WINDOWS = (5, 10)
LOOKBACK_DAYS = 60  # 拉取行情的自然日窗口，足够覆盖 20 个交易日，避免拉全量历史
RISK_LEVELS = ["低", "中", "高"]
HARD_RISK_MARKERS = ("ST", "死叉", "涨停")  # 命中即视为强风险信号，直接顶到最高仓位风险等级
MAX_WORKERS = 4  # enrich_stock_strategies 并发数，需兼顾行情接口限流
CIRCUIT_BREAKER_DROP_PCT = -3  # 大盘熔断跌幅阈值(%)
_PLOT_LOCK = threading.Lock()  # matplotlib pyplot 全局状态非线程安全，画图需要加锁


def _normalize_code(code: str) -> str:
    """把 LLM 返回的各种 code 格式规范化，主要去掉后缀或按规则添加交易所后缀以便尝试"""
    if not code:
        return ""
    c = code.strip()
    # 常见形式 '600000' / '600000.SH' / '600000.SZ' / '600000.SH' / '600000.SS' / '600000.SZ'
    if "." in c:
        c = c.split(".")[0]
    return c


def _try_fetch_hist(code_variants):
    """尝试多种 code 形式去获取 ak 的 K 线（重试），返回 df 或空 DataFrame（耗尽重试后不会返回 None）"""
    start_date = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    for attempt in range(MAX_FETCH_RETRIES):
        for variant in code_variants:
            try:
                logger.debug(f"尝试获取行情：{variant} (尝试 {attempt + 1}/{MAX_FETCH_RETRIES})")
                # 只拉最近窗口，避免每次都拉全量历史行情
                df = ak.stock_zh_a_hist(symbol=variant, period="daily", adjust="qfq", start_date=start_date)
                if df is not None and not df.empty:
                    return df
            except Exception as e:
                logger.debug(f"获取 {variant} 失败: {e}")
                continue
        time.sleep(FETCH_RETRY_DELAY)
    return pd.DataFrame()


def get_stock_technical_snapshot(stock_code: str) -> dict:
    """获取个股近期技术面快照，用于交叉验证；返回 technical dict（并不包含图片）"""
    try:
        logger.debug(f"获取 {stock_code} 的技术面数据...")
        base = _normalize_code(stock_code)
        if not base:
            logger.warning("股票代码规范化后为空")
            return {}

        # 生成候选 code 形式：原始、无后缀、带交易所后缀（按代码规则猜测）
        variants = [stock_code, base]
        if "." not in stock_code:
            if base.startswith("6"):
                variants.append(base + ".SH")
            elif base[:2] in ("43", "83", "87", "88"):
                variants.append(base + ".BJ")
            else:
                variants.append(base + ".SZ")

        df = _try_fetch_hist(variants)
        if df is None or df.empty or len(df) < 7:
            logger.warning(f"{stock_code} 数据不足或无法获取")
            return {}

        # 保证按日期升序，取最近 20 行作为计算依据
        df = df.tail(20)
        latest = df.iloc[-1]
        ma5 = df["收盘"].rolling(5).mean().iloc[-1]
        ma10 = df["收盘"].rolling(10).mean().iloc[-1]
        avg_volume = df["成交量"].mean()
        volume_ratio = (
            round(float(latest["成交量"]) / avg_volume, 2)
            if avg_volume and pd.notna(avg_volume)
            else None
        )
        # akshare 行情本身自带涨跌幅/换手率，直接复用，不用自己再算一遍
        change_pct = latest.get("涨跌幅")
        turnover_rate = latest.get("换手率")

        result = {
            "latest_close": round(float(latest["收盘"]), 2),
            "ma5": round(float(ma5), 2) if pd.notna(ma5) else None,
            "ma10": round(float(ma10), 2) if pd.notna(ma10) else None,
            "volume_ratio": volume_ratio,
            "above_ma5": float(latest["收盘"]) > ma5 if pd.notna(ma5) else None,
            "above_ma10": float(latest["收盘"]) > ma10 if pd.notna(ma10) else None,
            "change_pct": round(float(change_pct), 2) if change_pct is not None and pd.notna(change_pct) else None,
            "turnover_rate": round(float(turnover_rate), 2) if turnover_rate is not None and pd.notna(turnover_rate) else None,
            # 保留原始用于后续绘图
            "_raw_df": df,
        }
        logger.debug(f"✓ {stock_code} 技术面数据获取成功")
        return result

    except Exception as e:
        logger.warning(f"{stock_code} 技术面数据获取失败: {e}")
        return {}


def _generate_kline_base64(df: pd.DataFrame, days: int = 7) -> str:
    """用 mplfinance 绘制最近 days 天的 K 线并返回 base64 PNG"""
    fig = None
    try:
        if df is None or df.empty:
            return ""
        # 绘图窗口必须覆盖 mav 最长周期，否则 mplfinance 会因数据不足报错
        window = max(days, max(MA_WINDOWS) + 1)
        df_plot = df.tail(window).copy()
        # mplfinance 需要索引为 DatetimeIndex
        # akshare 返回的 DataFrame 可能已经以日期为索引或包含 '日期' 列
        if "日期" in df_plot.columns:
            df_plot.index = pd.to_datetime(df_plot["日期"])
        else:
            df_plot.index = pd.to_datetime(df_plot.index)
        # 规范列：Open/High/Low/Close/Volume
        df_plot = df_plot.rename(columns={"开盘": "Open", "最高": "High", "最低": "Low", "收盘": "Close", "成交量": "Volume"})
        fig_bytes = io.BytesIO()
        # mplfinance 依赖 pyplot 全局状态，并发线程下必须加锁串行，否则会画错/画坏图
        with _PLOT_LOCK:
            fig, _ = mpf.plot(
                df_plot, type="candle", style="charles", volume=False, mav=MA_WINDOWS,
                savefig=fig_bytes, figsize=(4, 2.2), tight_layout=True, returnfig=True,
            )
        fig_bytes.seek(0)
        img_b64 = base64.b64encode(fig_bytes.read()).decode("utf-8")
        return img_b64
    except Exception as e:
        logger.warning(f"K线图生成失败: {e}")
        return ""
    finally:
        # mplfinance 底层复用 pyplot 的全局 figure，不手动关闭会在循环调用时持续泄漏内存
        with _PLOT_LOCK:
            if fig is not None:
                plt.close(fig)
            else:
                plt.close("all")


def risk_check(stock: dict, technical: dict) -> list:
    """风险检查，返回额外风险提示列表"""
    warnings = []

    if "ST" in stock.get("stock_name", "").upper():
        warnings.append("标的名称命中 ST/*ST，存在退市或被特别处理的风险")

    if not technical:
        warnings.append("技术数据缺失，无法进行交叉验证")
        return warnings

    if technical.get("volume_ratio") and technical["volume_ratio"] > 3:
        warnings.append("成交量异常放大，警惕短期过热回调")

    if technical.get("above_ma5") is False:
        warnings.append("股价处于5日均线下方，短线趋势偏弱")

    ma5, ma10 = technical.get("ma5"), technical.get("ma10")
    if ma5 is not None and ma10 is not None and ma5 < ma10:
        warnings.append("5日均线下穿10日均线（死叉），短期趋势偏弱")

    if technical.get("change_pct") is not None and technical["change_pct"] >= 9.5:
        warnings.append("当日涨幅已接近或触及涨停，警惕追高风险")

    if technical.get("turnover_rate") is not None and technical["turnover_rate"] < 1:
        warnings.append("换手率过低，警惕流动性不足，建仓/退出可能较难")

    if stock.get("risk_level") == "高":
        warnings.append("LLM标注为高风险标的，建议降低仓位")

    return warnings


def assess_effective_risk_level(stock: dict, warnings: list) -> str:
    """结合 LLM 自报风险等级和技术面警告，得到实际用于仓位建议的风险等级（只会上调不会下调）"""
    base_level = stock.get("risk_level", "中")
    idx = RISK_LEVELS.index(base_level) if base_level in RISK_LEVELS else RISK_LEVELS.index("中")

    if any(marker in w for w in warnings for marker in HARD_RISK_MARKERS):
        idx = len(RISK_LEVELS) - 1
    else:
        # 每命中 2 条技术面警告，风险等级上调一档
        idx = min(idx + len(warnings) // 2, len(RISK_LEVELS) - 1)

    return RISK_LEVELS[idx]


def calculate_position_suggestion(risk_level: str) -> str:
    """根据风险等级给出仓位建议"""
    mapping = {
        "低": "单只不超过总仓位 15%",
        "中": "单只不超过总仓位 10%",
        "高": "单只不超过总仓位 5%，或观望不参与",
    }
    return mapping.get(risk_level, "单只不超过总仓位 10%")


def _enrich_single_stock(stock: dict):
    """处理单只股票的技术面补充，供并发调用；失败返回 None"""
    try:
        code = stock.get("stock_code", "")
        if not code:
            logger.warning("股票代码缺失，跳过此股票")
            return None

        logger.debug(f"处理股票: {stock.get('stock_name')} ({code})")

        technical = get_stock_technical_snapshot(code)
        warnings = risk_check(stock, technical)
        effective_risk_level = assess_effective_risk_level(stock, warnings)

        # 生成 k 线图 base64（若 technical 包含 _raw_df）
        kline_b64 = ""
        raw_df = technical.pop("_raw_df", None)
        if raw_df is not None and not raw_df.empty:
            kline_b64 = _generate_kline_base64(raw_df, days=7)

        stock["technical_snapshot"] = technical
        stock["extra_warnings"] = warnings
        stock["effective_risk_level"] = effective_risk_level
        stock["position_suggestion"] = calculate_position_suggestion(effective_risk_level)
        stock["kline_7d_base64"] = kline_b64  # 可能为空字符串

        return stock

    except Exception as e:
        logger.warning(f"处理股票 {stock.get('stock_name')} 失败: {e}")
        return None


def enrich_stock_strategies(stocks: list) -> list:
    """为每只候选股票补充技术面数据、仓位建议和7天K线图（Base64），线程池并发处理"""
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # 用 map 而不是 as_completed，保证输出顺序和输入 stocks 顺序一致
        results = list(executor.map(_enrich_single_stock, stocks))

    enriched = [r for r in results if r is not None]
    logger.info(f"✓ 成功处理 {len(enriched)} 只股票的策略补充")
    return enriched


def market_circuit_breaker_check(index_df) -> bool:
    """熔断检查：如果大盘当日跌幅超过阈值，返回True表示应暂停推荐"""
    try:
        if index_df is None or index_df.empty:
            logger.debug("大盘数据为空，无法进行熔断检查")
            return False

        sh_index = index_df[index_df["名称"].str.contains("上证", na=False)]

        if sh_index.empty:
            logger.warning("未找到上证指数数据")
            return False

        change_pct = sh_index.iloc[0]["涨跌幅"]
        logger.debug(f"上证指数涨跌幅: {change_pct}%")

        if change_pct < CIRCUIT_BREAKER_DROP_PCT:
            logger.warning(f"⚠️  大盘跌幅达 {change_pct}%，触发熔断机制")
            return True

        return False

    except Exception as e:
        logger.warning(f"熔断检查异常: {e}")
        return False
