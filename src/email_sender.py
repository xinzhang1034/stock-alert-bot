"""
邮件发送模块 - 适配 Hotmail/Outlook SMTP
"""
import os
import smtplib
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# 添加 src 到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config, logger

SMTP_SERVER = Config.SMTP_SERVER
SMTP_PORT = Config.SMTP_PORT
SENDER_EMAIL = Config.SENDER_EMAIL
SENDER_PASSWORD = Config.SENDER_PASSWORD
RECEIVER_EMAIL = Config.RECEIVER_EMAIL


def build_html_report(stocks: list, circuit_breaker: bool, yesterday_review: str = "") -> str:
    """构建 HTML 报告"""
    today = datetime.now().strftime("%Y-%m-%d")

    if circuit_breaker:
        return f"""
        <html>
        <body style="font-family: Arial, sans-serif;">
            <h2>【{today}】今日A股短线关注提示</h2>
            <p style="color:red; font-weight:bold;">⚠️ 大盘出现异常波动，今日不建议主动操作，请观望为主。</p>
            <p style="color:gray; font-size:12px;">
                免责声明：以上内容由自动化程序基于公开新闻和市场数据生成，仅供参考，不构成任何投资建议。
                股市有风险，投资需谨慎，请独立判断并自行承担投资决策的后果。
            </p>
        </body>
        </html>
        """

    rows = ""
    for s in stocks:
        warnings = "；".join(s.get("extra_warnings", [])) or "无"
        rows += f"""
        <tr>
            <td>{s.get('stock_name')} ({s.get('stock_code')})</td>
            <td>{s.get('reason')}</td>
            <td>{s.get('buy_price_range')}</td>
            <td>+{s.get('take_profit_pct')}%</td>
            <td>-{s.get('stop_loss_pct')}%</td>
            <td>{s.get('expected_days')}</td>
            <td>{s.get('risk_level')}</td>
            <td>{s.get('position_suggestion')}</td>
            <td>{s.get('risk_note')}；{warnings}</td>
        </tr>
        """

    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif;">
        <h2>【{today}】今日A股短线关注标的</h2>
        <table border="1" cellpadding="6" cellspacing="0" style="border-collapse: collapse; width:100%; font-size:13px;">
            <tr style="background-color:#f2f2f2;">
                <th>股票</th><th>入选理由</th><th>买入价区间</th><th>止盈</th><th>止损</th>
                <th>预期天数</th><th>风险等级</th><th>仓位建议</th><th>风险提示</th>
            </tr>
            {rows}
        </table>
        <h3>昨日推荐复盘</h3>
        <p>{yesterday_review or "暂无历史数据"}</p>
        <p style="color:gray; font-size:12px;">
            免责声明：以上内容由自动化程序基于公开新闻和市场数据生成，仅供参考，不构成任何投资建议。
            股市有风险，投资需谨慎，请独立判断并自行承担投资决策的后果。
        </p>
    </body>
    </html>
    """
    return html


def send_email(html_content: str):
    """发送邮件"""
    try:
        # 验证必需的配置
        if not SENDER_EMAIL or not SENDER_PASSWORD or not RECEIVER_EMAIL:
            raise ValueError("邮件配置不完整：缺少 SENDER_EMAIL、SENDER_PASSWORD 或 RECEIVER_EMAIL")
        
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"【A股短线助手】{datetime.now().strftime('%Y-%m-%d')} 今日关注标的"
        msg["From"] = SENDER_EMAIL
        msg["To"] = RECEIVER_EMAIL
        msg.attach(MIMEText(html_content, "html", "utf-8"))

        logger.debug(f"连接到 SMTP 服务器: {SMTP_SERVER}:{SMTP_PORT}...")
        
        # Outlook/Hotmail 使用 587 端口 + STARTTLS
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            
            logger.debug(f"使用账户登录: {SENDER_EMAIL}")
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            
            logger.debug(f"发送邮件至: {RECEIVER_EMAIL}")
            server.sendmail(SENDER_EMAIL, RECEIVER_EMAIL, msg.as_string())
        
        logger.info("✓ 邮件发送成功")
        
    except smtplib.SMTPAuthenticationError as e:
        logger.error(f"邮件发送失败 - 认证错误: {e}")
        logger.error("请检查 SENDER_EMAIL 和 SENDER_PASSWORD 是否正确")
        logger.error("注意：Outlook 需要使用应用专用密码，非登录密码")
        raise
    except smtplib.SMTPException as e:
        logger.error(f"邮件发送失败 - SMTP 错误: {e}")
        raise
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        raise
