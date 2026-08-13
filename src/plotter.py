import os
from pathlib import Path
from datetime import datetime, timedelta
import akshare as ak
import mplfinance as mpf
import pandas as pd

from config import Config, logger

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / Config.PLOT_DIR
OUT_DIR.mkdir(parents=True, exist_ok=True)


def plot_last_7days_kline(stock_code: str) -> str | None:
    """为给定股票生成最近 7 个交易日的 K 线图，并返回相对路径（相对于仓库根）。

    若无法生成则返回 None。
    """
    try:
        end = datetime.now()
        start = end - timedelta(days=14)
        df = ak.stock_zh_a_hist(symbol=stock_code, period="daily", adjust="qfq",
                                 start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
        if df is None or df.empty:
            logger.warning(f"无法获取 {stock_code} 的 OHLCV 数据，用于绘图")
            return None

        # 使用最近 7 个可用交易日
        df = df.tail(14)  # 取多一点以保证有 7 个交易日
        # 确认列名并重命名为 mplfinance 期望的列名
        rename_map = {}
        if "开盘" in df.columns:
            rename_map.update({"开盘": "Open", "最高": "High", "最低": "Low", "收盘": "Close", "成交量": "Volume"})
        else:
            rename_map.update({"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
        df = df.rename(columns=rename_map)

        # 选择尾部 7 行（交易日）
        if len(df) < 3:
            logger.warning(f"{stock_code} 可用数据过少，无法绘制 K 线")
            return None
        kdf = df.tail(7).copy()
        # mplfinance 需要 DatetimeIndex
        if not isinstance(kdf.index, pd.DatetimeIndex):
            # akshare 有时 index 是字符串日期列，尝试转换
            try:
                kdf.index = pd.to_datetime(kdf.index)
            except Exception:
                logger.warning(f"无法将索引转换为 DatetimeIndex: {stock_code}")
                return None

        filename = f"{stock_code}_{end.strftime('%Y%m%d')}.png"
        out_path = OUT_DIR / filename
        # 画图并保存
        mpf.plot(kdf, type='candle', style='yahoo', savefig=str(out_path), volume=True, mav=(5,))
        rel_path = os.path.relpath(out_path, ROOT)
        logger.debug(f"生成 K 线图: {rel_path}")
        return rel_path

    except Exception as e:
        logger.warning(f"为 {stock_code} 生成 K 线图失败: {e}")
        return None
