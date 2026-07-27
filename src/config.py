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
    
    # 调试配置
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    
    @classmethod
    def validate(cls) -> tuple[bool, list[str]]:
        """验证必需的配置是否存在"""
        errors = []
        
        if not cls.DASHSCOPE_API_KEY:
            errors.append("缺少 DASHSCOPE_API_KEY")
        if not cls.SENDER_EMAIL:
            errors.append("缺少 SENDER_EMAIL")
        if not cls.SENDER_PASSWORD:
            errors.append("缺少 SENDER_PASSWORD")
        if not cls.RECEIVER_EMAIL:
            errors.append("缺少 RECEIVER_EMAIL")
        
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
