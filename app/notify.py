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


async def spot_price() -> float | None:
    """Live XAU spot (USD/oz) from metalpriceapi, or None if unavailable.
    Numeric counterpart to spot_price_note(), used by the shadow poller."""
    if not settings.METALPRICE_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.metalpriceapi.com/v1/latest",
                params={"api_key": settings.METALPRICE_API_KEY,
                        "base": "USD", "currencies": "XAU"},
            )
            xau = r.json().get("rates", {}).get("XAU")
            return round(1 / xau, 3) if xau else None
    except Exception as exc:
        print("[spot_price] failed:", exc)
        return None


async def spot_price_note() -> str:
    """Fetch live XAU spot from metalpriceapi to flag stale alerts. Optional."""
    price = await spot_price()
    return f"Live spot (metalpriceapi): {price:.2f} USD/oz" if price else ""
