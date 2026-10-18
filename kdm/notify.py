"""Telegram notifications (separate HTTP session – never carries Kick cookies)."""
from __future__ import annotations

import logging

from curl_cffi.requests import AsyncSession

log = logging.getLogger("kdm.notify")


async def telegram_send(token: str, chat_id: str, text: str) -> tuple[bool, str]:
    if not token or not chat_id:
        return False, "Bot token and chat ID are required"
    try:
        async with AsyncSession(timeout=15) as s:
            r = await s.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
            )
        data = r.json()
        if data.get("ok"):
            return True, "OK"
        return False, str(data.get("description", f"HTTP {r.status_code}"))
    except Exception as e:
        return False, str(e)


DISCORD_RE = __import__("re").compile(
    r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$")


def discord_valid(url: str) -> bool:
    return bool(url and DISCORD_RE.match(url.strip()))


async def discord_send(url: str, text: str) -> tuple[bool, str]:
    if not discord_valid(url):
        return False, "invalid_discord_webhook"
    try:
        async with AsyncSession(timeout=15) as s:
            r = await s.post(url, json={"content": text, "username": "Kick Drops Miner"})
        return (200 <= r.status_code < 300), f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)
