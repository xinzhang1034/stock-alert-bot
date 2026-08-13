"""
配置管理模块 - 统一管理环境变量和配置
"""
import os
import logging
from typing import Optional

# 配置类
class Config:
    """应用配置"""
    
    # LLM 配置
    DASHSCOPE_API_KEY: str = os.getenv("DASHSCOPE_API_KEY", "")
    
    # 邮件配置
    SMTP_SERVER: str = os.getenv("SMTP_SERVER", "smtp-mail.outlook.com")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
    SENDER_EMAIL: str = os.getenv("SENDER_EMAIL", "")
    SENDER_PASSWORD: str = os.getenv("SENDER_PASSWORD", "")
    RECEIVER_EMAIL: str = os.getenv("RECEIVER_EMAIL", "")
    # 支持通过 RECEIVER_EMAILS 指定多个收件人（逗号/分号分隔）
    RECEIVER_EMAILS_ENV: str = os.getenv("RECEIVER_EMAILS", "")
    
    # 调试配置
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # 推荐数量（默认 10）
    RECOMMEND_COUNT: int = int(os.getenv("RECOMMEND_COUNT", "10"))

    # K 线图输出目录（相对于仓库根目录）
    PLOT_DIR: str = os.getenv("PLOT_DIR", "static/plots")

    # 复盘对比天数（默认次日）
    RECAP_LOOKAHEAD_DAYS: int = int(os.getenv("RECAP_LOOKAHEAD_DAYS", "1"))
    
    @classmethod
    def validate(cls) -> tuple[bool, list[str]]:
        """验证必需的配置是否存在

        接受单个 RECEIVER_EMAIL 或 多收件人环境变量 RECEIVER_EMAILS
        """
        errors = []
        
        if not cls.DASHSCOPE_API_KEY:
            errors.append("缺少 DASHSCOPE_API_KEY")
        if not cls.SENDER_EMAIL:
            errors.append("缺少 SENDER_EMAIL")
        if not cls.SENDER_PASSWORD:
            errors.append("缺少 SENDER_PASSWORD")
        # 接受单个 RECEIVER_EMAIL 或 环境变量 RECEIVER_EMAILS
        if not cls.RECEIVER_EMAIL and not cls.RECEIVER_EMAILS_ENV:
            errors.append("缺少 RECEIVER_EMAIL 或 RECEIVER_EMAILS (secrets)")
        
        return len(errors) == 0, errors


def setup_logging(log_level: str = "INFO") -> logging.Logger:
    """设置日志"""
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    return logging.getLogger(__name__)


# 初始化日志
logger = setup_logging(Config.LOG_LEVEL)
