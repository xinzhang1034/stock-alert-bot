"""
主流程入口 - 带完整错误处理和日志
"""
import json
import os
import sys
from datetime import datetime, timedelta

# 添加 src 到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config, logger
from news_fetcher import collect_all_data
from llm_analyzer import analyze_and_select_stocks
from strategy_generator import enrich_stock_strategies, market_circuit_breaker_check
from email_sender import build_html_report, send_email

HISTORY_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "history.json")


def load_history() -> list:
    """加载历史推荐记录"""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"加载历史记录失败: {e}")
            return []
    return []


def save_history(history: list):
    """保存历史推荐记录"""
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        logger.info(f"历史记录已保存，当前记录数: {len(history)}")
    except Exception as e:
        logger.error(f"保存历史记录失败: {e}")


def summarize_dataframe(df, columns, max_rows=15):
    """总结数据框"""
    if df is None or df.empty:
        return "暂无数据"
    cols = [c for c in columns if c in df.columns]
    return df[cols].head(max_rows).to_string(index=False)


def get_yesterday_review(history: list) -> str:
    """获取昨日推荐复盘"""
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    for entry in history:
        if entry["date"] == yesterday:
            names = [s["stock_name"] for s in entry.get("stocks", [])]
            return f"昨日({yesterday})推荐标的: {', '.join(names) if names else '无'}（建议自行核对实际涨跌情况）"
    return ""


def run():
    """主流程"""
    try:
        logger.info("=" * 60)
        logger.info("开始执行每日选股流程...")
        logger.info("=" * 60)
        
        # 1. 收集数据
        logger.info("步骤 1/5: 收集市场数据...")
        raw_data = collect_all_data()
        logger.info(f"✓ 新闻数据: {len(raw_data['cls_news'])} 条")
        logger.info(f"✓ 涨停股池: {len(raw_data['limit_up'])} 只")
        logger.info(f"✓ 龙虎榜: {len(raw_data['dragon_tiger'])} 条")
        logger.info(f"✓ 大盘指数: {len(raw_data['index'])} 行")
        
        # 2. 熔断检查
        logger.info("步骤 2/5: 执行市场熔断检查...")
        circuit_breaker = market_circuit_breaker_check(raw_data["index"])
        if circuit_breaker:
            logger.warning("⚠️  触发熔断机制！大盘波动过大，今日不建议操作")
        else:
            logger.info("✓ 市场正常，继续处理")
        
        # 3. LLM 选股
        stocks = []
        if not circuit_breaker:
            logger.info("步骤 3/5: 准备数据摘要...")
            news_summary = summarize_dataframe(raw_data["cls_news"], ["标题", "内容"])
            limit_up_summary = summarize_dataframe(raw_data["limit_up"], ["名称", "代码", "连板数"])
            dragon_tiger_summary = summarize_dataframe(raw_data["dragon_tiger"], ["名称", "代码", "净买入额"])
            
            logger.info("步骤 4/5: 调用 LLM 进行选股分析...")
            stocks = analyze_and_select_stocks(news_summary, limit_up_summary, dragon_tiger_summary)
            logger.info(f"✓ LLM 推荐 {len(stocks)} 只股票")
            
            if stocks:
                for i, stock in enumerate(stocks, 1):
                    logger.info(f"  {i}. {stock.get('stock_name')} ({stock.get('stock_code')}): {stock.get('reason')}")
            
            # 4. 技术面验证
            logger.info("步骤 5/5: 进行技术面验证和风险评估...")
            stocks = enrich_stock_strategies(stocks)
            logger.info(f"✓ 已补充技术指标和风险评估")
        else:
            logger.info("步骤 3-5: 跳过（已触发熔断）")
        
        # 5. 加载历史记录
        logger.info("加载历史推荐记录...")
        history = load_history()
        yesterday_review = get_yesterday_review(history)
        
        # 6. 生成并发送邮件
        logger.info("生成 HTML 报告...")
        html, images = build_html_report(stocks, circuit_breaker, yesterday_review)
        
        logger.info("发送邮件...")
        send_email(html, images)
        
        # 7. 保存历史记录
        logger.info("更新历史记录...")
        today = datetime.now().strftime("%Y-%m-%d")
        history = [h for h in history if h["date"] != today]
        history.append({"date": today, "stocks": stocks})
        history = history[-30:]  # 保留最近 30 天
        save_history(history)
        
        logger.info("=" * 60)
        logger.info("✓ 流程执行完毕！")
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error("=" * 60)
        logger.error(f"❌ 流程执行失败: {e}", exc_info=True)
        logger.error("=" * 60)
        sys.exit(1)


if __name__ == "__main__":
    # 验证配置
    is_valid, errors = Config.validate()
    if not is_valid:
        logger.error("配置检查失败:")
        for error in errors:
            logger.error(f"  ❌ {error}")
        logger.error("请参考 .env.example 文件配置所需的环境变量")
        sys.exit(1)
    
    logger.info("配置检查通过 ✓")
    run()
