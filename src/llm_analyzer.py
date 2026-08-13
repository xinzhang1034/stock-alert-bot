"""
LLM 分析模块 - 使用通义千问免费API（也可替换为智谱GLM/DeepSeek）
申请地址: https://dashscope.console.aliyun.com/
"""
import os
import json
import requests
import sys

# 添加 src 到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config, logger

DASHSCOPE_API_KEY = Config.DASHSCOPE_API_KEY
API_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation"

# 重试配置
MAX_RETRIES = 3
RETRY_DELAY = 2


def build_prompt(news_summary: str, limit_up_summary: str, dragon_tiger_summary: str) -> str:
    """构建 LLM 提示词（要求尽量返回最多 10 只候选标的，并避免仅凭连续涨停选股）"""
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
    """调用通义千问API，支持重试"""
    if not DASHSCOPE_API_KEY:
        raise ValueError("DASHSCOPE_API_KEY 未配置")
    
    headers = {
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "qwen-plus",
        "input": {"messages": [{"role": "user", "content": prompt}]},
        "parameters": {"result_format": "message", "temperature": 0.3},
    }
    
    for attempt in range(MAX_RETRIES):
        try:
            logger.debug(f"调用 LLM API (尝试 {attempt + 1}/{MAX_RETRIES})...")
            resp = requests.post(API_URL, headers=headers, json=payload, timeout=60)
            resp.raise_for_status()
            result = resp.json()
            
            if "error" in result:
                raise ValueError(f"API 错误: {result.get('error')}")
            
            content = result["output"]["choices"][0]["message"]["content"]
            logger.debug("✓ LLM 调用成功")
            return content
            
        except requests.exceptions.Timeout:
            logger.warning(f"API 调用超时 (尝试 {attempt + 1}/{MAX_RETRIES})")
            if attempt < MAX_RETRIES - 1:
                import time
                time.sleep(RETRY_DELAY)
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"网络连接错误: {e} (尝试 {attempt + 1}/{MAX_RETRIES})")
            if attempt < MAX_RETRIES - 1:
                import time
                time.sleep(RETRY_DELAY)
        except Exception as e:
            logger.error(f"LLM API 调用失败: {e}")
            raise
    
    raise TimeoutError(f"LLM API 在 {MAX_RETRIES} 次尝试后仍然超时")


def analyze_and_select_stocks(news_summary, limit_up_summary, dragon_tiger_summary):
    """主分析函数，返回结构化选股结果，优先返回最多 10 个候选"""
    try:
        prompt = build_prompt(news_summary, limit_up_summary, dragon_tiger_summary)
        raw_result = call_llm(prompt)
        
        # 清理 JSON 格式
        cleaned = raw_result.strip().strip("```json").strip("```").strip()
        stocks = json.loads(cleaned)
        
        if not isinstance(stocks, list):
            logger.warning(f"LLM 返回格式不是列表: {type(stocks)}")
            stocks = []
        
        # 强制限制不超过 10 个，避免后续流程数据量异常
        if len(stocks) > 10:
            logger.info(f"LLM 返回超过10只，截取前10只"); stocks = stocks[:10]
        
        logger.info(f"成功解析 {len(stocks)} 只推荐股票")
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
