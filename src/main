"""
主流程入口
"""
import json
import os
from datetime import datetime, timedelta

from news_fetcher import collect_all_data
from llm_analyzer import analyze_and_select_stocks
from strategy_generator import enrich_stock_strategies, market_circuit_breaker_check
from email_sender import build_html_report, send_email

HISTORY_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "history.json")


def load_history() -> list:
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_history(history: list):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def summarize_dataframe(df, columns, max_rows=15):
    if df is None or df.empty:
        return "暂无数据"
    cols = [c for c in columns if c in df.columns]
    return df[cols].head(max_rows).to_string(index=False)


def get_yesterday_review(history: list) -> str:
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    for entry in history:
        if entry["date"] == yesterday:
            names = [s["stock_name"] for s in entry.get("stocks", [])]
            return f"昨日({yesterday})推荐标的: {', '.join(names) if names else '无'}（建议自行核对实际涨跌情况）"
    return ""


def run():
    print("开始执行每日选股流程...")

    raw_data = collect_all_data()
    circuit_breaker = market_circuit_breaker_check(raw_data["index"])

    stocks = []
    if not circuit_breaker:
        news_summary = summarize_dataframe(raw_data["cls_news"], ["标题", "内容"])
        limit_up_summary = summarize_dataframe(raw_data["limit_up"], ["名称", "代码", "连板数"])
        dragon_tiger_summary = summarize_dataframe(raw_data["dragon_tiger"], ["名称", "代码", "净买入额"])

        stocks = analyze_and_select_stocks(news_summary, limit_up_summary, dragon_tiger_summary)
        stocks = enrich_stock_strategies(stocks)

    history = load_history()
    yesterday_review = get_yesterday_review(history)

    html = build_html_report(stocks, circuit_breaker, yesterday_review)
    send_email(html)

    today = datetime.now().strftime("%Y-%m-%d")
    history = [h for h in history if h["date"] != today]
    history.append({"date": today, "stocks": stocks})
    history = history[-30:]
    save_history(history)

    print("流程执行完毕")


if __name__ == "__main__":
    run()
