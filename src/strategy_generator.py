"""
策略生成模块 - 结合技术面数据校验LLM给出的建议，并计算仓位建议
"""
import akshare as ak
import pandas as pd
import sys
import os

# 添加 src 到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import logger


def get_stock_technical_snapshot(stock_code: str) -> dict:
    """获取个股近期技术面快照，用于交叉验证"""
    try:
        logger.debug(f"获取 {stock_code} 的技术面数据...")
        df = ak.stock_zh_a_hist(symbol=stock_code, period="daily", adjust="qfq")
        
        if df.empty or len(df) < 10:
            logger.warning(f"{stock_code} 数据不足")
            return {}
        
        df = df.tail(20)
        latest = df.iloc[-1]
        ma5 = df["收盘"].rolling(5).mean().iloc[-1]
        ma10 = df["收盘"].rolling(10).mean().iloc[-1]
        avg_volume = df["成交量"].mean()

        result = {
            "latest_close": round(latest["收盘"], 2),
            "ma5": round(ma5, 2),
            "ma10": round(ma10, 2),
            "volume_ratio": round(latest["成交量"] / avg_volume, 2) if avg_volume else None,
            "above_ma5": latest["收盘"] > ma5,
            "above_ma10": latest["收盘"] > ma10,
        }
        logger.debug(f"✓ {stock_code} 技术面数据获取成功")
        return result
        
    except Exception as e:
        logger.warning(f"{stock_code} 技术面数据获取失败: {e}")
        return {}


def risk_check(stock: dict, technical: dict) -> list:
    """风险检查，返回额外风险提示列表"""
    warnings = []
    
    if not technical:
        warnings.append("技术数据缺失，无法进行交叉验证")
        return warnings
    
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
