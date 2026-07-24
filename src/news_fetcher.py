"""
新闻抓取模块 - 针对A股市场
使用 akshare（免费开源财经数据库）获取新闻、公告、龙虎榜等数据
"""
import akshare as ak
import pandas as pd
from datetime import datetime


def get_cls_telegraph_news(limit=50):
    """获取财联社电报新闻（免费，无需API Key）"""
    try:
        df = ak.stock_info_global_cls(symbol="全部")
        return df.head(limit)
    except Exception as e:
        print(f"财联社新闻获取失败: {e}")
        return pd.DataFrame()


def get_em_news(limit=50):
    """获取东方财富财经早餐/新闻"""
    try:
        df = ak.stock_news_em()
        return df.head(limit)
    except Exception as e:
        print(f"东方财富新闻获取失败: {e}")
        return pd.DataFrame()


def get_dragon_tiger_list():
    """获取当日龙虎榜数据（判断游资/机构动向）"""
    try:
        today = datetime.now().strftime("%Y%m%d")
        df = ak.stock_lhb_detail_em(start_date=today, end_date=today)
        return df
    except Exception as e:
        print(f"龙虎榜数据获取失败: {e}")
        return pd.DataFrame()


def get_limit_up_stocks():
    """获取当日涨停股池（判断市场热点）"""
    try:
        today = datetime.now().strftime("%Y%m%d")
        df = ak.stock_zt_pool_em(date=today)
        return df
    except Exception as e:
        print(f"涨停池数据获取失败: {e}")
        return pd.DataFrame()


def get_index_data():
    """获取大盘指数当前状态（判断市场整体情绪）"""
    try:
        sh = ak.stock_zh_index_spot_em(symbol="沪深重要指数")
        return sh
    except Exception as e:
        print(f"指数数据获取失败: {e}")
        return pd.DataFrame()


def collect_all_data():
    """汇总所有数据源"""
    return {
        "cls_news": get_cls_telegraph_news(),
        "em_news": get_em_news(),
        "dragon_tiger": get_dragon_tiger_list(),
        "limit_up": get_limit_up_stocks(),
        "index": get_index_data(),
    }


if __name__ == "__main__":
    data = collect_all_data()
    for key, df in data.items():
        print(f"\n=== {key} ===")
        print(df.head())
