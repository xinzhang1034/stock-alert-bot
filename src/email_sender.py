"""
邮件发送模块 - 适配 Hotmail/Outlook SMTP
支持响应式 HTML 邮件模板
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
    """构建响应式 HTML 报告 - 手机友好版本"""
    today = datetime.now().strftime("%Y-%m-%d")

    # 通用样式
    base_styles = """
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', 'Oxygen', 'Ubuntu', 'Cantarell', sans-serif;
            background-color: #f5f5f5;
            color: #333;
            line-height: 1.6;
        }
        .container { 
            max-width: 600px;
            margin: 0 auto;
            background-color: #ffffff;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 24px;
            text-align: center;
        }
        .header h1 {
            font-size: 24px;
            margin-bottom: 8px;
        }
        .header .date {
            font-size: 14px;
            opacity: 0.9;
        }
        .content {
            padding: 24px;
        }
        .alert {
            background-color: #fff3cd;
            border-left: 4px solid #ff6b6b;
            padding: 16px;
            margin-bottom: 20px;
            border-radius: 4px;
            color: #856404;
        }
        .alert.danger {
            background-color: #f8d7da;
            color: #721c24;
        }
        .stock-card {
            background-color: #f9f9f9;
            border: 1px solid #e0e0e0;
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 12px;
            transition: all 0.3s ease;
        }
        .stock-card:hover {
            border-color: #667eea;
            box-shadow: 0 4px 8px rgba(102, 126, 234, 0.1);
        }
        .stock-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
            border-bottom: 2px solid #667eea;
            padding-bottom: 8px;
        }
        .stock-name {
            font-size: 18px;
            font-weight: bold;
            color: #667eea;
        }
        .stock-code {
            color: #999;
            font-size: 12px;
        }
        .reason {
            font-size: 14px;
            color: #555;
            margin-bottom: 12px;
            padding: 8px;
            background-color: #fff;
            border-left: 3px solid #667eea;
            border-radius: 2px;
        }
        .metrics {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
            margin-bottom: 12px;
        }
        .metric {
            background-color: #fff;
            padding: 10px;
            border-radius: 4px;
            text-align: center;
            border: 1px solid #e0e0e0;
        }
        .metric-label {
            font-size: 11px;
            color: #999;
            text-transform: uppercase;
            margin-bottom: 4px;
        }
        .metric-value {
            font-size: 16px;
            font-weight: bold;
            color: #667eea;
        }
        .risk-high { color: #ff6b6b; }
        .risk-medium { color: #ffa94d; }
        .risk-low { color: #51cf66; }
        .risk-level {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: bold;
            margin-bottom: 12px;
        }
        .risk-level.high {
            background-color: #ffe0e0;
            color: #c92a2a;
        }
        .risk-level.medium {
            background-color: #fff3e0;
            color: #e67700;
        }
        .risk-level.low {
            background-color: #e6ffed;
            color: #2f9e44;
        }
        .notes {
            font-size: 13px;
            color: #666;
            background-color: #f0f0f0;
            padding: 12px;
            border-radius: 4px;
            border-left: 3px solid #ffa94d;
        }
        .section-title {
            font-size: 18px;
            font-weight: bold;
            color: #333;
            margin-top: 24px;
            margin-bottom: 16px;
            padding-bottom: 8px;
            border-bottom: 2px solid #667eea;
        }
        .review-section {
            background-color: #f9f9f9;
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 20px;
            border-left: 4px solid #667eea;
        }
        .footer {
            background-color: #f5f5f5;
            padding: 16px 24px;
            font-size: 11px;
            color: #999;
            text-align: center;
            line-height: 1.8;
        }
        .stats {
            background-color: #f0f4ff;
            padding: 12px;
            border-radius: 4px;
            text-align: center;
            margin-bottom: 12px;
        }
        .stats-number {
            font-size: 28px;
            font-weight: bold;
            color: #667eea;
        }
        .stats-label {
            font-size: 12px;
            color: #666;
            margin-top: 4px;
        }
        @media (max-width: 600px) {
            .container { border-radius: 0; }
            .content { padding: 16px; }
            .header { padding: 16px; }
            .metrics { grid-template-columns: 1fr; }
            .header h1 { font-size: 20px; }
        }
    </style>
    """

    if circuit_breaker:
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            {base_styles}
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>📊 A股短线助手</h1>
                    <div class="date">{today}</div>
                </div>
                <div class="content">
                    <div class="alert danger">
                        <strong>⚠️ 市场风险提示</strong><br>
                        大盘出现异常波动，今日不建议主动操作。<br>
                        <strong>建议：观望为主，控制风险</strong>
                    </div>
                    <div style="background-color: #f0f4ff; padding: 12px; border-radius: 4px; text-align: center;">
                        <div style="font-size: 14px; color: #667eea; font-weight: bold;">熔断触发 🛑</div>
                        <div style="font-size: 12px; color: #999; margin-top: 4px;">系统自动暂停推荐</div>
                    </div>
                </div>
                <div class="footer">
                    <strong>⚖️ 免责声明</strong><br>
                    本内容由自动化程序基于公开新闻和市场数据生成，仅供参考，不构成任何投资建议。<br>
                    股市有风险，投资需谨慎，请独立判断并自行承担投资决策的后果。
                </div>
            </div>
        </body>
        </html>
        """

    stocks_html = ""
    for i, s in enumerate(stocks, 1):
        risk_level = s.get('risk_level', '中')
        risk_class = 'high' if '高' in risk_level else ('low' if '低' in risk_level else 'medium')
        
        warnings = s.get("extra_warnings", [])
        warnings_text = "；".join(warnings) if warnings else "无"
        
        stocks_html += f"""
        <div class="stock-card">
            <div class="stock-header">
                <div>
                    <div class="stock-name">{i}. {s.get('stock_name', 'N/A')}</div>
                    <div class="stock-code">{s.get('stock_code', 'N/A')}</div>
                </div>
                <div class="risk-level {risk_class}">{risk_level}风险</div>
            </div>
            
            <div class="reason">
                💡 {s.get('reason', '无')}
            </div>
            
            <div class="metrics">
                <div class="metric">
                    <div class="metric-label">买入区间</div>
                    <div class="metric-value">{s.get('buy_price_range', 'N/A')}</div>
                </div>
                <div class="metric">
                    <div class="metric-label">预期收益</div>
                    <div class="metric-value" style="color: #51cf66;">+{s.get('take_profit_pct', 'N/A')}%</div>
                </div>
                <div class="metric">
                    <div class="metric-label">止损设置</div>
                    <div class="metric-value" style="color: #ff6b6b;">-{s.get('stop_loss_pct', 'N/A')}%</div>
                </div>
                <div class="metric">
                    <div class="metric-label">预期周期</div>
                    <div class="metric-value">{s.get('expected_days', 'N/A')}</div>
                </div>
            </div>
            
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">
                <div style="background-color: #f0f4ff; padding: 8px; border-radius: 4px; text-align: center;">
                    <div style="font-size: 11px; color: #999;">仓位建议</div>
                    <div style="font-size: 14px; color: #667eea; font-weight: bold; margin-top: 4px;">{s.get('position_suggestion', 'N/A')}</div>
                </div>
                <div style="background-color: #f0f4ff; padding: 8px; border-radius: 4px; text-align: center;">
                    <div style="font-size: 11px; color: #999;">主要风险</div>
                    <div style="font-size: 12px; color: #667eea; margin-top: 4px;">{s.get('risk_note', '无')}</div>
                </div>
            </div>
            
            {'<div class="notes">⚠️ 额外风险: ' + warnings_text + '</div>' if warnings_text != '无' else ''}
        </div>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        {base_styles}
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>📊 A股短线助手</h1>
                <div class="date">{today} 今日关注标的</div>
            </div>
            
            <div class="content">
                <div class="stats">
                    <div class="stats-number">{len(stocks)}</div>
                    <div class="stats-label">只精选个股</div>
                </div>
                
                {stocks_html}
                
                <div class="section-title">📈 昨日复盘</div>
                <div class="review-section">
                    {yesterday_review or '<span style="color: #999;">暂无历史数据</span>'}
                </div>
            </div>
            
            <div class="footer">
                <strong>⚖️ 免责声明</strong><br>
                本内容由自动化程序基于公开新闻和市场数据生成，仅供参考，不构成任何投资建议。<br>
                股市有风险，投资需谨慎，请独立判断并自行承担投资决策的后果。
            </div>
        </div>
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
