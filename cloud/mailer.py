"""发信 —— SMTP（服务器、账号、发信地址都在 secrets.env 里配）。

⚠️ 端口只能 465(SSL) 或 587(STARTTLS)：**很多云服务器默认封 25 端口**，填 25 会直接超时。

发信走线程池（smtplib 是同步阻塞的），别把事件循环卡住。
"""

import asyncio
import logging
import smtplib
import ssl
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr, formatdate

import config

logger = logging.getLogger("octopus.cloud.mailer")


class MailError(RuntimeError):
    pass


def _send_sync(to: str, subject: str, body: str) -> None:
    ctx = ssl.create_default_context()
    if config.SMTP_PORT == 465:
        server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=ctx, timeout=25)
    else:
        server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=25)
        server.starttls(context=ctx)
    try:
        server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = formataddr((str(Header(config.SMTP_FROM_NAME, "utf-8")), config.SMTP_USER))
        msg["To"] = to
        msg["Subject"] = Header(subject, "utf-8")
        msg["Date"] = formatdate(localtime=True)
        server.sendmail(config.SMTP_USER, [to], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass


async def send(to: str, subject: str, body: str) -> None:
    if not config.smtp_configured():
        raise MailError("SMTP 未配置（/octopus/config/secrets.env 缺 SMTP_USER/SMTP_PASSWORD）")
    try:
        await asyncio.to_thread(_send_sync, to, subject, body)
    except Exception as e:
        logger.warning("发信失败 to=%s: %s", to, e)
        raise MailError(f"邮件发送失败: {type(e).__name__}") from e


async def send_code(to: str, code: str, minutes: int) -> None:
    body = (
        f"你的 Octopus 验证码是：{code}\n\n"
        f"{minutes} 分钟内有效，在 Octopus 客户端输入即可完成验证。\n\n"
        f"如果不是你本人操作，忽略这封邮件即可 —— 没有验证码，别人无法登录你的账号。\n\n"
        f"-- Octopus"
    )
    await send(to, f"Octopus 验证码 {code}", body)
