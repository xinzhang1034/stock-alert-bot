"""
邮件发送模块 - 适配 Hotmail/Outlook SMTP
"""
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp-mail.outlook.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")  # Outlook 需使用"应用密码"
RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL")


def build_html_report(stocks: list, circuit_breaker: bool, yesterday_review: str = "") -> str:
    today = datetime.now().strftime("%Y-%m-%d")

    if circuit_breaker:
        return f"""
        <h2>【{today}】今日A股短线关注提示</h2>
        <p style="color:red; font-weight:bold;">⚠️ 大盘出现异常波动，今日不建议主动操作，请观望为主。</p>
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
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"【A股短线助手】{datetime.now().strftime('%Y-%m-%d')} 今日关注标的"
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECEIVER_EMAIL
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    # Outlook/Hotmail 使用 587 端口 + STARTTLS
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, RECEIVER_EMAIL, msg.as_string())
    print("邮件发送成功")
