"""
新闻抓取模块 - 针对A股市场
使用 akshare（免费开源财经数据库）获取新闻、公告、龙虎榜等数据

修复记录：
- get_dragon_tiger_list / get_limit_up_stocks：原来固定用"今天"查龙虎榜/涨停池，
  这两类数据通常收盘后（约16-17点）才发布，如果任务在盘中/开盘前跑，today 几乎必然
  查不到数据。改为从"最近一个大概率已发布数据的交易日"开始查，查到空数据就自动往前
  回退几天兜底（仍未接入交易日历，无法识别法定节假日，已在注释里注明这个局限）
- get_dragon_tiger_list / get_limit_up_stocks / get_index_data：原来外层 try/except
  实际上永远不会触发（_retry_call 内部已经吞掉所有异常，只会返回结果或空 DataFrame，
  不会抛出），是一段死代码；get_index_data 顺手去掉了没意义的 `if not df.empty else
  pd.DataFrame()`（_retry_call 的返回值本来就已经是这个结果，判断多余）
- _retry_call：重试之间补上 sleep，避免失败后立刻打第二枪
- collect_all_data：5 个数据源互不依赖，改为并发拉取，减少总耗时；每个 future.result()
  加了超时保护（避免单个接口卡死拖垮整体流程），收尾补一行各数据源行数的汇总日志
- get_dragon_tiger_list / get_limit_up_stocks：新增 limit 参数（默认 30），避免数据量大时
  把下游 LLM 的输入撑得太大
"""
import akshare as ak
import pandas as pd
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import sys
import os

# 添加 src 到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import logger

# 重试配置
MAX_RETRIES = 2
RETRY_DELAY = 1.0
MAX_DATE_LOOKBACK_DAYS = 5  # 龙虎榜/涨停池查询时，当天无数据最多往前回退几天
DEFAULT_RESULT_LIMIT = 30  # 龙虎榜/涨停池默认最多保留的行数，控制下游 LLM 输入大小
FUTURE_TIMEOUT_SECONDS = 30  # 并发收集时单个数据源的等待上限


def _retry_call(func, *args, func_name="", **kwargs):
    """通用重试装饰器"""
    for attempt in range(MAX_RETRIES):
        try:
            logger.debug(f"调用 {func_name} (尝试 {attempt + 1}/{MAX_RETRIES})...")
            result = func(*args, **kwargs)
            logger.debug(f"✓ {func_name} 成功")
            return result
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                logger.warning(f"{func_name} 失败: {e}")
                return pd.DataFrame()
            logger.debug(f"{func_name} 失败，重试中: {e}")
            time.sleep(RETRY_DELAY)
    return pd.DataFrame()  # 正常不会走到这里，for 循环每一轮都会 return


def _latest_likely_trade_date(reference: datetime = None) -> datetime:
    """猜测"数据大概率已发布"的最近一个交易日：收盘(15点)前用前一天，并跳过周末。
    注意：没有接交易日历，无法识别法定节假日，节假日前后仍要靠调用方的按天回退兜底"""
    ref = reference or datetime.now()
    if ref.hour < 15:
        ref = ref - timedelta(days=1)
    while ref.weekday() >= 5:  # 5=周六, 6=周日
        ref = ref - timedelta(days=1)
    return ref


def get_cls_telegraph_news(limit=50):
    """获取财联社电报新闻（免费，无需API Key）"""
    try:
        df = _retry_call(
            ak.stock_info_global_cls,
            symbol="全部",
            func_name="财联社新闻获取"
        )
        if not df.empty:
            return df.head(limit)
        return pd.DataFrame()
    except Exception as e:
        logger.error(f"财联社新闻获取异常: {e}")
        return pd.DataFrame()


def get_em_news(limit=50):
    """获取东方财富财经早餐/新闻"""
    try:
        df = _retry_call(
            ak.stock_news_em,
            func_name="东方财富新闻获取"
        )
        if not df.empty:
            return df.head(limit)
        return pd.DataFrame()
    except Exception as e:
        logger.error(f"东方财富新闻获取异常: {e}")
        return pd.DataFrame()


def get_dragon_tiger_list(max_lookback_days=MAX_DATE_LOOKBACK_DAYS, limit=DEFAULT_RESULT_LIMIT):
    """获取最近一个交易日的龙虎榜数据（判断游资/机构动向），当天查不到会自动往前回退"""
    try:
        trade_date = _latest_likely_trade_date()
        for _ in range(max_lookback_days):
            date_str = trade_date.strftime("%Y%m%d")
            df = _retry_call(
                ak.stock_lhb_detail_em,
                start_date=date_str,
                end_date=date_str,
                func_name=f"龙虎榜数据获取({date_str})"
            )
            if not df.empty:
                return df.head(limit)
            trade_date -= timedelta(days=1)
        return pd.DataFrame()
    except Exception as e:
        logger.error(f"龙虎榜数据获取异常: {e}")
        return pd.DataFrame()


def get_limit_up_stocks(max_lookback_days=MAX_DATE_LOOKBACK_DAYS, limit=DEFAULT_RESULT_LIMIT):
    """获取最近一个交易日的涨停股池（判断市场热点），当天查不到会自动往前回退"""
    try:
        trade_date = _latest_likely_trade_date()
        for _ in range(max_lookback_days):
            date_str = trade_date.strftime("%Y%m%d")
            df = _retry_call(
                ak.stock_zt_pool_em,
                date=date_str,
                func_name=f"涨停池数据获取({date_str})"
            )
            if not df.empty:
                return df.head(limit)
            trade_date -= timedelta(days=1)
        return pd.DataFrame()
    except Exception as e:
        logger.error(f"涨停池数据获取异常: {e}")
        return pd.DataFrame()


def get_index_data():
    """获取大盘指数当前状态（判断市场整体情绪）"""
    return _retry_call(
        ak.stock_zh_index_spot_em,
        symbol="沪深重要指数",
        func_name="大盘指数获取"
    )


def collect_all_data():
    """汇总所有数据源（5 个数据源互不依赖，并发拉取以减少总耗时）"""
    logger.info("开始收集市场数据...")

    tasks = {
        "cls_news": get_cls_telegraph_news,
        "em_news": get_em_news,
        "dragon_tiger": get_dragon_tiger_list,
        "limit_up": get_limit_up_stocks,
        "index": get_index_data,
    }
    data = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = {key: executor.submit(func) for key, func in tasks.items()}
        for key, future in futures.items():
            try:
                data[key] = future.result(timeout=FUTURE_TIMEOUT_SECONDS)
            except FutureTimeoutError:
                # 超时的线程本身无法强制中止，只是不再等待它，返回空结果占位
                logger.warning(f"{key} 数据源超过 {FUTURE_TIMEOUT_SECONDS}s 未返回，视为失败")
                data[key] = pd.DataFrame()
            except Exception as e:
                logger.warning(f"{key} 数据源获取异常: {e}")
                data[key] = pd.DataFrame()

    for key, df in data.items():
        logger.info(f"  {key}: {len(df)} 行")

    logger.info("市场数据收集完成")
    return data


if __name__ == "__main__":
    logger.info("测试数据获取模块...")
    data = collect_all_data()
    for key, df in data.items():
        print(f"\n=== {key} ===")
        print(f"行数: {len(df)}")
        print(df.head(3))
