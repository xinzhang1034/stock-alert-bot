import json
from datetime import datetime, timedelta
from pathlib import Path
import akshare as ak

ROOT = Path(__file__).resolve().parent.parent
HISTORY_FILE = ROOT / "data" / "history.json"


def load_history():
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def generate_recap_for_date(date_str: str = None, lookahead_days: int = 1):
    """读取指定日期的历史推荐并生成复盘结果。

    返回 HTML 表格字符串，便于直接嵌入邮件。若无数据返回空字符串。
    """
    if date_str is None:
        date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    history = load_history()
    rows = []

    for entry in history:
        if entry.get("date") == date_str:
            for s in entry.get("stocks", []):
                code = s.get("stock_code")
                recommend_price = s.get("recommend_price")
                # 抓取后续行情
                start = datetime.strptime(date_str, "%Y-%m-%d")
                end = start + timedelta(days=lookahead_days + 3)
                try:
                    df = ak.stock_zh_a_hist(symbol=code, period="daily", adjust="qfq",
                                             start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
                except Exception:
                    df = None
                if df is None or df.empty:
                    rows.append({
                        "stock_code": code,
                        "stock_name": s.get("stock_name"),
                        "recommend_price": recommend_price,
                        "close_next": None,
                        "return_pct": None,
                        "status": "no_data"
                    })
                    continue
                # 选次日收盘
                close_next = None
                if len(df) > 1:
                    close_next = df.iloc[1]["收盘"] if "收盘" in df.columns else df.iloc[1].get("close")
                else:
                    close_next = df.iloc[0]["收盘"] if "收盘" in df.columns else df.iloc[0].get("close")

                ret = None
                if recommend_price and close_next is not None:
                    try:
                        ret = round((float(close_next) - float(recommend_price)) / float(recommend_price) * 100, 2)
                    except Exception:
                        ret = None

                rows.append({
                    "stock_code": code,
                    "stock_name": s.get("stock_name"),
                    "recommend_price": recommend_price,
                    "close_next": close_next,
                    "return_pct": ret,
                    "status": "ok"
                })

    if not rows:
        return ""

    # 生成简单 HTML 表格
    html = ['<table style="width:100%;border-collapse:collapse;font-size:13px">']
    html.append('<thead><tr style="background:#f0f0f0"><th style="padding:8px;border:1px solid #e0e0e0;text-align:left">代码</th><th style="padding:8px;border:1px solid #e0e0e0;text-align:left">名称</th><th style="padding:8px;border:1px solid #e0e0e0;text-align:right">推荐价</th><th style="padding:8px;border:1px solid #e0e0e0;text-align:right">次日收盘</th><th style="padding:8px;border:1px solid #e0e0e0;text-align:right">收益(%)</th></tr></thead>')
    html.append('<tbody>')
    for r in rows:
        ret_display = (f"{r['return_pct']}%" if r['return_pct'] is not None else "-")
        close_disp = (f"{r['close_next']:.2f}" if isinstance(r['close_next'], (int, float)) else (r['close_next'] or '-'))
        rec_disp = (f"{r['recommend_price']:.2f}" if isinstance(r['recommend_price'], (int, float)) else (r['recommend_price'] or '-'))
        html.append(f"<tr><td style=\"padding:8px;border:1px solid #e0e0e0\">{r['stock_code']}</td><td style=\"padding:8px;border:1px solid #e0e0e0\">{r['stock_name']}</td><td style=\"padding:8px;border:1px solid #e0e0e0;text-align:right\">{rec_disp}</td><td style=\"padding:8px;border:1px solid #e0e0e0;text-align:right\">{close_disp}</td><td style=\"padding:8px;border:1px solid #e0e0e0;text-align:right\">{ret_display}</td></tr>")
    html.append('</tbody></table>')
    return ''.join(html)
