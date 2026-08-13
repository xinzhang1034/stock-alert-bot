"""
策略生成模块 - 结合技术面数据校验LLM给出的建议，并计算仓位建议
"""
import akshare as ak
import pandas as pd
import sys
import os
from datetime import datetime

# 添加 src 到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import logger


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """简单 RSI 实现（不依赖 talib）返回 pd.Series"""
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    ma_up = up.rolling(window=period, min_periods=period).mean()
    ma_down = down.rolling(window=period, min_periods=period).mean()
    rs = ma_up / ma_down
    rsi = 100 - (100 / (1 + rs))
    return rsi


def get_stock_technical_snapshot(stock_code: str) -> dict:
    """获取个股近期技术面快照，用于交叉验证。

    返回字典。若抓取失败，返回包含键 `_error` 的字典，便于上层展示具体原因。
    可能的错误码：no_data, missing_columns, rate_limited, fetch_error
    成功时包含字段：latest_close, ma5, ma10, volume_ratio, above_ma5, above_ma10, rsi
    """
    try:
        logger.debug(f"获取 {stock_code} 的技术面数据...")
        # akshare 的日期参数格式为 YYYYMMDD，可不传
        df = ak.stock_zh_a_hist(symbol=stock_code, period="daily", adjust="qfq")

        if df is None or df.empty:
            logger.warning(f"{stock_code} 数据为空")
            return {"_error": "no_data"}

        if len(df) < 10:
            logger.warning(f"{stock_code} 数据不足 (rows={len(df)})")
            return {"_error": "no_data"}

        # 容错列名：支持中文列名（开盘/最高/最低/收盘/成交量）或英文
        cols = set(df.columns)
        required = {"开盘", "最高", "最低", "收盘", "成交量"}
        if not required.intersection(cols):
            logger.warning(f"{stock_code} 缺少必要列: {cols}")
            return {"_error": "missing_columns"}

        # 取最近 60 天用于指标计算，然后取最后 20 行为快照
        df = df.tail(60)
        # 确保使用中文列名路径
        close_col = "收盘" if "收盘" in df.columns else "close"
        vol_col = "成交量" if "成交量" in df.columns else "volume"

        df = df.tail(20)
        latest = df.iloc[-1]
        ma5 = df[close_col].rolling(5).mean().iloc[-1]
        ma10 = df[close_col].rolling(10).mean().iloc[-1]
        avg_volume = df[vol_col].mean() if vol_col in df.columns else None

        # RSI 14
        full_close = df[close_col]
        rsi_series = compute_rsi(full_close, period=14)
        latest_rsi = float(rsi_series.iloc[-1]) if not rsi_series.isnull().all() else None

        result = {
            "latest_close": round(float(latest[close_col]), 2),
            "ma5": round(float(ma5), 2) if pd.notna(ma5) else None,
            "ma10": round(float(ma10), 2) if pd.notna(ma10) else None,
            "volume_ratio": round(float(latest[vol_col]) / float(avg_volume), 2) if avg_volume and avg_volume != 0 else None,
            "above_ma5": float(latest[close_col]) > float(ma5) if pd.notna(ma5) else None,
            "above_ma10": float(latest[close_col]) > float(ma10) if pd.notna(ma10) else None,
            "rsi": round(latest_rsi, 2) if latest_rsi is not None else None,
        }
        logger.debug(f"✓ {stock_code} 技术面数据获取成功")
        return result

    except Exception as e:
        msg = str(e)
        if "429" in msg or "rate" in msg.lower():
            logger.warning(f"{stock_code} 可能被限流: {msg}")
            return {"_error": "rate_limited"}
        logger.warning(f"{stock_code} 技术面数据获取失败: {msg}")
        return {"_error": "fetch_error"}


def risk_check(stock: dict, technical: dict) -> list:
    """风险检查，返回额外风险提示列表"""
    warnings = []

    if not technical:
        warnings.append("技术数据缺失，无法进行交叉验证")
        return warnings

    if technical.get("_error"):
        code = technical.get("_error")
        human = {
            "no_data": "无历史数据/样本不足（停牌或数据不可用）",
            "missing_columns": "数据字段缺失，无法计算技术指标",
            "rate_limited": "数据源限流/访问失败",
            "fetch_error": "抓取数据异常"
        }.get(code, f"技术数据错误：{code}")
        warnings.append(human)
        return warnings

    # RSI 过热
    if technical.get("rsi") and technical["rsi"] > 70:
        warnings.append(f"RSI 指标偏高（{technical['rsi']}），短期可能过热")

    if technical.get("volume_ratio") and technical["volume_ratio"] > 3:
        warnings.append("成交量异常放大，警惕短期过热回调")

    if technical.get("above_ma5") is not None and not technical.get("above_ma5"):
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
    """为每只候选股票补充技术面数据、仓位建议并设置 recommend_price（用于复盘）"""
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

            stock["technical_snapshot"] = technical
            stock["extra_warnings"] = warnings
            stock["position_suggestion"] = calculate_position_suggestion(
                stock.get("risk_level", "中")
            )

            # 若 LLM 未返回推荐价，使用技术快照中的最新收盘作为推荐价（便于复盘）
            if not stock.get("recommend_price"):
                rp = technical.get("latest_close") if technical and not technical.get("_error") else None
                stock["recommend_price"] = rp

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

        sh_index = index_df[index_df.get("名称", "").str.contains("上证", na=False)] if "名称" in index_df.columns else index_df

        if sh_index is None or sh_index.empty:
            logger.warning("未找到上证指数数据")
            return False

        change_pct = float(sh_index.iloc[0].get("涨跌幅", 0))
        logger.debug(f"上证指数涨跌幅: {change_pct}%")

        if change_pct < -3:
            logger.warning(f"⚠️  大盘跌幅达 {change_pct}%，触发熔断机制")
            return True

        return False

    except Exception as e:
        logger.warning(f"熔断检查异常: {e}")
        return False
