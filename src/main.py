"""
主流程入口 - 带完整错误处理和日志
已增强：
- 保存推荐时记录 saved_recommendation_close 字段
- get_yesterday_review 返回 HTML 复盘（对比推荐价 vs 次日收盘，如果 next_day_close 已被回填）
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
    """获取昨日推荐复盘（返回 HTML）

    逻辑：查找 history 中昨日的推荐记录，列出每只标的的
    - 推荐保存价 saved_recommendation_close
    - next_day_close（如果已有则显示并计算涨跌%）
    """
    if not history:
        return ""

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    for entry in history:
        if entry.get("date") == yesterday:
            stocks = entry.get("stocks", [])
            if not stocks:
                return f"<div>昨日({yesterday})无推荐标的</div>"

            parts = [f"<div>昨日({yesterday}) 推荐 {len(stocks)} 只：</div>", "<ul>"]
            for s in stocks:
                name = s.get("stock_name", "N/A")
                code = s.get("stock_code", "N/A")
                saved = s.get("saved_recommendation_close")
                next_close = s.get("next_day_close")

                if saved is None:
                    parts.append(f"<li>{name} ({code}) - 推荐价不可用，无法复盘</li>")
                elif next_close is None:
                    parts.append(f"<li>{name} ({code}) - 推荐价 {saved}，未回填次日收盘</li>")
                else:
                    try:
                        pct = round((float(next_close) - float(saved)) / float(saved) * 100, 2)
                        sign = "↑" if pct > 0 else ("↓" if pct < 0 else "→")
                        color = "#51cf66" if pct > 0 else ("#ff6b6b" if pct < 0 else "#999")
                        parts.append(
                            f"<li>{name} ({code}) - 推荐价 {saved}，次日收盘 {next_close}，变化 <span style='color:{color};font-weight:bold'>{sign} {pct}%</span></li>"
                        )
                    except Exception:
                        parts.append(f"<li>{name} ({code}) - 推荐价 {saved}，次日收盘 {next_close}，无法计算涨跌</li>")

            parts.append("</ul>")
            return "".join(parts)

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
        logger.info(f"✓ 新闻数据: {len(raw_data.get('cls_news') or [])} 条")
        logger.info(f"✓ 涨停股池: {len(raw_data.get('limit_up') or [])} 只")
        logger.info(f"✓ 龙虎榜: {len(raw_data.get('dragon_tiger') or [])} 条")
        logger.info(f"✓ 大盘指数: {len(raw_data.get('index') or [])} 行")
        
        # 2. 熔断检查
        logger.info("步骤 2/5: 执行市场熔断检查...")
        circuit_breaker = market_circuit_breaker_check(raw_data.get("index"))
        if circuit_breaker:
            logger.warning("⚠️  触发熔断机制！大盘波动过大，今日不建议操作")
        else:
            logger.info("✓ 市场正常，继续处理")
        
        # 3. LLM 选股
        stocks = []
        if not circuit_breaker:
            logger.info("步骤 3/5: 准备数据摘要...")
            news_summary = summarize_dataframe(raw_data.get('cls_news'), ["标题", "内容"])
            limit_up_summary = summarize_dataframe(raw_data.get('limit_up'), ["名称", "代码", "连板数"])
            dragon_tiger_summary = summarize_dataframe(raw_data.get('dragon_tiger'), ["名称", "代码", "净买入额"])
            
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
        html = build_html_report(stocks, circuit_breaker, yesterday_review)
        
        logger.info("发送邮件...")
        send_email(html)
        
        # 7. 保存历史记录（在保存时把推荐时的 latest_close 一并保存）
        logger.info("更新历史记录...")
        today = datetime.now().strftime("%Y-%m-%d")
        history = [h for h in history if h.get("date") != today]

        stocks_to_save = []
        for s in stocks:
            copy = dict(s)  # shallow copy
            ts = copy.get("technical_snapshot") or {}
            if ts:
                copy["saved_recommendation_close"] = ts.get("latest_close")
            # next_day_close 可能由后续脚本回填，这里保留如果已有的值
            copy["next_day_close"] = copy.get("next_day_close")
            stocks_to_save.append(copy)

        history.append({"date": today, "stocks": stocks_to_save})
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
