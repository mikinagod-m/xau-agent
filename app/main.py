"""XAU agent — FastAPI entrypoint.

Flow: TradingView alert() webhook -> parse -> journal -> (Claude) -> Telegram.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from . import db, stats
from .analyst import analyze_entry
from .config import settings
from .notify import send_telegram, spot_price_note
from .parser import parse_alert


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.init_db()
    yield
    await db.close_db()


app = FastAPI(title="xau-agent", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != settings.WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="bad token")

    body = (await request.body()).decode("utf-8", errors="replace")
    alert = parse_alert(body)
    await db.log_raw(alert.kind, body)

    # Respond to TradingView immediately; do the slow work in the background.
    asyncio.create_task(_handle(alert))
    return {"received": True, "kind": alert.kind}


async def _handle(alert) -> None:
    try:
        if alert.kind == "entry":
            stats_ctx = await stats.stats_block(alert.regime, alert.side)
            spot = await spot_price_note()
            read = await analyze_entry(alert, stats_ctx, spot)
            await db.open_trade(alert, claude_read=read)

            arrow = "🟢" if alert.side == "LONG" else "🔴"
            await send_telegram(
                f"{arrow} {alert.side} {alert.grade} — {alert.setup}\n"
                f"Entry {alert.entry} | SL {alert.sl} | TP {alert.tp} | {alert.rr}R\n"
                f"Regime {alert.regime} | Score {alert.score} | Sess {alert.ctx.get('Sess')}\n"
                f"{'─' * 24}\n{read}"
            )

        elif alert.kind == "lifecycle":
            row = await db.close_trade(alert.life_id, alert.event)
            icon = "✅" if alert.event == "TP HIT" else "❌" if alert.event == "SL HIT" else "⚠️"
            detail = (
                f"{row['side']} {row['grade']} {row['setup']} ({row['regime']})"
                if row else alert.life_id
            )
            await send_telegram(f"{icon} {alert.event} — {detail}")

        elif alert.kind == "watch":
            b = stats.bucket_stats(alert.regime, alert.side)
            hist = (
                f"\nHist: {b['win_rate_pct']}% WR, net {b['net_pnl_usd']:+.2f} ({b['trades']}t)"
                if b else ""
            )
            await send_telegram(
                f"👀 WATCH {alert.side} {alert.grade} {alert.setup} "
                f"Q{alert.watch_quality}% — {alert.watch_reason or 'risk gate'}\n"
                f"SL {alert.sl} | TP {alert.tp} | {alert.rr}R | Regime {alert.regime}{hist}",
                watch_channel=True,
            )

        else:
            await send_telegram(f"❓ Unparsed alert:\n{alert.raw[:500]}", watch_channel=True)

    except Exception as exc:
        # Last-resort guard: never crash silently.
        print("[handler error]", exc)
        try:
            await send_telegram(f"⚠️ Agent error handling alert: {exc}")
        except Exception:
            pass
