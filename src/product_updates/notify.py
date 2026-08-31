import os
import smtplib
from email.message import EmailMessage
from decimal import Decimal

import httpx
from dotenv import load_dotenv

from .models import Change

load_dotenv()

def _money(value: Decimal | None) -> str:
    return f"₹{value:,.2f}" if value is not None else "not listed"

def render(changes: list[Change]) -> str:
    lines = ["Product price update"]
    for change in changes:
        offer = change.offer
        if change.kind == "new": detail = f"New listing at {_money(offer.price)}"
        elif change.kind == "price": detail = f"{_money(change.previous_price)} → {_money(offer.price)}"
        else: detail = f"Availability: {'in stock' if offer.available else 'out of stock'}"
        lines.extend([f"{offer.retailer}: {offer.title}", detail, offer.url, ""])
    return "\n".join(lines)

def send(message: str) -> list[str]:
    sent = []
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        httpx.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True}, timeout=20).raise_for_status()
        sent.append("telegram")
    webhook = os.getenv("WEBHOOK_URL")
    if webhook:
        httpx.post(webhook, json={"text": message}, timeout=20).raise_for_status(); sent.append("webhook")
    host, recipient = os.getenv("SMTP_HOST"), os.getenv("SMTP_TO")
    if host and recipient:
        email = EmailMessage(); email["Subject"] = "Product price update"; email["From"] = os.environ["SMTP_FROM"]; email["To"] = recipient; email.set_content(message)
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587"))) as smtp:
            smtp.starttls(); smtp.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"]); smtp.send_message(email)
        sent.append("email")
    return sent
