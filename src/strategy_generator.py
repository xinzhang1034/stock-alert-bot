"""
策略生成模块 - 结合技术面数据校验LLM给出的建议，并计算仓位建议
"""
import akshare as ak
import pandas as pd


def get_stock_technical_snapshot(stock_code: str) -> dict:
    """获取个股近期技术面快照，用于交叉验证"""
    try:
        df = ak.stock_zh_a_hist(symbol=stock_code, period="daily", adjust="qfq")
        df = df.tail(20)
        latest = df.iloc[-1]
        ma5 = df["收盘"].rolling(5).mean().iloc[-1]
        ma10 = df["收盘"].rolling(10).mean().iloc[-1]
        avg_volume = df["成交量"].mean()

        return {
            "latest_close": latest["收盘"],
            "ma5": round(ma5, 2),
            "ma10": round(ma10, 2),
            "volume_ratio": round(latest["成交量"] / avg_volume, 2) if avg_volume else None,
            "above_ma5": latest["收盘"] > ma5,
            "above_ma10": latest["收盘"] > ma10,
        }
    except Exception as e:
        print(f"{stock_code} 技术面数据获取失败: {e}")
        return {}


def risk_check(stock: dict, technical: dict) -> list:
    """风险检查，返回额外风险提示列表"""
    warnings = []
    if technical.get("volume_ratio") and technical["volume_ratio"] > 3:
        warnings.append("成交量异常放大，警惕短期过热回调")
    if not technical.get("above_ma5"):
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
    """为每只候选股票补充技术面数据和仓位建议"""
    enriched = []
    for stock in stocks:
        code = stock.get("stock_code", "")
        technical = get_stock_technical_snapshot(code)
        warnings = risk_check(stock, technical)
        stock["technical_snapshot"] = technical
        stock["extra_warnings"] = warnings
        stock["position_suggestion"] = calculate_position_suggestion(stock.get("risk_level", "中"))
        enriched.append(stock)
    return enriched


def market_circuit_breaker_check(index_df) -> bool:
    """熔断检查：如果大盘当日跌幅超过阈值，返回True表示应暂停推荐"""
    try:
        sh_index = index_df[index_df["名称"].str.contains("上证")]
        if not sh_index.empty:
            change_pct = sh_index.iloc[0]["涨跌幅"]
            if change_pct < -3:
                return True
        return False
    except Exception:
        return False
