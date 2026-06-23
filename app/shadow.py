"""Shadow-tracking for WATCH / risk-blocked setups.

The Pine engine only arms lifecycle tracking for setups it actually trades
(XAU-U9.3.pine line ~2206); WATCH/blocked alerts carry no life_id and never get
a TP/SL HIT lifecycle alert. To learn whether a filter is wrongly excluding
profitable setups, we journal each blocked setup as a *shadow* trade and
simulate its outcome from live spot price (metalpriceapi) — exactly the
fallback the project notes call for.

Caveat: spot polling is coarse (minute-ish) and uses a different feed than the
OANDA chart, so shadow outcomes are approximate — fine for re-evaluation
signals, not for execution.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from . import db
from .config import settings
from .notify import spot_price
from .parser import ParsedAlert


def derive_entry(side: str | None, sl: float | None, tp: float | None,
                 rr: float | None) -> float | None:
    """Reconstruct the would-be entry from the plan's SL/TP/RR.

    For both directions RR = reward/risk gives a single solution:
        entry = (TP + RR*SL) / (RR + 1)
    Returns None when inputs are missing or degenerate (RR <= 0).
    """
    if sl is None or tp is None or rr is None or rr <= 0:
        return None
    return round((tp + rr * sl) / (rr + 1), 3)


def dedup_key(a: ParsedAlert, entry: float) -> str:
    """Collapse the same blocked setup re-firing each bar / webhook retries.
    Intentionally time-independent: a setup that stays blocked across bars is
    one opportunity, not many."""
    score = a.ctx.get("Score", "?") if a.ctx else "?"
    return f"{a.side}|{a.setup}|{a.grade}|{a.sl}|{a.tp}|{a.rr}|{score}"


async def record_watch(a: ParsedAlert) -> bool:
    """Journal a WATCH alert as a shadow trade. Returns False when it can't be
    tracked (missing TP/RR -> no derivable entry) or shadow tracking is off."""
    if not settings.SHADOW_TRACKING:
        return False
    entry = derive_entry(a.side, a.sl, a.tp, a.rr)
    if entry is None:
        return False
    return await db.insert_shadow(a, entry, dedup_key(a, entry))


def _evaluate(side: str, price: float, sl: float, tp: float) -> str | None:
    """Return 'TP HIT' / 'SL HIT' / None for a spot price vs the plan levels."""
    if side == "LONG":
        if price >= tp:
            return "TP HIT"
        if price <= sl:
            return "SL HIT"
    elif side == "SHORT":
        if price <= tp:
            return "TP HIT"
        if price >= sl:
            return "SL HIT"
    return None


async def resolve_open_shadows() -> dict:
    """One poll cycle: fetch spot, resolve any open shadows that hit TP/SL, and
    expire those older than SHADOW_MAX_AGE_MIN. Returns a small counters dict."""
    shadows = await db.open_shadows()
    if not shadows:
        return {"open": 0, "resolved": 0, "expired": 0}

    price = await spot_price()
    now = datetime.now(timezone.utc)
    max_age = settings.SHADOW_MAX_AGE_MIN
    resolved = expired = 0

    for s in shadows:
        sl, tp = s.get("sl"), s.get("tp")
        outcome = None
        if price is not None and sl is not None and tp is not None:
            outcome = _evaluate(s["side"], price, sl, tp)
        if outcome:
            await db.resolve_shadow(s["id"], outcome, price)
            resolved += 1
            continue
        created = s["created_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if (now - created).total_seconds() / 60.0 >= max_age:
            await db.resolve_shadow(s["id"], "EXPIRED", price)
            expired += 1

    return {"open": len(shadows), "resolved": resolved, "expired": expired}


async def run_shadow_poller() -> None:
    """Background loop started in the FastAPI lifespan. Resolves open shadows on
    a fixed interval. Survives transient errors; cancelled cleanly on shutdown."""
    interval = max(15, settings.SHADOW_POLL_SECONDS)
    print(f"[shadow] poller started (every {interval}s, expire {settings.SHADOW_MAX_AGE_MIN}m)")
    while True:
        try:
            await asyncio.sleep(interval)
            result = await resolve_open_shadows()
            if result["resolved"] or result["expired"]:
                print(f"[shadow] {result}")
        except asyncio.CancelledError:
            print("[shadow] poller stopping")
            raise
        except Exception as exc:
            print("[shadow] poll error:", exc)
