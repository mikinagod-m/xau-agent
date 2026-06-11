"""Outbound Telegram messages + optional metalpriceapi spot-price sanity check."""
from __future__ import annotations

import httpx

from .config import settings


async def send_telegram(text: str, watch_channel: bool = False) -> None:
    chat_id = settings.TELEGRAM_WATCH_CHAT_ID if watch_channel else settings.TELEGRAM_CHAT_ID
    if not settings.TELEGRAM_BOT_TOKEN or not chat_id:
        print("[telegram disabled]", text[:120])
        return
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as client:
        # Plain text on purpose: alert payloads contain characters that break
        # Telegram Markdown parsing. Format with unicode + line breaks instead.
        r = await client.post(url, json={"chat_id": chat_id, "text": text})
        if r.status_code != 200:
            print("[telegram error]", r.status_code, r.text[:200])


async def spot_price_note() -> str:
    """Fetch live XAU spot from metalpriceapi to flag stale alerts. Optional."""
    if not settings.METALPRICE_API_KEY:
        return ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.metalpriceapi.com/v1/latest",
                params={"api_key": settings.METALPRICE_API_KEY,
                        "base": "USD", "currencies": "XAU"},
            )
            data = r.json()
            xau = data.get("rates", {}).get("XAU")
            if xau:
                return f"Live spot (metalpriceapi): {1 / xau:.2f} USD/oz"
    except Exception as exc:
        return f"(spot check failed: {exc})"
    return ""
