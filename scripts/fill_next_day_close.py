"""
脚本：回填次日收盘价（next_day_close）到 data/history.json
使用场景：在次日收盘后运行此脚本，脚本会读取 history.json 中缺失 next_day_close 且
下一交易日已收盘的推荐记录，尝试抓取每只股票的收盘价并回填，然后保存 history.json。

用法：
  python scripts/fill_next_day_close.py --write
  不传 --date 时会自动扫描所有待回填的日期（即使某天定时任务没跑，下次也会自动补齐）；
  只想处理某一天可以加 --date 2026-08-13；不加 --write 默认按 dry-run 处理，只打印不写文件。

注意：脚本依赖 akshare，网络/数据源可能导致个别股票无法获取到收盘价（停牌/刚上市），
脚本会在无法获取时跳过并记录警告。

修复记录：
- 【严重】原来的 fill_next_day_close 直接把 target_date（推荐当天）自己的收盘价存进
  next_day_close 字段，等于"次日收盘价"存的是"当天收盘价"，和 main.py 里
  saved_recommendation_close（同样是推荐当天的价格）几乎是同一个值，导致"次日复盘"算出来
  的涨跌幅长期趋近于 0，完全没有反映真实的次日表现，这个 bug 会让整个复盘功能失去意义。
  修复：新增 _next_trading_day_str()，从 target_date 往后找下一个交易日（跳过周末，
  未接交易日历、无法识别法定节假日），实际抓取的是这个"次日"的收盘价
- _try_fetch_close_for_date：原来用 str.contains() 做日期匹配，是子串匹配不是精确匹配，
  理论上有误匹配风险；改成精确相等比较。同时原来没有限定 start_date/end_date，每次都拉
  全量历史行情，改成只拉 target_date 前后一小段窗口，减少不必要的数据量
- fill_next_day_close：股票代码后缀猜测那段用 `any(... for _ in [code])` 的绕弯写法
  简化为直接的 in 判断；补上北交所（43/83/87/88 开头 -> .BJ）分支，和策略生成模块保持一致
- fill_next_day_close：写 history.json 改成先写临时文件再 os.replace 原子替换，和 main.py
  的 save_history 保持一致，避免进程中途被杀导致文件损坏
- 【CLI 默认行为变更，注意】原来 argparse 的 --dry-run 默认是 False，也就是"不加任何参数
  直接跑就会真的写文件"，和函数自己 dry_run=True 的"安全默认值"互相矛盾，容易在忘记加
  --dry-run 时误写脏数据。改成默认按 dry-run 处理，显式加 --write 才会真正落盘
- 新增执行完毕后的汇总日志（成功/跳过/失败数量）

优化（第二轮）：
- 不传 --date 时，改为自动扫描 history.json 里所有“存在缺失 next_day_close 且次日已经
  收盘”的日期一次性全部回填，即使某天定时任务没跑，下次跑也会自动补上
- 新增 _is_close_available()：如果计算出来的“次日”还没收盘，提前跳过不去请求，避免
  对每只股票都请求失败一遍才发现“还没到时间”
- 同一个日期在 history.json 里匹配到多条记录时，现在会明确记一条 warning
- 新增基于 fcntl 的文件锁，包住 history.json 的读取-修改-写入整个过程（仅 Unix 系统生效）
"""

import os
import sys
import json
import time
import argparse
import contextlib
from datetime import datetime, timedelta

# 添加 src 到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import akshare as ak
from src.strategy_generator import _normalize_code
from src.config import logger

# HISTORY_FILE 与 main.py 保持一致
HISTORY_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "history.json")
LOCK_FILE = HISTORY_FILE + ".lock"

MAX_FETCH_RETRIES = 3
FETCH_RETRY_DELAY = 1.5
MARKET_CLOSE_HOUR = 15  # 简化判断：认为当天这个时点之后收盘数据大概率已可用


@contextlib.contextmanager
def _history_file_lock():
    """用文件锁保护 history.json 的读取-修改-写入整个过程，避免和 main.py 等其它进程
    并发读写时互相覆盖。仅 Unix 系统生效，其它平台会跳过不报错"""
    try:
        import fcntl
    except ImportError:
        logger.debug("当前平台没有 fcntl，跳过 history.json 的文件锁保护")
        yield
        return

    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    lock_fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fp, fcntl.LOCK_UN)
        lock_fp.close()


def _is_close_available(close_date_str: str) -> bool:
    """粗略判断 close_date 这一天的收盘数据是否大概率已经可用"""
    close_date = datetime.strptime(close_date_str, "%Y-%m-%d").date()
    now = datetime.now()
    if close_date < now.date():
        return True
    if close_date == now.date() and now.hour >= MARKET_CLOSE_HOUR:
        return True
    return False


def _next_trading_day_str(date_str: str) -> str:
    """从 date_str（YYYY-MM-DD，推荐发出的那一天）往后找下一个自然日意义上的交易日。
    注意：没有接交易日历，无法识别法定节假日，节假日次日仍可能查不到数据（会记警告并跳过）"""
    d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
    while d.weekday() >= 5:  # 5=周六 6=周日
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _try_fetch_close_for_date(code_variants, target_date_str):
    """尝试用多个 code 变体精确抓取 target_date_str（'YYYY-MM-DD'）当天的收盘价，
    返回收盘价或 None。"""
    target = datetime.strptime(target_date_str, "%Y-%m-%d")
    start_date = (target - timedelta(days=10)).strftime("%Y%m%d")
    end_date = (target + timedelta(days=1)).strftime("%Y%m%d")
    ymd = target.strftime("%Y%m%d")
    ymd_dash = target_date_str

    for attempt in range(MAX_FETCH_RETRIES):
        for variant in code_variants:
            try:
                # 只拉目标日前后一小段窗口，避免每次都拉全量历史行情
                df = ak.stock_zh_a_hist(
                    symbol=variant, period="daily", adjust="qfq",
                    start_date=start_date, end_date=end_date,
                )
                if df is None or df.empty:
                    continue
                # 有些 df 的索引就是日期，也有可能有 '日期' 列；统一按精确相等匹配，不用子串匹配
                if "日期" in df.columns:
                    df_dates = df["日期"].astype(str)
                    mask = (df_dates == ymd_dash) | (df_dates.str.replace("-", "", regex=False) == ymd)
                    matched = df[mask]
                else:
                    idx = df.index.astype(str)
                    mask = (idx == ymd_dash) | (idx.str.replace("-", "", regex=False) == ymd)
                    matched = df[mask]

                if matched is not None and not matched.empty:
                    close = matched.iloc[0]["收盘"]
                    return float(close)
            except Exception as e:
                logger.debug(f"尝试获取 {variant} 在 {target_date_str} 收盘失败: {e}")
                continue
        time.sleep(FETCH_RETRY_DELAY)
    return None


def _fill_entry_stocks(stocks: list, close_date: str) -> tuple:
    """对单个 history 条目里的 stocks 列表回填 next_day_close，返回 (filled, skipped, failed)"""
    filled, skipped, failed = 0, 0, 0
    for s in stocks:
        if s.get("next_day_close") is not None:
            logger.info(f"{s.get('stock_name')}({s.get('stock_code')}) 已有 next_day_close，跳过")
            skipped += 1
            continue

        code = s.get("stock_code", "")
        base = _normalize_code(code)
        variants = [code, base]
        if "." not in str(code):
            if base.startswith("6"):
                variants.append(base + ".SH")
            elif base[:2] in ("43", "83", "87", "88"):
                variants.append(base + ".BJ")
            else:
                variants.append(base + ".SZ")

        close = _try_fetch_close_for_date(variants, close_date)
        if close is None:
            logger.warning(f"无法获取 {s.get('stock_name')}({code}) 在 {close_date} 的收盘价")
            failed += 1
        else:
            logger.info(f"回填 {s.get('stock_name')}({code}) 在 {close_date} 的收盘价: {close}")
            s["next_day_close"] = close
            filled += 1
    return filled, skipped, failed


def _find_pending_dates(history: list) -> list:
    """找出所有"存在缺失 next_day_close 的股票，且下一交易日已经收盘"的历史日期，
    用于不传 --date 时自动补齐所有欠下的回填"""
    pending = []
    for entry in history:
        date = entry.get("date")
        stocks = entry.get("stocks", [])
        if not date or not stocks:
            continue
        if all(s.get("next_day_close") is not None for s in stocks):
            continue
        close_date = _next_trading_day_str(date)
        if _is_close_available(close_date):
            pending.append(date)
    return sorted(set(pending))


def fill_next_day_close(target_date: str = None, dry_run: bool = True):
    """主函数：回填推荐批次在下一个交易日的收盘价到 next_day_close 字段。
    target_date: 'YYYY-MM-DD'，只回填这一天的推荐；不传则自动扫描 history.json 里所有
    "缺 next_day_close 且次日已收盘"的记录一次性全部回填。
    注意：脚本假定 history.json 中的记录的 'date' 字段格式为 'YYYY-MM-DD'。
    """
    with _history_file_lock():
        if not os.path.exists(HISTORY_FILE):
            logger.error(f"历史文件不存在：{HISTORY_FILE}")
            return

        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            try:
                history = json.load(f)
            except Exception as e:
                logger.error(f"加载历史文件失败: {e}")
                return

        if target_date is not None:
            target_dates = [target_date]
        else:
            target_dates = _find_pending_dates(history)
            if not target_dates:
                logger.info("没有需要回填的记录（要么都已回填，要么次日还没收盘）")
                return
            logger.info(f"未传 --date，自动发现 {len(target_dates)} 个待回填日期: {', '.join(target_dates)}")

        any_found = False
        total_filled, total_skipped, total_failed = 0, 0, 0

        for td in target_dates:
            matched_entries = [e for e in history if e.get("date") == td]
            if not matched_entries:
                logger.warning(f"history.json 中未找到日期为 {td} 的记录")
                continue
            if len(matched_entries) > 1:
                logger.warning(
                    f"日期 {td} 在 history.json 里出现了 {len(matched_entries)} 条记录，"
                    f"数据可能异常，将全部处理"
                )
            any_found = True

            close_date = _next_trading_day_str(td)
            if not _is_close_available(close_date):
                logger.info(f"{td} 的下一交易日 {close_date} 还没收盘，跳过（下次再跑会自动补上）")
                continue

            for entry in matched_entries:
                filled, skipped, failed = _fill_entry_stocks(entry.get("stocks", []), close_date)
                total_filled += filled
                total_skipped += skipped
                total_failed += failed

        if not any_found:
            return

        logger.info(f"本次回填汇总：成功 {total_filled}，跳过（已回填） {total_skipped}，失败 {total_failed}")

        if dry_run:
            logger.info("dry-run 模式，不写入文件。确认无误后加 --write 参数再运行以实际写入")
        else:
            try:
                tmp_path = HISTORY_FILE + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(history, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, HISTORY_FILE)
                logger.info(f"已保存回填的历史到 {HISTORY_FILE}")
            except Exception as e:
                logger.error(f"保存回填结果失败: {e}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='回填次日收盘价至 history.json')
    parser.add_argument('--date', type=str,
                         help='只回填这一天的推荐，格式 YYYY-MM-DD；不传则自动扫描所有待回填的日期')
    parser.add_argument('--write', action='store_true',
                         help='实际写入 history.json；不加此参数默认按 dry-run 处理，只打印不写文件')
    args = parser.parse_args()

    fill_next_day_close(target_date=args.date, dry_run=not args.write)
