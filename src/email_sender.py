"""
邮件发送模块 - 适配多收件人、支持内联 K 线图与复盘表格
"""
import os
import re
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

# 收件人：优先使用环境变量 RECEIVER_EMAILS（逗号分隔），兼容单个 RECEIVER_EMAIL
RECEIVER_EMAILS_RAW = os.getenv("RECEIVER_EMAILS", "") or Config.RECEIVER_EMAILS_ENV
RECEIVER_EMAIL_SINGLE = os.getenv("RECEIVER_EMAIL", "") or Config.RECEIVER_EMAIL


def parse_and_validate_recipients(raw: str, single: str) -> list:
    """解析并校验收件人字符串，返回合法的邮箱列表"""
    candidates = []
    if raw:
        # 支持逗号或分号分隔
        for part in re.split(r"[;,]", raw):
            p = part.strip()
            if p:
                candidates.append(p)
    if single and single.strip():
        candidates.append(single.strip())

    # 简单邮箱正则校验（覆盖常见情况）
    email_re = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    valid = [e for e in candidates if email_re.match(e)]
    if not valid:
        return []
    # 去重并保留顺序
    seen = set()
    out = []
    for e in valid:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out

RECEIVERS = parse_and_validate_recipients(RECEIVER_EMAILS_RAW, RECEIVER_EMAIL_SINGLE)


def build_html_report(stocks: list, circuit_breaker: bool, yesterday_review: str = "") -> tuple[str, dict]:
    """构建响应式 HTML 报告 - 返回 (html, images)"""
    today = datetime.now().strftime("%Y-%m-%d")

    base_styles = """
    <style>
      /* 略：样式与之前一致，保持邮件美观 */
      body{font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial}
      .container{max-width:700px;margin:0 auto;background:#fff;border-radius:8px;padding:16px}
    </style>
    """

    images = {}

    if circuit_breaker:
        html = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">{base_styles}</head>
        <body><div class=\"container\"><h1>📊 A股短线助手</h1><p>熔断触发，今日暂停推荐</p></div></body></html>
        """
        return html, images

    stocks_html = ""
    for i, s in enumerate(stocks, 1):
        # 生成/嵌入 k 线
        code = s.get('stock_code')
        img_tag = ''
        cid = None
        if code:
            img_path = plot_last_7days_kline(code)
            if img_path:
                cid = f"img_{i}_{code}".replace('.', '_')
                try:
                    img_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', img_path)
                    with open(img_file, 'rb') as f:
                        img_bytes = f.read()
                    images[cid] = img_bytes
                    img_tag = f'<img src="cid:{cid}" alt="{code} kline" style="max-width:100%;height:auto;border:1px solid #e0e0e0;border-radius:4px;margin-top:8px;"/>'
                except Exception as e:
                    logger.warning(f"读取 K 线图失败: {e}")
                    img_tag = '<div style="color:#999;font-size:12px;margin-top:8px;">K线图不可用</div>'
            else:
                img_tag = '<div style="color:#999;font-size:12px;margin-top:8px;">K线图不可用</div>'

        warnings = s.get('extra_warnings', [])
        warnings_text = '；'.join(warnings) if warnings else '无'

        stocks_html += f"""
        <div style=\"border:1px solid #eaeaea;padding:12px;border-radius:8px;margin-bottom:12px\">
          <div style=\"display:flex;justify-content:space-between;align-items:center;\"><div><strong>{i}. {s.get('stock_name','N/A')}</strong><div style=\"color:#666;font-size:12px\">{s.get('stock_code','')}</div></div><div style=\"color:#e55353;font-weight:bold\">{s.get('risk_level','中')}风险</div></div>
          <div style=\"margin-top:8px;color:#333\">{s.get('reason','')}</div>
          <div style=\"display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:8px\">
            <div style=\"background:#fff;padding:6px;border-radius:4px;border:1px solid #eee;text-align:center\"><div style=\"font-size:11px;color:#999\">买入区间</div><div style=\"font-weight:bold;color:#2b6cb0\">{s.get('buy_price_range','-')}</div></div>
            <div style=\"background:#fff;padding:6px;border-radius:4px;border:1px solid #eee;text-align:center\"><div style=\"font-size:11px;color:#999\">预期收益</div><div style=\"font-weight:bold;color:#2b6cb0\">+{s.get('take_profit_pct','-')}%</div></div>
            <div style=\"background:#fff;padding:6px;border-radius:4px;border:1px solid #eee;text-align:center\"><div style=\"font-size:11px;color:#999\">止损</div><div style=\"font-weight:bold;color:#e53e3e\">-{s.get('stop_loss_pct','-')}%</div></div>
          </div>
          {img_tag}
          {('<div style=\"margin-top:8px;background:#fff3cd;padding:8px;border-radius:4px\">⚠️ 额外风险: ' + warnings_text + '</div>') if warnings_text!='无' else ''}
        </div>
        """

    recap_html = yesterday_review or generate_recap_for_date()
    if not recap_html:
        recap_html = '<span style="color:#999">暂无历史数据</span>'

    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">{base_styles}</head>
    <body><div class=\"container\"><h1>📊 A股短线助手</h1><div style=\"margin-top:12px\">今日关注: {len(stocks)} 只</div>{stocks_html}<h3>昨日复盘</h3><div>{recap_html}</div><div style=\"margin-top:12px;color:#999;font-size:12px\">本内容仅供参考，不构成投资建议</div></div></body></html>
    """

    return html, images


def send_email(html_content: str, images: dict | None = None) -> bool:
    """发送邮件，images: cid->bytes，用于内联图片"""
    try:
        if not SENDER_EMAIL or not SENDER_PASSWORD:
            raise ValueError("邮件配置不完整：缺少 SENDER_EMAIL 或 SENDER_PASSWORD")

        # 校验收件人
        if not RECEIVERS:
            raise ValueError("没有可用的收件人，请设置 RECEIVER_EMAIL 或 RECEIVER_EMAILS")

        msg = MIMEMultipart('related')
        alt = MIMEMultipart('alternative')
        msg.attach(alt)
        msg['Subject'] = f"【A股短线助手】{datetime.now().strftime('%Y-%m-%d')} 今日关注标的"
        msg['From'] = SENDER_EMAIL
        msg['To'] = ', '.join(RECEIVERS)
        alt.attach(MIMEText(html_content, 'html', 'utf-8'))

        # attach images
        if images:
            for cid, b in images.items():
                try:
                    img = MIMEImage(b)
                    img.add_header('Content-ID', f'<{cid}>')
                    img.add_header('Content-Disposition', 'inline', filename=f"{cid}.png")
                    msg.attach(img)
                except Exception as e:
                    logger.warning(f"附加图片失败: {e}")

        # 保存到 outbox
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'outbox')
        os.makedirs(out_dir, exist_ok=True)
        out_file = os.path.join(out_dir, f"email_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html")
        with open(out_file, 'w', encoding='utf-8') as f:
            f.write(html_content)
        logger.info(f"已保存邮件 HTML 到 {out_file}")

        # send via SMTP
        logger.debug(f"连接到 SMTP {SMTP_SERVER}:{SMTP_PORT}")
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            logger.debug(f"发送邮件至: {RECEIVERS}")
            server.sendmail(SENDER_EMAIL, RECEIVERS, msg.as_string())

        logger.info("邮件发送成功")
        return True

    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        raise
