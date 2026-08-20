"""
主流程入口 - 带完整错误处理和日志
已增强：
- 保存推荐时记录 saved_recommendation_close 字段
- get_yesterday_review 返回 HTML 复盘（对比推荐价 vs 次日收盘，如果 next_day_close 已被回填）
- 增加补充逻辑：当 LLM 返回少于目标数量时，使用涨停池/龙虎榜补充到目标数量
- 修复：避免对 pandas.DataFrame 使用布尔运算导致异常（使用 safe_len）

修复记录（本轮）：
- run()：邮件生成/发送失败原来会让整个 try 块直接跳到顶层 except 并 sys.exit(1)，
  导致后面"保存历史记录"完全被跳过——一旦邮件发送偶发失败（比如 SMTP 抖动），今天的
  推荐就彻底丢失，第二天的"昨日复盘"也没有数据可对比。改成给生成/发送邮件单独包一层
  try/except，失败只记日志，不影响历史记录照常保存
- Config.validate() 原来只在 `if __name__ == "__main__":` 里检查，如果这个模块被别的
  入口 import 后直接调用 run()（而不是作为脚本直接跑），配置校验会被跳过，配置错误会
  一路下沉到 call_llm 内部才报，还会被 analyze_and_select_stocks 的 except 吞掉，
  表现为"今天莫名其妙 0 只推荐"且日志里看不出真正原因。改为校验放进 run() 内部，
  不管怎么调用都会先检查
- get_yesterday_review：原来严格按自然日"昨天"的日期字符串去匹配历史记录，如果昨天
  是周末/脚本没跑，就会一直找不到匹配、复盘永远是空的。改成取历史记录里"今天之前
  最近的一条"，不再要求日期严格等于自然日昨天
- _build_stock_from_limit_up_row：候选列名列表里 '代码(证券代码)' 被复制粘贴了两次
  （其中一个候选列名实际上没生效），改成统一的 _first_present() 辅助函数，并换成更
  常见的候选列名；同时把占位字段从字符串 'N/A' 改成 None（LLM 分析模块那边校验
  数值字段时 'N/A' 会被当成非法值处理，None 更符合"确实没有这个值"的语义），补充项
  统一标记 is_supplement=True，risk_level 默认从"中"改成"高"（这些标的没有经过 LLM
  的基本面/消息面筛选，只是单纯出现在涨停池/龙虎榜里，按更保守的风险等级处理更合理）
- summarize_dataframe：如果请求的列名一个都不在 df 里，原来会对空列 DataFrame 调用
  to_string() 输出一段看似有数据实则没内容的字符串，改为直接返回"暂无数据"提示

未改动但值得注意的一点：build_prompt 明确要求 LLM"避免只因为某股连续涨停/连板就推荐"，
但 supplement_stocks 在 LLM 数量不足时，恰恰是直接从涨停池/龙虎榜里补，某种程度上和这条
指导原则是矛盾的。这次只做了"标记清楚 + 风险等级调高"的透明化处理，没有改变这个补充机制
本身，是否需要对补充候选也跑一遍技术面/风险筛选，需要你来决定。

修复记录（第三轮）：
- save_history：原来直接 open(...,'w') 写文件，进程写到一半被杀会留下损坏/半截的 JSON，
  丢的不只是当天而是整个历史。改成先写临时文件再 os.replace() 原子替换
- run()：新增整体超时看门狗（基于 signal.alarm，仅类 Unix 系统生效，Windows 上会自动
  跳过不报错），避免某一步卡住后整个定时任务无限挂住，影响后续调度
- 新增 _consecutive_days_recommended：基于 history 计算某只股票连续被推荐的天数，写入
  stock['consecutive_days']，方便邮件里标记"连续第N天上榜"，方便看出模型是否在反复推荐同一批股票
- 配合 email_sender 新增的纯文本兼底 part，这里调用 build_plain_text_summary 一起传给 send_email
"""
import json
import os
import sys
import signal
from datetime import datetime, timedelta
from html import escape as html_escape

# 添加 src 到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config, logger
from news_fetcher import collect_all_data
from llm_analyzer import analyze_and_select_stocks
from strategy_generator import enrich_stock_strategies, market_circuit_breaker_check
from email_sender import build_html_report, build_plain_text_summary, send_email

HISTORY_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "history.json")

# 当 LLM 返回不足时，补充到此数量
TARGET_RECOMMEND_COUNT = 10
RUN_TIMEOUT_SECONDS = 20 * 60  # 整个流程最多允许跑 20 分钟，超时强制中断（仅类 Unix 系统生效）


class _RunTimeoutError(Exception):
    """整体流程执行超时"""


def _timeout_handler(signum, frame):
    raise _RunTimeoutError(f"整个流程执行超过 {RUN_TIMEOUT_SECONDS} 秒，强制中断")


def safe_len(obj) -> int:
    """安全获取长度，兼容 pandas.DataFrame 的 empty 属性，避免对 DataFrame 做布尔判断"""
    try:
        if obj is None:
            return 0
        # pandas DataFrame has `empty` attribute
        if hasattr(obj, "empty"):
            return 0 if obj.empty else len(obj)
        return len(obj)
    except Exception:
        return 0


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
    """保存历史推荐记录（先写临时文件再原子替换，避免进程写到一半被杀导致文件损坏）"""
    try:
        history_dir = os.path.dirname(HISTORY_FILE)
        os.makedirs(history_dir, exist_ok=True)
        tmp_path = HISTORY_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, HISTORY_FILE)
        logger.info(f"历史记录已保存，当前记录数: {len(history)}")
    except Exception as e:
        logger.error(f"保存历史记录失败: {e}")


def summarize_dataframe(df, columns, max_rows=15):
    """总结数据框"""
    if df is None or (hasattr(df, 'empty') and df.empty):
        return "暂无数据"
    cols = [c for c in columns if c in df.columns]
    if not cols:
        return "暂无数据（列名不匹配）"
    return df[cols].head(max_rows).to_string(index=False)


def get_yesterday_review(history: list) -> str:
    """获取最近一次推荐复盘（返回 HTML）

    注意：这里取的是历史记录里"今天之前最近的一条"，而不是严格要求日期等于自然日
    "昨天"——如果昨天是周末/脚本没跑，严格匹配"昨天"会一直找不到，复盘会永远是空的。

    逻辑：列出该条记录里每只标的的
    - 推荐保存价 saved_recommendation_close
    - next_day_close（如果已有则显示并计算涨跌%）
    """
    if not history:
        return ""

    today = datetime.now().strftime("%Y-%m-%d")
    prior_entries = [e for e in history if e.get("date") and e["date"] < today]
    if not prior_entries:
        return ""

    entry = max(prior_entries, key=lambda e: e["date"])
    review_date = entry["date"]
    stocks = entry.get("stocks", [])
    if not stocks:
        return f"<div>最近一次({review_date})推荐无标的</div>"

    parts = [f"<div>最近一次({review_date}) 推荐 {len(stocks)} 只：</div>", "<ul>"]
    for s in stocks:
        # 这段 HTML 会被 email_sender 原样插入邮件正文，股票名称/代码来自历史记录（源头是
        # LLM/网络抓取内容），这里必须转义，避免 HTML 注入
        name = html_escape(str(s.get("stock_name", "N/A")))
        code = html_escape(str(s.get("stock_code", "N/A")))
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


def _consecutive_days_recommended(history: list, stock_code: str) -> int:
    """计算某只股票在历史记录里"今天之前连续被推荐"的天数（不含今天），
    按 history 记录本身的日期倒序逐个向前看，一旦中断就停，用于识别模型是否在
    反复推荐同一批股票"""
    if not stock_code:
        return 0
    streak = 0
    for entry in sorted(history, key=lambda e: e.get("date", ""), reverse=True):
        codes = {s.get("stock_code") for s in entry.get("stocks", [])}
        if stock_code in codes:
            streak += 1
        else:
            break
    return streak


def _first_present(row, keys):
    """依次尝试多个候选列名，返回第一个存在且非空的值，都没有则返回 None"""
    for key in keys:
        try:
            value = row.get(key)
        except Exception:
            value = None
        if value not in (None, ""):
            return value
    return None


def _build_stock_from_limit_up_row(row) -> dict:
    """从涨停/龙虎榜行构建简易的 stock dict 作为补充项
    该条目的字段尽量兼容 LLM 输出格式，具体信息后续会被 enrich 填充
    """
    name = _first_present(row, ["名称", "证券简称", "股票简称", "name"])
    code = _first_present(row, ["代码", "证券代码", "股票代码", "code"])

    if not name:
        name = str(row.iloc[0]) if hasattr(row, 'iloc') else 'N/A'
    if not code:
        try:
            code = str(row.iloc[1]) if hasattr(row, 'iloc') else 'N/A'
        except Exception:
            code = 'N/A'

    return {
        'stock_name': str(name),
        'stock_code': str(code),
        'reason': '补充：来自涨停/龙虎榜池，未经 LLM 筛选，仅供参考',
        'buy_price_range': None,
        'take_profit_pct': None,
        'stop_loss_pct': None,
        'expected_days': None,
        'risk_level': '高',  # 缺少基本面/消息面筛选，按更保守的风险等级处理
        'risk_note': '由系统补充，未经 LLM 分析，需人工核验',
        'is_supplement': True,
    }


def supplement_stocks(raw_data: dict, stocks: list, target: int = TARGET_RECOMMEND_COUNT) -> list:
    """当 LLM 返回少于 target 时，使用涨停池和龙虎榜补充候选至 target
    会避免重复已存在的代码
    """
    if not raw_data:
        return stocks

    existing_codes = set([s.get('stock_code') for s in stocks if s.get('stock_code')])
    candidates = []

    # 优先使用涨停池
    limit_up_df = raw_data.get('limit_up')
    if limit_up_df is not None and not (hasattr(limit_up_df, 'empty') and limit_up_df.empty):
        for _, row in limit_up_df.iterrows():
            cand = _build_stock_from_limit_up_row(row)
            if cand['stock_code'] not in existing_codes:
                candidates.append(cand)
                existing_codes.add(cand['stock_code'])
            if len(stocks) + len(candidates) >= target:
                break

    # 如果仍不足，使用龙虎榜
    if len(stocks) + len(candidates) < target:
        lhb_df = raw_data.get('dragon_tiger')
        if lhb_df is not None and not (hasattr(lhb_df, 'empty') and lhb_df.empty):
            for _, row in lhb_df.iterrows():
                cand = _build_stock_from_limit_up_row(row)
                if cand['stock_code'] not in existing_codes:
                    candidates.append(cand)
                    existing_codes.add(cand['stock_code'])
                if len(stocks) + len(candidates) >= target:
                    break

    # 最后补充到目标
    if candidates:
        logger.info(f"补充了 {len(candidates)} 个候选以达到目标 {target} 只（未经 LLM 筛选，需人工验证）")
    return stocks + candidates


def run():
    """主流程"""
    is_valid, errors = Config.validate()
    if not is_valid:
        logger.error("配置检查失败:")
        for error in errors:
            logger.error(f"  ❌ {error}")
        logger.error("请参考 .env.example 文件配置所需的环境变量")
        sys.exit(1)
    logger.info("配置检查通过 ✓")

    watchdog_supported = hasattr(signal, "SIGALRM")
    if watchdog_supported:
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(RUN_TIMEOUT_SECONDS)
    else:
        logger.debug("当前平台不支持 SIGALRM，跳过整体超时看门狗（仅类 Unix 系统可用）")

    try:
        logger.info("=" * 60)
        logger.info("开始执行每日选股流程...")
        logger.info("=" * 60)
        
        # 1. 收集数据
        logger.info("步骤 1/6: 收集市场数据...")
        raw_data = collect_all_data()
        logger.info(f"✓ 新闻数据: {safe_len(raw_data.get('cls_news'))} 条")
        logger.info(f"✓ 涨停股池: {safe_len(raw_data.get('limit_up'))} 只")
        logger.info(f"✓ 龙虎榜: {safe_len(raw_data.get('dragon_tiger'))} 条")
        logger.info(f"✓ 大盘指数: {safe_len(raw_data.get('index'))} 行")
        
        # 2. 熔断检查
        logger.info("步骤 2/6: 执行市场熔断检查...")
        circuit_breaker = market_circuit_breaker_check(raw_data.get("index"))
        if circuit_breaker:
            logger.warning("⚠️  触发熔断机制！大盘波动过大，今日不建议操作")
        else:
            logger.info("✓ 市场正常，继续处理")
        
        # 3. LLM 选股
        stocks = []
        if not circuit_breaker:
            logger.info("步骤 3/6: 准备数据摘要...")
            news_summary = summarize_dataframe(raw_data.get('cls_news'), ["标题", "内容"])
            limit_up_summary = summarize_dataframe(raw_data.get('limit_up'), ["名称", "代码", "连板数"])
            dragon_tiger_summary = summarize_dataframe(raw_data.get('dragon_tiger'), ["名称", "代码", "净买入额"])
            
            logger.info("步骤 4/6: 调用 LLM 进行选股分析...")
            stocks = analyze_and_select_stocks(news_summary, limit_up_summary, dragon_tiger_summary)
            logger.info(f"✓ LLM 推荐 {len(stocks)} 只股票")

            # 如果 LLM 返回不足，我们用涨停/龙虎榜补充
            if len(stocks) < TARGET_RECOMMEND_COUNT:
                stocks = supplement_stocks(raw_data, stocks, TARGET_RECOMMEND_COUNT)
                logger.info(f"当前候选数: {len(stocks)}（包含系统补充）")

            if stocks:
                for i, stock in enumerate(stocks, 1):
                    logger.info(f"  {i}. {stock.get('stock_name')} ({stock.get('stock_code')}): {stock.get('reason')}")
            
            # 4. 技术面验证
            logger.info("步骤 5/6: 进行技术面验证和风险评估...")
            stocks = enrich_stock_strategies(stocks)
            logger.info(f"✓ 已补充技术指标和风险评估")
        else:
            logger.info("步骤 3-5: 跳过（已触发熔断）")
        
        # 5. 加载历史记录
        logger.info("加载历史推荐记录...")
        history = load_history()
        for s in stocks:
            prior_streak = _consecutive_days_recommended(history, s.get("stock_code"))
            if prior_streak:
                s["consecutive_days"] = prior_streak + 1
        yesterday_review = get_yesterday_review(history)
        
        # 6. 生成并发送邮件（失败只记日志，不阻止下面保存历史记录，避免今天的推荐彻底丢失）
        try:
            logger.info("生成 HTML 报告...")
            html = build_html_report(stocks, circuit_breaker, yesterday_review)
            plain_text = build_plain_text_summary(stocks, circuit_breaker)
            logger.info("发送邮件...")
            send_email(html, plain_text)
        except Exception as e:
            logger.error(f"生成/发送邮件失败，仍会继续保存历史记录: {e}", exc_info=True)
        
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
            # next_day_close 目前这套流程里不会产生，预留字段给以后的回填脚本用
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
    finally:
        if watchdog_supported:
            signal.alarm(0)


if __name__ == "__main__":
    run()
