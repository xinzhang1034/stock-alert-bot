"""
脚本：回填次日收盘价（next_day_close）到 data/history.json
使用场景：在次日收盘后运行此脚本，脚本会读取 history.json 中指定日期（默认为昨日）的推荐列表，
尝试抓取每只股票在该交易日的收盘价并回填到 next_day_close 字段，然后保存 history.json。

用法：
  python scripts/fill_next_day_close.py --date 2026-08-13 --dry-run
  默认 --date 为昨天（以运行当天计算），--dry-run 表示不写文件，只打印将要写入的内容。

注意：脚本依赖 akshare，网络/数据源可能导致个别股票无法获取到收盘价（停牌/刚上市），
脚本会在无法获取时跳过并记录警告。
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timedelta

# 添加 src 到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import akshare as ak
from src.strategy_generator import _normalize_code
from src.config import logger

# HISTORY_FILE 与 main.py 保持一致
HISTORY_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "history.json")

MAX_FETCH_RETRIES = 3
FETCH_RETRY_DELAY = 1.5


def _try_fetch_close_for_date(code_variants, target_date_str):
    """尝试用多个 code 变体抓取 target_date_str 的收盘价（格式 'YYYY-MM-DD' 或 'YYYYMMDD'），
    返回收盘价或 None。"""
    # akshare 接口对日期格式的容忍度不同，统一尝试用 YYYYMMDD 和 YYYY-MM-DD
    ymd = datetime.strptime(target_date_str, "%Y-%m-%d").strftime("%Y%m%d")
    ymd_dash = target_date_str

    for attempt in range(MAX_FETCH_RETRIES):
        for variant in code_variants:
            try:
                # 获取最近几天的日线，包含目标日
                df = ak.stock_zh_a_hist(symbol=variant, period="daily", adjust="qfq")
                if df is None or df.empty:
                    continue
                # 有些 df 的索引就是日期，也有可能有 '日期' 列
                if "日期" in df.columns:
                    # 兼容不同日期格式
                    df_dates = df["日期"].astype(str)
                    mask = df_dates.str.contains(ymd) | df_dates.str.contains(ymd_dash)
                    matched = df[mask]
                else:
                    idx = df.index.astype(str)
                    mask = idx.str.contains(ymd) | idx.str.contains(ymd_dash)
                    matched = df[mask]

                if matched is not None and not matched.empty:
                    # 取第一行的收盘
                    close = matched.iloc[0]["收盘"]
                    return float(close)
            except Exception as e:
                logger.debug(f"尝试获取 {variant} 在 {target_date_str} 收盘失败: {e}")
                continue
        time.sleep(FETCH_RETRY_DELAY)
    return None


def fill_next_day_close(target_date: str = None, dry_run: bool = True):
    """主函数：回填 target_date 的次日收盘价到 history.json 中对应日期的记录
    target_date: 'YYYY-MM-DD'，表示要回填哪一天的“次日收盘”（例如：若要回填 2026-08-13，则目标日期为 '2026-08-13'）
    注意：脚本假定 history.json 中的记录的 'date' 字段格式为 'YYYY-MM-DD'。
    """
    if target_date is None:
        target_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    # 读取历史
    if not os.path.exists(HISTORY_FILE):
        logger.error(f"历史文件不存在：{HISTORY_FILE}")
        return

    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        try:
            history = json.load(f)
        except Exception as e:
            logger.error(f"加载历史文件失败: {e}")
            return

    found = False
    for entry in history:
        if entry.get("date") == target_date:
            found = True
            stocks = entry.get("stocks", [])
            for s in stocks:
                # 如果已有 next_day_close，则跳过
                if s.get("next_day_close") is not None:
                    logger.info(f"{s.get('stock_name')}({s.get('stock_code')}) 已有 next_day_close，跳过")
                    continue

                code = s.get("stock_code", "")
                base = _normalize_code(code)
                variants = [code, base]
                if not any('.' in str(code) for _ in [code]):
                    if base.startswith("6"):
                        variants.append(base + ".SH")
                    else:
                        variants.append(base + ".SZ")

                close = _try_fetch_close_for_date(variants, target_date)
                if close is None:
                    logger.warning(f"无法获取 {s.get('stock_name')}({code}) 在 {target_date} 的收盘价")
                else:
                    logger.info(f"回填 {s.get('stock_name')}({code}) 在 {target_date} 的收盘价: {close}")
                    s["next_day_close"] = close

    if not found:
        logger.warning(f"history.json 中未找到日期为 {target_date} 的记录")
        return

    if dry_run:
        logger.info("dry-run 模式，不写入文件。请检查日志并在确认后取消 --dry-run")
    else:
        try:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
            logger.info(f"已保存回填的历史到 {HISTORY_FILE}")
        except Exception as e:
            logger.error(f"保存回填结果失败: {e}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='回填次日收盘价至 history.json')
    parser.add_argument('--date', type=str, help='要回填的日期，格式 YYYY-MM-DD，默认昨日')
    parser.add_argument('--dry-run', action='store_true', help='仅打印将要写入的内容，不实际写文件')
    args = parser.parse_args()

    fill_next_day_close(target_date=args.date, dry_run=args.dry_run)
