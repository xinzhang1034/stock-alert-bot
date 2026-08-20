"""
邮件发送模块 - 增强：嵌入 7 天 K 线图（base64）、更紧凑的指标布局与昨日复盘 HTML 渲染

修复记录：
- build_html_report：所有来自 LLM/涨停池补充的自由文本字段（stock_name/stock_code/
  reason/risk_note/position_suggestion/warnings 等）原来直接拼进 HTML，没有做任何转义。
  这些内容最终源头是网络抓取的新闻/公告文本，经由 LLM 转述后仍可能带入 HTML 标签甚至
  链接（无论是模型"复述"了网页里的标签，还是极端情况下的 prompt injection），未转义直接
  拼进邮件正文存在 HTML 注入风险（邮件客户端里显示错乱、伪造链接/按钮等）。新增 _safe()
  辅助函数，统一做 html.escape() 转义
- build_html_report：_safe() 同时把 None 值当成"缺失"处理，返回默认占位符。主流程模块
  那边把补充候选（涨停池/龙虎榜兜底）的数值字段占位符从字符串 'N/A' 改成了 None，如果这里
  还按 `s.get(key, 'N/A')` 取值，key 存在但值是 None 时 .get() 的默认值不会生效，会直接
  显示成"+None%"这种难看的文本，_safe() 一并修掉这个问题
- send_email：Subject 直接赋值中文字符串，在部分环境/邮件客户端下可能因为没有按 RFC 2047
  编码而出现头部乱码甚至 UnicodeEncodeError，改用 email.header.Header 显式按 utf-8 编码
- send_email：原来只发一次，网络类/SMTP 临时故障（连接超时、服务器临时拒绝等）不会重试；
  改成给这类可重试的错误加了 2 次重试，认证错误（密码/账号错误）不去重试，直接失败

优化（第二轮）：
- 新增 build_plain_text_summary + send_email 支持传入纯文本内容：multipart/alternative
  原本只有 html 一个 part，不符合 MIME 惯例（应该至少有 plain+html 两个版本），
  也会让部分垃圾邮件过滤器对"只有 HTML 没有纯文本"的邮件降权
- send_email：补上显式的 Date/Message-ID 头，提升送达率/避免被判为垃圾邮件
- send_email：RECEIVER_EMAIL 支持逗号/分号分隔的多个收件人（原来会把整个字符串当成 to_addrs
  传给 sendmail，如果里面有逗号会被当成一个不合法地址）
- send_email：starttls 显式传 ssl.create_default_context()，不依赖 Python 版本默认行为
- send_email：拼好邮件后先估算一下总体积，过大时记一条 warning（base64 内嵌图片容易把邮件
  撑大，部分邮件客户端会截断过大的正文）
- build_html_report：新增两个小提示：补充候选（is_supplement）占比过高时在统计区下方加一条
  醒目提醒；每张股票卡片若带 consecutive_days（来自 main 模块）且 >1，显示"连续第N天上榜"徽章
"""
import os
import ssl
import sys
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formatdate, make_msgid
from html import escape as html_escape
from datetime import datetime

# 在无图形环境下使用 Agg 后端
try:
    import matplotlib
    matplotlib.use("Agg")
except Exception:
    pass

# 添加 src 到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config, logger

SMTP_SERVER = Config.SMTP_SERVER
SMTP_PORT = Config.SMTP_PORT
SENDER_EMAIL = Config.SENDER_EMAIL
SENDER_PASSWORD = Config.SENDER_PASSWORD
RECEIVER_EMAIL = Config.RECEIVER_EMAIL

MAX_SEND_RETRIES = 2
SEND_RETRY_DELAY = 3
MAX_EMAIL_SIZE_WARN_BYTES = 300 * 1024  # 超过这个大小只警告，不阻止发送


def _safe(value, default: str = "N/A") -> str:
    """把可能是 None/缺失的字段统一转成占位符，并做 HTML 转义
    （内容源头是网络新闻/LLM 转述，不能保证不含 HTML 特殊字符，直接拼接有注入风险）"""
    if value is None or value == "":
        value = default
    return html_escape(str(value))


def build_html_report(stocks: list, circuit_breaker: bool, yesterday_review: str = "") -> str:
    """构建响应式 HTML 报告 - 手机友好版本，支持嵌入k线图（base64）和复盘HTML"""
    today = datetime.now().strftime("%Y-%m-%d")

    # 通用样式（保持与之前一致，不过 metrics 改为 3 列桌面视图）
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
            max-width: 680px;
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
        .content { padding: 18px; }
        .alert { background-color: #fff3cd; border-left: 4px solid #ff6b6b; padding: 16px; margin-bottom: 20px; border-radius: 4px; color: #856404; }
        .alert.danger { background-color: #f8d7da; color: #721c24; }
        .stock-card { background-color: #f9f9f9; border: 1px solid #e0e0e0; border-radius: 8px; padding: 12px; margin-bottom: 12px; }
        .stock-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; border-bottom: 2px solid #667eea; padding-bottom: 6px; }
        .stock-name { font-size: 17px; font-weight: bold; color: #667eea; }
        .stock-code { color: #999; font-size: 12px; }
        .reason { font-size: 14px; color: #555; margin-bottom: 8px; padding: 6px; background-color: #fff; border-left: 3px solid #667eea; border-radius: 2px; }
        .metrics { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-bottom: 10px; }
        .metric { background-color: #fff; padding: 8px; border-radius: 4px; text-align: center; border: 1px solid #e0e0e0; }
        .metric-label { font-size: 11px; color: #999; margin-bottom: 4px; }
        .metric-value { font-size: 15px; font-weight: bold; color: #667eea; }
        .risk-level { display: inline-block; padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: bold; margin-bottom: 8px; }
        .risk-level.high { background-color: #ffe0e0; color: #c92a2a; }
        .risk-level.medium { background-color: #fff3e0; color: #e67700; }
        .risk-level.low { background-color: #e6ffed; color: #2f9e44; }
        .notes { font-size: 13px; color: #666; background-color: #f0f0f0; padding: 10px; border-radius: 4px; border-left: 3px solid #ffa94d; }
        .section-title { font-size: 18px; font-weight: bold; color: #333; margin-top: 18px; margin-bottom: 10px; padding-bottom: 6px; border-bottom: 2px solid #667eea; }
        .review-section { background-color: #f9f9f9; border-radius: 8px; padding: 12px; margin-bottom: 14px; border-left: 4px solid #667eea; }
        .footer { background-color: #f5f5f5; padding: 12px 18px; font-size: 11px; color: #999; text-align: center; line-height: 1.6; }
        .stats { background-color: #f0f4ff; padding: 10px; border-radius: 4px; text-align: center; margin-bottom: 12px; }
        .stats-number { font-size: 26px; font-weight: bold; color: #667eea; }
        .stats-label { font-size: 12px; color: #666; margin-top: 4px; }
        @media (max-width: 620px) { .metrics { grid-template-columns: 1fr; } .container { border-radius: 0; } }
    </style>
    """

    if circuit_breaker:
        return f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">{base_styles}</head>
        <body>
        <div class="container"><div class="header"><h1>📊 A股短线助手</h1><div class="date">{today}</div></div>
        <div class="content"><div class="alert danger"><strong>⚠️ 市场风险提示</strong><br>大盘出现异常波动，今日不建议主动操作。<br><strong>建议：观望为主，控制风险</strong></div>
        <div style="background-color:#f0f4ff;padding:12px;border-radius:4px;text-align:center;"><div style="font-size:14px;color:#667eea;font-weight:bold;">熔断触发 🛑</div><div style="font-size:12px;color:#999;margin-top:4px;">系统自动暂停推荐</div></div>
        </div>
        <div class="footer"><strong>⚖️ 免责声明</strong><br>本内容仅供参考，不构成投资建议。</div></div></body></html>
        """

    stocks_html = ""
    supplement_count = sum(1 for s in stocks if s.get('is_supplement'))
    supplement_banner = ""
    if supplement_count > 0:
        supplement_banner = (
            f'<div style="background-color:#fff3cd;color:#856404;padding:8px;border-radius:4px;'
            f'text-align:center;font-size:12px;margin-bottom:10px;">'
            f'⚠️ 其中 {supplement_count}/{len(stocks)} 只为系统自动补充（未经 LLM 分析），请谨慎参考</div>'
        )

    for i, s in enumerate(stocks, 1):
        risk_level = s.get('risk_level') or '中'
        risk_class = 'high' if '高' in risk_level else ('low' if '低' in risk_level else 'medium')
        warnings = s.get("extra_warnings", [])
        warnings_text = "；".join(html_escape(str(w)) for w in warnings) if warnings else "无"

        consecutive_days = s.get('consecutive_days')
        streak_badge = (
            f'<span style="font-size:11px;color:#e67700;margin-left:6px;">🔁连续{consecutive_days}天上榜</span>'
            if consecutive_days and consecutive_days > 1 else ''
        )

        kline_html = ''
        if s.get('kline_7d_base64'):
            # base64 字母表本身不含 " < > 等字符，可以安全地直接拼进 src 属性
            kline_html = f"<img src=\"data:image/png;base64,{s.get('kline_7d_base64')}\" alt=\"kline\" style=\"width:200px;height:120px;border-radius:6px;\"/>"
        else:
            kline_html = '<div style="font-size:12px;color:#999;">K线图不可用</div>'

        stocks_html += f"""
        <div class="stock-card">
            <div class="stock-header">
                <div><div class="stock-name">{i}. {_safe(s.get('stock_name'))}{streak_badge}</div><div class="stock-code">{_safe(s.get('stock_code'))}</div></div>
                <div class="risk-level {risk_class}">{_safe(risk_level)}风险</div>
            </div>

            <div class="reason">💡 {_safe(s.get('reason'), '无')}</div>

            <div class="metrics">
                <div class="metric"><div class="metric-label">买入区间</div><div class="metric-value">{_safe(s.get('buy_price_range'))}</div></div>
                <div class="metric"><div class="metric-label">预期收益</div><div class="metric-value" style="color:#51cf66;">+{_safe(s.get('take_profit_pct'))}%</div></div>
                <div class="metric"><div class="metric-label">止损设置</div><div class="metric-value" style="color:#ff6b6b;">-{_safe(s.get('stop_loss_pct'))}%</div></div>
            </div>

            <div style="display:flex;gap:12px;align-items:center;margin-top:8px;">
                <div style="flex:1;min-width:120px;">
                    <div style="background-color:#f0f4ff;padding:8px;border-radius:4px;text-align:center;"><div style="font-size:11px;color:#999;">仓位建议</div><div style="font-size:14px;color:#667eea;font-weight:bold;margin-top:4px;">{_safe(s.get('position_suggestion'))}</div></div>
                </div>
                <div style="flex:1;min-width:120px;">
                    <div style="background-color:#f0f4ff;padding:8px;border-radius:4px;text-align:center;"><div style="font-size:12px;color:#667eea;margin-top:4px;">{_safe(s.get('risk_note'), '无')}</div></div>
                </div>
                <div style="width:200px;text-align:center;">{kline_html}</div>
            </div>

            {('<div class="notes">⚠️ 额外风险: ' + warnings_text + '</div>') if warnings_text != '无' else ''}
        </div>
        """

    # 昨日复盘：接收HTML（若无则展示提示，该 HTML 由 main.get_yesterday_review 生成，
    # 需要在那边对拼进去的股票名称/代码做转义，这里直接信任并原样插入，不能再转义一次）
    yesterday_html = yesterday_review or '<span style="color: #999;">暂无历史数据</span>'

    html_report = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">{base_styles}</head>
    <body>
    <div class="container">
        <div class="header"><h1>📊 A股短线助手</h1><div class="date">{today} 今日关注标的</div></div>
        <div class="content">
            <div class="stats"><div class="stats-number">{len(stocks)}</div><div class="stats-label">只精选个股</div></div>
            {supplement_banner}
            {stocks_html}
            <div class="section-title">📈 昨日复盘</div>
            <div class="review-section">{yesterday_html}</div>
        </div>
        <div class="footer"><strong>⚖️ 免责声明</strong><br>本内容由自动化程序基于公开新闻和市场数据生成，仅供参考，不构成任何投资建议。股市有风险，投资需谨慎。</div>
    </div>
    </body>
    </html>
    """
    return html_report


def build_plain_text_summary(stocks: list, circuit_breaker: bool) -> str:
    """给不支持/不渲染 HTML 的邮件客户端提供纯文本兜底内容"""
    if circuit_breaker:
        return "【A股短线助手】大盘出现异常波动，已触发熔断，今日不建议操作。详情请查看 HTML 版本。"
    if not stocks:
        return "【A股短线助手】今日无推荐标的。详情请查看 HTML 版本。"

    lines = [f"【A股短线助手】今日推荐 {len(stocks)} 只标的："]
    for i, s in enumerate(stocks, 1):
        tag = "（系统补充，未经LLM分析）" if s.get("is_supplement") else ""
        lines.append(
            f"{i}. {s.get('stock_name', 'N/A')}({s.get('stock_code', 'N/A')}){tag} - {s.get('reason', '')}"
        )
    lines.append("详情、指标及K线图请查看 HTML 版本。本内容仅供参考，不构成任何投资建议。")
    return "\n".join(lines)


def _parse_recipients(raw: str) -> list:
    """支持用逗号或分号分隔多个收件人"""
    if not raw:
        return []
    normalized = raw.replace(";", ",")
    return [addr.strip() for addr in normalized.split(",") if addr.strip()]


def send_email(html_content: str, plain_text_content: str = None):
    """发送邮件（网络/SMTP 临时故障会重试；认证错误重试无意义，直接失败）"""
    recipients = _parse_recipients(RECEIVER_EMAIL)
    if not SENDER_EMAIL or not SENDER_PASSWORD or not recipients:
        raise ValueError("邮件配置不完整：缺少 SENDER_EMAIL、SENDER_PASSWORD 或 RECEIVER_EMAIL")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(f"【A股短线助手】{datetime.now().strftime('%Y-%m-%d')} 今日关注标的", "utf-8")
    msg["From"] = SENDER_EMAIL
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    # multipart/alternative 约定：更简单的版本在前，最优先展示的版本放最后
    msg.attach(MIMEText(
        plain_text_content or "本邮件包含 HTML 内容，请使用支持 HTML 的邮件客户端查看。",
        "plain", "utf-8"
    ))
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    raw_message = msg.as_string()
    size = len(raw_message.encode("utf-8"))
    if size > MAX_EMAIL_SIZE_WARN_BYTES:
        logger.warning(f"邮件正文较大（约 {size // 1024} KB），部分邮件客户端可能会截断显示")

    last_error = None
    for attempt in range(MAX_SEND_RETRIES):
        try:
            logger.debug(f"连接到 SMTP 服务器: {SMTP_SERVER}:{SMTP_PORT}... (尝试 {attempt + 1}/{MAX_SEND_RETRIES})")

            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                logger.debug(f"使用账户登录: {SENDER_EMAIL}")
                server.login(SENDER_EMAIL, SENDER_PASSWORD)
                logger.debug(f"发送邮件至: {recipients}")
                server.sendmail(SENDER_EMAIL, recipients, raw_message)

            logger.info("✓ 邮件发送成功")
            return

        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"邮件发送失败 - 认证错误: {e}")
            logger.error("请检查 SENDER_EMAIL 和 SENDER_PASSWORD 是否正确")
            raise
        except (smtplib.SMTPException, OSError) as e:
            last_error = e
            logger.warning(f"邮件发送失败，将重试 (尝试 {attempt + 1}/{MAX_SEND_RETRIES}): {e}")
            if attempt < MAX_SEND_RETRIES - 1:
                time.sleep(SEND_RETRY_DELAY)
        except Exception as e:
            logger.error(f"邮件发送失败: {e}")
            raise

    logger.error(f"邮件发送在 {MAX_SEND_RETRIES} 次尝试后仍然失败: {last_error}")
    raise last_error
