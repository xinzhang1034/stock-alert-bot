"""
新闻抓取模块 - 针对A股市场
使用 akshare（免费开源财经数据库）获取新闻、公告、龙虎榜等数据
"""
import akshare as ak
import pandas as pd
from datetime import datetime
import sys
import os

# 添加 src 到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import logger

# 重试配置
MAX_RETRIES = 2


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
    return pd.DataFrame()


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


def get_dragon_tiger_list():
    """获取当日龙虎榜数据（判断游资/机构动向）"""
    try:
        today = datetime.now().strftime("%Y%m%d")
        df = _retry_call(
            ak.stock_lhb_detail_em,
            start_date=today,
            end_date=today,
            func_name="龙虎榜数据获取"
        )
        return df if not df.empty else pd.DataFrame()
    except Exception as e:
        logger.error(f"龙虎榜数据获取异常: {e}")
        return pd.DataFrame()


def get_limit_up_stocks():
    """获取当日涨停股池（判断市场热点）"""
    try:
        today = datetime.now().strftime("%Y%m%d")
        df = _retry_call(
            ak.stock_zt_pool_em,
            date=today,
            func_name="涨停池数据获取"
        )
        return df if not df.empty else pd.DataFrame()
    except Exception as e:
        logger.error(f"涨停池数据获取异常: {e}")
        return pd.DataFrame()


def get_index_data():
    """获取大盘指数当前状态（判断市场整体情绪）"""
    try:
        df = _retry_call(
            ak.stock_zh_index_spot_em,
            symbol="沪深重要指数",
            func_name="大盘指数获取"
        )
        return df if not df.empty else pd.DataFrame()
    except Exception as e:
        logger.error(f"指数数据获取异常: {e}")
        return pd.DataFrame()


def collect_all_data():
    """汇总所有数据源"""
    logger.info("开始收集市场数据...")
    
    data = {
        "cls_news": get_cls_telegraph_news(),
        "em_news": get_em_news(),
        "dragon_tiger": get_dragon_tiger_list(),
        "limit_up": get_limit_up_stocks(),
        "index": get_index_data(),
    }
    
    logger.info("市场数据收集完成")
    return data


if __name__ == "__main__":
    logger.info("测试数据获取模块...")
    data = collect_all_data()
    for key, df in data.items():
        print(f"\n=== {key} ===")
        print(f"行数: {len(df)}")
        print(df.head(3))
