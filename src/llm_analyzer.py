"""
LLM 分析模块 - 使用通义千问免费API（也可替换为智谱GLM/DeepSeek）
申请地址: https://dashscope.console.aliyun.com/

修复记录：
- call_llm：原来只对 Timeout/ConnectionError 重试，HTTP 层面的 429（限流）/5xx（服务端
  临时故障）这类最常见的失败反而会被最后的 `except Exception: raise` 直接抛出、完全不重试。
  改成按状态码判断：429/500/502/503/504 会重试，其余 4xx（如 401/400，重试也没用）直接失败；
  重试等待也从固定延迟改成简单递增（RETRY_DELAY * 尝试次数）
- call_llm：API 返回体里带 "error" 字段的业务错误，原来用 ValueError 抛出，会和"API Key
  未配置"这个真正的配置错误撞到同一个 except ValueError 分支，导致业务错误被误报成"配置检查
  失败"。新增 LLMAPIError 区分开，且业务错误也会走重试
- analyze_and_select_stocks：清理 Markdown 代码块用 `.strip("```json")` 其实是按字符集合
  做 strip，不是按子串删除，只是恰好在常见情况下"凑巧能用"，并不可靠。改成用正则精确去掉
  开头的 ```json 和结尾的 ``` 代码围栏
- analyze_and_select_stocks：新增对每个候选项的基本合法性校验（必须是 dict 且带 stock_code），
  过滤掉 LLM 可能返回的畸形条目，避免脏数据传到下游模块时才报错

功能优化（第二轮）：
- build_prompt：三段摘要新增长度截断兵底，避免上游漏传限制时 prompt 无限变大
- analyze_and_select_stocks：新增对 take_profit_pct/stop_loss_pct/buy_price_range 等
  数值型字段的合理性校验，不合法直接丢弃该候选
- call_llm：显式设置 max_tokens，并记录 finish_reason，输出被截断时及时告警
- model/temperature/max_tokens 改从 Config 读取（带默认值），不用改代码调参；
  最终选中的股票代码/名称也补充进日志，方便直接看结果不用开 debug
"""
import os
import re
import json
import time
import requests
import sys

# 添加 src 到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config, logger

DASHSCOPE_API_KEY = Config.DASHSCOPE_API_KEY
API_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation"
LLM_MODEL = getattr(Config, "LLM_MODEL", "qwen-plus")
LLM_TEMPERATURE = getattr(Config, "LLM_TEMPERATURE", 0.3)
LLM_MAX_TOKENS = getattr(Config, "LLM_MAX_TOKENS", 2000)
MAX_SUMMARY_CHARS = 3000  # 单段摘要最大字符数，防止 prompt 无限变大

# 重试配置
MAX_RETRIES = 3
RETRY_DELAY = 2
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}  # 限流/服务端临时故障，值得重试


class LLMAPIError(Exception):
    """LLM API 返回的业务级错误（区别于网络问题和"API Key 未配置"这类配置错误）"""


def _truncate(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    """截断过长的摘要文本，避免拼进 prompt 后无限膨胀"""
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "...(已截断)"


def build_prompt(news_summary: str, limit_up_summary: str, dragon_tiger_summary: str) -> str:
    """构建 LLM 提示词（要求尽量返回最多 10 只候选标的，并避免仅凭连续涨停选股）"""
    news_summary = _truncate(news_summary)
    limit_up_summary = _truncate(limit_up_summary)
    dragon_tiger_summary = _truncate(dragon_tiger_summary)
    return f"""你是一位专业的A股短线投资分析师。请根据以下今日信息，筛选出最多10只值得关注的短线标的，若没有合适标的返回空数组 []。

【今日财经新闻摘要】
{news_summary}

【今日涨停股池】
{limit_up_summary}

【今日龙虎榜数据】
{dragon_tiger_summary}

提示：避免只因为某股连续涨停/连板就推荐；请综合消息面、资金面与技术面（如量能、均线）给出候选，并尽量给出行业/主题覆盖，降低高度集中于同一板块的情况。

请以严格的JSON数组格式输出，每个元素包含以下字段：
- stock_name: 股票名称
- stock_code: 股票代码（请返回与行情接口兼容的代码，若有后缀也可）
- reason: 入选理由（结合消息面+资金面+技术面，50字以内）
- buy_price_range: 建议买入价区间（如"12.5-13.0"）
- take_profit_pct: 止盈百分比（如8，表示+8%）
- stop_loss_pct: 止损百分比（如5，表示-5%）
- expected_days: 预期持有天数（如"1-3天"）
- risk_level: 风险等级（低/中/高）
- risk_note: 风险提示（30字以内）

只输出JSON数组，不要有其他文字说明。如果没有合适标的，输出空数组 []。"""


def call_llm(prompt: str) -> str:
    """调用通义千问API，支持重试（网络异常/超时/限流/5xx 会重试；其余 4xx 客户端错误不重试）"""
    if not DASHSCOPE_API_KEY:
        raise ValueError("DASHSCOPE_API_KEY 未配置")

    headers = {
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": LLM_MODEL,
        "input": {"messages": [{"role": "user", "content": prompt}]},
        "parameters": {"result_format": "message", "temperature": LLM_TEMPERATURE, "max_tokens": LLM_MAX_TOKENS},
    }

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            logger.debug(f"调用 LLM API (尝试 {attempt + 1}/{MAX_RETRIES})...")
            resp = requests.post(API_URL, headers=headers, json=payload, timeout=60)
            resp.raise_for_status()
            result = resp.json()

            if "error" in result:
                raise LLMAPIError(f"API 返回业务错误: {result.get('error')}")

            choice = result["output"]["choices"][0]
            finish_reason = choice.get("finish_reason")
            if finish_reason and finish_reason != "stop":
                logger.warning(f"LLM 输出可能被截断（finish_reason={finish_reason}），JSON 解析大概率会失败")
            content = choice["message"]["content"]
            logger.debug("✓ LLM 调用成功")
            return content

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            last_error = e
            if status not in RETRYABLE_STATUS_CODES:
                logger.error(f"LLM API 返回不可重试的状态码 {status}: {e}")
                raise
            logger.warning(f"LLM API 返回 {status}，将重试 (尝试 {attempt + 1}/{MAX_RETRIES})")
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = e
            logger.warning(f"网络问题: {e} (尝试 {attempt + 1}/{MAX_RETRIES})")
        except LLMAPIError as e:
            last_error = e
            logger.warning(f"{e}，将重试 (尝试 {attempt + 1}/{MAX_RETRIES})")
        except Exception as e:
            logger.error(f"LLM API 调用失败（不重试）: {e}")
            raise

        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAY * (attempt + 1))  # 简单递增等待，减轻限流场景下的连续打空

    raise RuntimeError(f"LLM API 在 {MAX_RETRIES} 次尝试后仍然失败: {last_error}")


def _strip_markdown_fence(text: str) -> str:
    """去掉 LLM 返回内容外层可能带的 ```json ... ``` 代码围栏"""
    cleaned = text.strip()
    cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
    cleaned = re.sub(r"```\s*$", "", cleaned)
    return cleaned.strip()


def _parse_positive_number(value):
    """把 take_profit_pct/stop_loss_pct 这类字段解析成正数，格式不对返回 None"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def _is_valid_price_range(value) -> bool:
    """校验 buy_price_range 是否为 '低-高' 且 低<高 的格式"""
    if not isinstance(value, str) or "-" not in value:
        return False
    parts = value.split("-")
    if len(parts) != 2:
        return False
    try:
        low, high = float(parts[0]), float(parts[1])
    except ValueError:
        return False
    return 0 < low < high


def analyze_and_select_stocks(news_summary, limit_up_summary, dragon_tiger_summary):
    """主分析函数，返回结构化选股结果，优先返回最多 10 个候选"""
    try:
        prompt = build_prompt(news_summary, limit_up_summary, dragon_tiger_summary)
        raw_result = call_llm(prompt)

        cleaned = _strip_markdown_fence(raw_result)
        stocks = json.loads(cleaned)

        if not isinstance(stocks, list):
            logger.warning(f"LLM 返回格式不是列表: {type(stocks)}")
            stocks = []

        # 过滤掉不是 dict、缺少 stock_code，或数值字段不合理的畸形条目，不让脏数据流入下游模块
        valid_stocks = []
        for item in stocks:
            if not isinstance(item, dict) or not item.get("stock_code"):
                logger.warning(f"忽略格式不合法的候选项: {item!r}")
                continue
            if _parse_positive_number(item.get("take_profit_pct")) is None:
                logger.warning(f"止盈百分比不合法，忽略该候选: {item!r}")
                continue
            if _parse_positive_number(item.get("stop_loss_pct")) is None:
                logger.warning(f"止损百分比不合法，忽略该候选: {item!r}")
                continue
            if not _is_valid_price_range(item.get("buy_price_range", "")):
                logger.warning(f"买入价区间不合法，忽略该候选: {item!r}")
                continue
            valid_stocks.append(item)
        stocks = valid_stocks

        # 强制限制不超过 10 个，避免后续流程数据量异常
        if len(stocks) > 10:
            logger.info("LLM 返回超过10只，截取前10只")
            stocks = stocks[:10]

        stock_labels = ", ".join(f"{s.get('stock_name')}({s.get('stock_code')})" for s in stocks)
        logger.info(f"成功解析 {len(stocks)} 只推荐股票: {stock_labels}")
        return stocks

    except json.JSONDecodeError as e:
        logger.error(f"JSON 解析失败: {e}")
        logger.debug(f"原始响应: {raw_result}")
        return []
    except ValueError as e:
        logger.error(f"配置检查失败: {e}")
        return []
    except Exception as e:
        logger.error(f"LLM 分析失败: {e}")
        return []


if __name__ == "__main__":
    # 测试模式
    logger.info("测试 LLM 分析模块...")
    demo_news = "某新能源公司公布业绩超预期，某半导体政策利好出台"
    demo_limit_up = "示例股票A 涨停 连板2天"
    demo_dragon_tiger = "示例股票A 上榜 游资净买入5000万"
    result = analyze_and_select_stocks(demo_news, demo_limit_up, demo_dragon_tiger)
    print(json.dumps(result, ensure_ascii=False, indent=2))
