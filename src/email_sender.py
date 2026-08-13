"""
邮件发送模块 - 适配 Hotmail/Outlook SMTP
支持响应式 HTML 邮件模板，嵌入每只股票最近 7 日 K 线（inline image）并在邮件中插入昨日复盘表格。
"""
import os
import smtplib
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from datetime import datetime

# 添加 src 到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config, logger
from plotter import plot_last_7days_kline
from recap import generate_recap_for_date

SMTP_SERVER = Config.SMTP_SERVER
SMTP_PORT = Config.SMTP_PORT
SENDER_EMAIL = Config.SENDER_EMAIL
SENDER_PASSWORD = Config.SENDER_PASSWORD
# 允许同时发送到多个收件人
RECEIVER_EMAILS = [email.strip() for email in os.getenv("RECEIVER_EMAILS", ",").split(",") if email.strip()]
# 兼容单个 RECEIVER_EMAIL 环境变量
if not RECEIVER_EMAILS and Config.RECEIVER_EMAIL:
    RECEIVER_EMAILS = [Config.RECEIVER_EMAIL]

# 输出 outbox 用于存储生成的邮件 HTML（便于审阅）
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
OUTBOX_DIR = os.path.join(ROOT, "data", "outbox")
os.makedirs(OUTBOX_DIR, exist_ok=True)


def build_html_report(stocks: list, circuit_breaker: bool, yesterday_review: str = "") -> tuple[str, dict]:
    """构建响应式 HTML 报告 - 手机友好版本

    返回 (html_string, images) 其中 images 是 dict mapping cid -> image bytes
    """
    today = datetime.now().strftime("%Y-%m-%d")

    # 通用样式（略微调整：metrics 三列）
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
            max-width: 700px;
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
        .header h1 { font-size: 24px; margin-bottom: 8px; }
        .header .date { font-size: 14px; opacity: 0.9; }
        .content { padding: 24px; }
        .alert { background-color: #fff3cd; border-left: 4px solid #ff6b6b; padding: 16px; margin-bottom: 20px; border-radius: 4px; color: #856404; }
        .stock-card { background-color: #f9f9f9; border: 1px solid #e0e0e0; border-radius: 8px; padding: 16px; margin-bottom: 12px; }
        .stock-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; border-bottom: 2px solid #667eea; padding-bottom: 8px; }
        .stock-name { font-size: 18px; font-weight: bold; color: #667eea; }
        .stock-code { color: #999; font-size: 12px; }
        .reason { font-size: 14px; color: #555; margin-bottom: 12px; padding: 8px; background-color: #fff; border-left: 3px solid #667eea; border-radius: 2px; }
        .metrics { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 12px; }
        .metric { background-color: #fff; padding: 10px; border-radius: 4px; text-align: center; border: 1px solid #e0e0e0; }
        .metric-label { font-size: 11px; color: #999; text-transform: uppercase; margin-bottom: 4px; }
        .metric-value { font-size: 16px; font-weight: bold; color: #667eea; }
        .notes { font-size: 13px; color: #666; background-color: #f0f0f0; padding: 12px; border-radius: 4px; border-left: 3px solid #ffa94d; }
        .section-title { font-size: 18px; font-weight: bold; color: #333; margin-top: 24px; margin-bottom: 16px; padding-bottom: 8px; border-bottom: 2px solid #667eea; }
        .review-section { background-color: #f9f9f9; border-radius: 8px; padding: 16px; margin-bottom: 20px; border-left: 4px solid #667eea; }
        .footer { background-color: #f5f5f5; padding: 16px 24px; font-size: 11px; color: #999; text-align: center; line-height: 1.8; }
        @media (max-width: 800px) { .metrics { grid-template-columns: repeat(2,1fr); } }
        @media (max-width: 480px) { .metrics { grid-template-columns: 1fr; } .container { border-radius:0 } }
    </style>
    """

    images = {}

    if circuit_breaker:
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset=\"UTF-8\">
            <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
            {base_styles}
        </head>
        <body>
            <div class=\"container\">
                <div class=\"header\">
                    <h1>📊 A股短线助手</h1>
                    <div class=\"date\">{today}</div>
                </div>
                <div class=\"content\">
                    <div class=\"alert danger\">⚠️ 大盘出现异常波动，今日不建议主动操作。</div>
                </div>
                <div class=\"footer\">本内容仅供参考，不构成投资建议。</div>
            </div>
        </body>
        </html>
        """
        return html, images

    stocks_html = ""
    for i, s in enumerate(stocks, 1):
        risk_level = s.get('risk_level', '中')
        risk_class = 'high' if '高' in risk_level else ('low' if '低' in risk_level else 'medium')
        warnings = s.get('extra_warnings', [])
        warnings_text = '；'.join(warnings) if warnings else '无'

        # 尝试生成/查找 K 线图并嵌入
        img_tag = ''
        cid = None
        try:
            code = s.get('stock_code')
            img_path = plot_last_7days_kline(code) if code else None
            if img_path:
                # 生成唯一 cid
                cid = f"img_{i}_{code}".replace('.', '_')
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', img_path), 'rb') as f:
                    img_bytes = f.read()
                images[cid] = img_bytes
                img_tag = f'<img src="cid:{cid}" alt="{code} kline" style="max-width:100%;height:auto;border:1px solid #e0e0e0;border-radius:4px;margin-top:8px;"/>'
            else:
                img_tag = '<div style="color:#999;font-size:12px;margin-top:8px;">K线图不可用</div>'
        except Exception as e:
            logger.warning(f"嵌入 K 线图失败: {e}")
            img_tag = '<div style="color:#999;font-size:12px;margin-top:8px;">K线图生成失败</div>'

        stocks_html += f"""
        <div class=\"stock-card\">
            <div class=\"stock-header\">
                <div>
                    <div class=\"stock-name\">{i}. {s.get('stock_name', 'N/A')}</div>
                    <div class=\"stock-code\">{s.get('stock_code', 'N/A')}</div>
                </div>
                <div class=\"risk-level {risk_class}\">{risk_level}风险</div>
            </div>
            <div class=\"reason\">💡 {s.get('reason', '无')}</div>
            <div class=\"metrics\">
                <div class=\"metric\"><div class=\"metric-label\">买入区间</div><div class=\"metric-value\">{s.get('buy_price_range', 'N/A')}</div></div>
                <div class=\"metric\"><div class=\"metric-label\">预期收益</div><div class=\"metric-value\" style=\"color:#51cf66\">+{s.get('take_profit_pct', 'N/A')}%</div></div>
                <div class=\"metric\"><div class=\"metric-label\">止损设置</div><div class=\"metric-value\" style=\"color:#ff6b6b\">-{s.get('stop_loss_pct', 'N/A')}%</div></div>
                <div class=\"metric\"><div class=\"metric-label\">预期周期</div><div class=\"metric-value\">{s.get('expected_days', 'N/A')}</div></div>
                <div class=\"metric\"><div class=\"metric-label\">仓位建议</div><div class=\"metric-value\">{s.get('position_suggestion', 'N/A')}</div></div>
                <div class=\"metric\"><div class=\"metric-label\">主要风险</div><div class=\"metric-value\">{s.get('risk_note', '无')}</div></div>
            </div>
            {img_tag}
            {('<div class=\"notes\">⚠️ 额外风险: ' + warnings_text + '</div>') if warnings_text != '无' else ''}
        </div>
        """

    # 昨日复盘 HTML
    recap_html = yesterday_review or generate_recap_for_date()
    if not recap_html:
        recap_html = '<span style="color:#999;">暂无历史数据</span>'

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset=\"UTF-8\">
        <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
        {base_styles}
    </head>
    <body>
        <div class=\"container\">
            <div class=\"header\">
                <h1>📊 A股短线助手</h1>
                <div class=\"date\">{today} 今日关注标的</div>
            </div>
            <div class=\"content\">
                <div class=\"stats\" style=\"background:#f0f4ff;padding:12px;border-radius:4px;text-align:center;margin-bottom:12px;\">
                    <div style=\"font-size:28px;font-weight:bold;color:#667eea\">{len(stocks)}</div>
                    <div style=\"font-size:12px;color:#666;margin-top:4px;\">只精选个股</div>
                </div>
                {stocks_html}
                <div class=\"section-title\">📈 昨日复盘</div>
                <div class=\"review-section\">{recap_html}</div>
            </div>
            <div class=\"footer\">本内容由自动化程序生成，仅供参考，不构成投资建议。</div>
        </div>
    </body>
    </html>
    """

    return html, images


def send_email(html_content: str, images: dict):
    """发送邮件，支持内联图片（images: cid -> bytes），并把 HTML 保存到 data/outbox/"""
    try:
        # 验证必需的配置
        if not SENDER_EMAIL or not SENDER_PASSWORD or not RECEIVER_EMAILS:
            raise ValueError("邮件配置不完整：缺少 SENDER_EMAIL、SENDER_PASSWORD 或 RECEIVER_EMAILS/RECEIVER_EMAIL")

        msg = MIMEMultipart("related")
        msg_alt = MIMEMultipart("alternative")
        msg.attach(msg_alt)

        msg["Subject"] = f"【A股短线助手】{datetime.now().strftime('%Y-%m-%d')} 今日关注标的"
        msg["From"] = SENDER_EMAIL
        msg["To"] = ", ".join(RECEIVER_EMAILS)

        msg_alt.attach(MIMEText(html_content, "html", "utf-8"))

        # attach inline images
        for cid, img_bytes in images.items():
            try:
                img = MIMEImage(img_bytes)
                img.add_header('Content-ID', f'<{cid}>')
                img.add_header('Content-Disposition', 'inline', filename=f"{cid}.png")
                msg.attach(img)
            except Exception as e:
                logger.warning(f"附加图片 {cid} 失败: {e}")

        # 保存 HTML 到 outbox
        out_file = os.path.join(OUTBOX_DIR, f"email_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html")
        with open(out_file, 'w', encoding='utf-8') as f:
            f.write(html_content)
        logger.info(f"已将邮件 HTML 保存到 {out_file}")

        logger.debug(f"连接到 SMTP 服务器: {SMTP_SERVER}:{SMTP_PORT}...")
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            logger.debug(f"使用账户登录: {SENDER_EMAIL}")
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            logger.debug(f"发送邮件至: {RECEIVER_EMAILS}")
            server.sendmail(SENDER_EMAIL, RECEIVER_EMAILS, msg.as_string())

        logger.info("✓ 邮件发送成功")
        return True

    except smtplib.SMTPAuthenticationError as e:
        logger.error(f"邮件发送失败 - 认证错误: {e}")
        raise
    except smtplib.SMTPException as e:
        logger.error(f"邮件发送失败 - SMTP 错误: {e}")
        raise
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        raise
