"""XAU agent — FastAPI entrypoint.

Flow: TradingView alert() webhook -> parse -> journal -> (Claude) -> Telegram.
"""
from __future__ import annotations

import os
import csv
import io
import hmac
import asyncio
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from . import db, execution, shadow, stats
from .analyst import analyze_entry, extract_verdict
from .config import settings
from .notify import send_telegram, spot_price_note
from .parser import parse_alert


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.init_db()
    poller = None
    if settings.SHADOW_TRACKING and settings.METALPRICE_API_KEY:
        poller = asyncio.create_task(shadow.run_shadow_poller())
    yield
    if poller:
        poller.cancel()
        try:
            await poller
        except asyncio.CancelledError:
            pass
    await db.close_db()


app = FastAPI(title="xau-agent", lifespan=lifespan)


def _secret_ok(provided: str, expected: str) -> bool:
    """Constant-time secret check that fails closed when the expected secret is
    unset (empty). Prevents the old `change-me`/blank-default bypass."""
    if not expected:
        return False
    return hmac.compare_digest(provided or "", expected)


def _require_admin(secret: str) -> None:
    if not _secret_ok(secret, settings.ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="bad secret")


@app.get("/health")
async def health():
    return {"ok": True, "execution": execution.status()}


@app.post("/webhook/{token}")
async def webhook(token: str, request: Request, background_tasks: BackgroundTasks):
    # Fail closed: a misconfigured (unset) token rejects everything rather than
    # falling back to a guessable default.
    if not settings.WEBHOOK_TOKEN:
        raise HTTPException(status_code=503, detail="webhook not configured")
    if not _secret_ok(token, settings.WEBHOOK_TOKEN):
        raise HTTPException(status_code=403, detail="bad token")

    body = (await request.body()).decode("utf-8", errors="replace")
    alert = parse_alert(body)
    await db.log_raw(alert.kind, body)

    # Respond to TradingView immediately; do the slow work in the background.
    # BackgroundTasks is managed by Starlette — no weak-reference GC risk.
    background_tasks.add_task(_handle, alert)
    return {"received": True, "kind": alert.kind}


async def _handle(alert) -> None:
    try:
        print(f"[handle] kind={alert.kind} life_id={alert.life_id} side={alert.side}")
        if alert.kind == "entry":
            # A1/A7: an entry with no life_id cannot be deduped or reconciled to
            # its lifecycle (and could never be safely executed). Surface it
            # loudly instead of silently dropping it.
            if not alert.life_id:
                print("[handle] entry: MISSING life_id — not journaled")
                await send_telegram(
                    f"⚠️ Entry with NO life_id — not journaled (cannot dedup/track):\n"
                    f"{alert.raw[:400]}"
                )
                return

            # A3: claim the signal BEFORE any expensive/irreversible work. If the
            # row already exists this is a webhook replay/retry — stop here so we
            # never double-call Claude, double-notify, or (later) double-order.
            print(f"[handle] entry: claiming life_id={alert.life_id}")
            claimed = await db.open_trade(alert)
            if not claimed:
                print(f"[handle] entry: duplicate life_id={alert.life_id} — skipping")
                return

            print(f"[handle] entry: fetching stats")
            stats_ctx = await stats.stats_block(alert)
            spot = await spot_price_note()
            print(f"[handle] entry: calling Claude")
            read = await analyze_entry(alert, stats_ctx, spot)
            verdict = extract_verdict(read)
            print(f"[handle] entry: writing verdict={verdict}")
            await db.set_verdict(alert.life_id, read, verdict)
            print(f"[handle] entry: sending Telegram")
            arrow = "🟢" if alert.side == "LONG" else "🔴"
            await send_telegram(
                f"{arrow} {alert.side} {alert.grade} — {alert.setup}\n"
                f"Entry {alert.entry} | SL {alert.sl} | TP {alert.tp} | {alert.rr}R\n"
                f"Regime {alert.regime} | Score {alert.score} | Sess {alert.ctx.get('Sess')}\n"
                f"{'─' * 24}\n{read}"
            )
            print(f"[handle] entry: done")

        elif alert.kind == "lifecycle":
            result = await db.close_trade(alert.life_id, alert.event)
            status = result["status"]
            if status == "already_closed":
                # Replayed/duplicate lifecycle alert — do not re-notify.
                print(f"[handle] lifecycle: duplicate {alert.event} for {alert.life_id} — skipping")
                return
            icon = "✅" if alert.event == "TP HIT" else "❌" if alert.event == "SL HIT" else "⚠️"
            row = result["row"]
            if row:
                detail = f"{row['side']} {row['grade']} {row['setup']} ({row['regime']})"
            else:
                # not_found (orphan) or no_db — surface with the id we have.
                detail = f"{alert.life_id} (no journaled entry)"
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
            # Shadow-track the blocked setup so we can later measure whether the
            # risk gate is excluding profitable trades (no Pine lifecycle exists).
            if await shadow.record_watch(alert):
                print(f"[handle] watch: shadow-tracked {alert.side} {alert.setup}")

        else:
            await send_telegram(f"❓ Unparsed alert:\n{alert.raw[:500]}", watch_channel=True)

    except Exception as exc:
        # Last-resort guard: never crash silently.
        import traceback
        print("[handler error]", exc)
        print(traceback.format_exc())
        try:
            await send_telegram(f"⚠️ Agent error handling alert: {exc}")
        except Exception:
            pass

@app.get("/trades", response_class=HTMLResponse)
async def trades(secret: str = ""):
    _require_admin(secret)
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
    _require_admin(secret)
    return await db.live_summary()


@app.get("/stats/shadow")
async def shadow_stats_page(secret: str = ""):
    """Realised-R for WATCH/blocked setups, simulated from spot price. Shows what
    the risk gate filtered out, in the same shape as /stats for comparison."""
    _require_admin(secret)
    return await db.shadow_summary()


@app.get("/admin/import-old")
async def import_old(secret: str = ""):
    """One-shot: merge the previous deployment's journal (set OLD_DATABASE_URL first)."""
    _require_admin(secret)
    old_url = os.environ.get("OLD_DATABASE_URL", "")
    if not old_url:
        return {"error": "Set the OLD_DATABASE_URL variable on this service first, then retry."}
    try:
        return await db.import_old_journal(old_url)
    except Exception as exc:  # surface connection/schema problems readably
        return {"error": str(exc)}


@app.get("/admin/delete-test-rows")
async def delete_test_rows(secret: str = ""):
    """One-off: remove the bogus test rows at entry 3345.6. Idempotent."""
    _require_admin(secret)
    return {"result": await db.delete_test_rows()}


@app.get("/admin/raw-alerts")
async def raw_alerts(secret: str = "", limit: int = 50):
    """Last N raw webhook bodies received — use to verify TradingView is delivering."""
    _require_admin(secret)
    rows = await db.raw_alerts_recent(min(limit, 200))
    return [
        {
            "id": r["id"],
            "received_at": r["received_at"].isoformat(),
            "kind": r["kind"],
            "body": r["body"][:500],
        }
        for r in rows
    ]


@app.get("/trades.csv")
async def trades_csv(secret: str = ""):
    """Full journal export, verdict included, for offline analysis."""
    _require_admin(secret)
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