"""
策略生成模块 - 结合技术面数据校验LLM给出的建议，并计算仓位建议
增加：
- 更鲁棒的股票代码规范化和重试
- 生成最近7天K线图（Base64 PNG），供邮件展示
"""
import akshare as ak
import pandas as pd
import sys
import os
import time
import io
import base64
import matplotlib.pyplot as plt
import mplfinance as mpf

# 添加 src 到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import logger

MAX_FETCH_RETRIES = 3
FETCH_RETRY_DELAY = 1.5


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
    """尝试多种 code 形式去获取 ak 的 K 线（重试），返回 df 或 None"""
    for attempt in range(MAX_FETCH_RETRIES):
        for variant in code_variants:
            try:
                logger.debug(f"尝试获取行情：{variant} (尝试 {attempt + 1}/{MAX_FETCH_RETRIES})")
                df = ak.stock_zh_a_hist(symbol=variant, period="daily", adjust="qfq")
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

        # 生成候选 code 形式：原始、无后缀、带交易所后缀（按首位判断）
        variants = [stock_code, base]
        # 如果没有后缀，根据首位猜交易所���6 -> SH，其他 -> SZ
        if not any("." in stock_code for _ in [stock_code]):
            if base.startswith("6"):
                variants.append(base + ".SH")
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

        result = {
            "latest_close": round(float(latest["收盘"]), 2),
            "ma5": round(float(ma5), 2) if pd.notna(ma5) else None,
            "ma10": round(float(ma10), 2) if pd.notna(ma10) else None,
            "volume_ratio": round(float(latest["成交量"]) / avg_volume, 2) if avg_volume else None,
            "above_ma5": float(latest["收盘"]) > ma5 if pd.notna(ma5) else None,
            "above_ma10": float(latest["收盘"]) > ma10 if pd.notna(ma10) else None,
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
    try:
        if df is None or df.empty:
            return ""
        df_plot = df.tail(days).copy()
        # mplfinance 需要索引为 DatetimeIndex
        # akshare 返回的 DataFrame 可能已经以日期为索引或包含 '日期' 列
        if "日期" in df_plot.columns:
            df_plot.index = pd.to_datetime(df_plot["日期"]) 
        else:
            df_plot.index = pd.to_datetime(df_plot.index)
        # 规范列：Open/High/Low/Close/Volume
        df_plot = df_plot.rename(columns={"开盘": "Open", "最高": "High", "最低": "Low", "收盘": "Close", "成交量": "Volume"})
        fig_bytes = io.BytesIO()
        mpf.plot(df_plot, type="candle", style="charles", volume=False, mav=(5,10), savefig=fig_bytes, figsize=(4, 2.2), tight_layout=True)
        fig_bytes.seek(0)
        img_b64 = base64.b64encode(fig_bytes.read()).decode("utf-8")
        return img_b64
    except Exception as e:
        logger.debug(f"K线图生成失败: {e}")
        return ""


def risk_check(stock: dict, technical: dict) -> list:
    """风险检查，返回额外风险提示列表"""
    warnings = []

    if not technical:
        warnings.append("技术数据缺失，无法进行交叉验证")
        return warnings

    if technical.get("volume_ratio") and technical["volume_ratio"] > 3:
        warnings.append("成交量异常放大，警惕短期过热回调")

    if technical.get("above_ma5") is False:
        warnings.append("股价处于5日均线下方，短线趋势偏弱")

    if stock.get("risk_level") == "高":
        warnings.append("LLM标注为高风险标的，建议降低仓位")

    return warnings


def calculate_position_suggestion(risk_level: str) -> str:
    """根据风险等级给出仓位建议"""
    mapping = {
        "低": "单只不超过总仓位 15%",
        "中": "单只不超过总仓位 10%",
        "高": "单只不超过总仓位 5%，或观望不参与",
    }
    return mapping.get(risk_level, "单只不超过总仓位 10%")


def enrich_stock_strategies(stocks: list) -> list:
    """为每只候选股票补充技术面数据、仓位建议和7天K线图（Base64）"""
    enriched = []

    for stock in stocks:
        try:
            code = stock.get("stock_code", "")
            if not code:
                logger.warning("股票代码缺失，跳过此股票")
                continue

            logger.debug(f"处理股票: {stock.get('stock_name')} ({code})")

            technical = get_stock_technical_snapshot(code)
            warnings = risk_check(stock, technical)

            # 生成 k 线图 base64（若 technical 包含 _raw_df）
            kline_b64 = ""
            raw_df = technical.pop("_raw_df", None)
            if raw_df is not None and not raw_df.empty:
                kline_b64 = _generate_kline_base64(raw_df, days=7)

            stock["technical_snapshot"] = technical
            stock["extra_warnings"] = warnings
            stock["position_suggestion"] = calculate_position_suggestion(
                stock.get("risk_level", "中")
            )
            stock["kline_7d_base64"] = kline_b64  # 可能为空字符串

            enriched.append(stock)

        except Exception as e:
            logger.warning(f"处理股票 {stock.get('stock_name')} 失败: {e}")
            continue

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

        if change_pct < -3:
            logger.warning(f"⚠️  大盘跌幅达 {change_pct}%，触发熔断机制")
            return True

        return False

    except Exception as e:
        logger.warning(f"熔断检查异常: {e}")
        return False
