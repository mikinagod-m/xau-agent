"""XAU agent — FastAPI entrypoint.

Flow: TradingView alert() webhook -> parse -> journal -> (Claude) -> Telegram.
"""
from __future__ import annotations

import asyncio
import csv
import io
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from . import db, stats
from .analyst import analyze_entry, extract_verdict
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
            stats_ctx = await stats.stats_block(alert)
            spot = await spot_price_note()
            read = await analyze_entry(alert, stats_ctx, spot)
            await db.open_trade(alert, claude_read=read, verdict=extract_verdict(read))

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

@app.get("/trades", response_class=HTMLResponse)
async def trades(secret: str = ""):
    if secret != settings.WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="bad secret")
    rows = await db.all_trades()
    cells = "".join(
        f"<tr><td>{r['opened_at']:%a %d %b %H:%M}</td><td>{r['side']}</td>"
        f"<td>{r['grade']}</td><td>{r['setup']}</td><td>{r['regime'] or '-'}</td>"
        f"<td>{r['entry']}</td><td>{r['sl']}</td><td>{r['tp']}</td><td>{r['rr']}</td>"
        f"<td>{r['outcome'] or 'OPEN'}</td>"
        f"<td>{r['verdict'] or '-'}</td>"
        f"<td>{(r['claude_read'] or '').splitlines()[0][:90] if r['claude_read'] else ''}</td></tr>"
        for r in rows
    )
    return f"""<html><head><title>XAU Agent Journal</title><style>
    body{{font-family:monospace;background:#111;color:#ddd;padding:16px}}
    table{{border-collapse:collapse;width:100%}}
    td,th{{border:1px solid #333;padding:4px 8px;font-size:12px}}
    th{{background:#1b1b1b;text-align:left}}tr:nth-child(even){{background:#161616}}
    a{{color:#7ab8ff}}
    </style></head><body>
    <h2>XAU Agent — Trade Journal ({len(rows)} rows)
    <small><a href="/trades.csv?secret={secret}">download CSV</a></small></h2>
    <table><tr><th>Opened (UTC)</th><th>Dir</th><th>Grade</th><th>Setup</th><th>Regime</th>
    <th>Entry</th><th>SL</th><th>TP</th><th>RR</th><th>Result</th><th>Verdict</th><th>Claude read</th></tr>
    {cells}</table></body></html>"""


@app.get("/stats")
async def stats_page(secret: str = ""):
    """Live journal aggregates (incl. HTF alignment) as JSON — same data Claude sees."""
    if secret != settings.WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="bad secret")
    return await db.live_summary()


@app.get("/admin/delete-test-rows")
async def delete_test_rows(secret: str = ""):
    """One-off: remove the bogus test rows at entry 3345.6. Idempotent."""
    if secret != settings.WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="bad secret")
    return {"result": await db.delete_test_rows()}


@app.get("/trades.csv")
async def trades_csv(secret: str = ""):
    """Full journal export, verdict included, for offline analysis."""
    if secret != settings.WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="bad secret")
    rows = await db.all_trades(limit=5000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["life_id", "side", "grade", "setup", "regime", "entry", "sl", "tp",
                "rr", "opened_at", "outcome", "closed_at", "verdict", "claude_read"])
    for r in rows:
        w.writerow([
            r["life_id"], r["side"], r["grade"], r["setup"], r["regime"],
            r["entry"], r["sl"], r["tp"], r["rr"],
            r["opened_at"].isoformat() if r["opened_at"] else "",
            r["outcome"] or "OPEN",
            r["closed_at"].isoformat() if r["closed_at"] else "",
            r["verdict"] or "",
            r["claude_read"] or "",
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="trades.csv"'},
    )