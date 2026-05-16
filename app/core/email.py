import random
from email.mime.text import MIMEText

import aiosmtplib

from app.core.settings import settings


def generate_code() -> str:
    return str(random.randint(100000, 999999))


async def send_verification_email(to_email: str, code: str) -> None:
    msg = MIMEText(f"인증번호: {code}\n\n15분 내에 입력해주세요.")
    msg["Subject"] = "[Edge있조] 이메일 인증번호"
    msg["From"] = settings.smtp_user
    msg["To"] = to_email

    await aiosmtplib.send(
        msg,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_user,
        password=settings.smtp_password,
        start_tls=True,
    )
