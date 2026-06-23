"""Postgres journal. Railway injects DATABASE_URL when you attach the Postgres plugin."""
from __future__ import annotations

import json
import re

import asyncpg

from . import alignment
from .config import settings
from .parser import ParsedAlert

_pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_alerts (
    id          BIGSERIAL PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind        TEXT NOT NULL,
    body        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    life_id    TEXT PRIMARY KEY,
    side       TEXT NOT NULL,
    grade      TEXT,
    setup      TEXT,
    regime     TEXT,
    entry      DOUBLE PRECISION,
    sl         DOUBLE PRECISION,
    tp         DOUBLE PRECISION,
    rr         DOUBLE PRECISION,
    ctx        JSONB,
    opened_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome    TEXT,            -- TP HIT / SL HIT / AMBIGUOUS
    closed_at  TIMESTAMPTZ,
    claude_read TEXT
);

-- Shadow journal: WATCH/blocked setups the engine did NOT trade. Pine emits no
-- lifecycle for these, so `outcome` is filled by the spot-price poller. `entry`
-- is reconstructed from SL/TP/RR (the would-be plan). dedup_key collapses the
-- same blocked setup re-firing each bar / webhook retries into one row.
CREATE TABLE IF NOT EXISTS shadow_trades (
    id          BIGSERIAL PRIMARY KEY,
    dedup_key   TEXT UNIQUE,
    side        TEXT NOT NULL,
    grade       TEXT,
    setup       TEXT,
    regime      TEXT,
    entry       DOUBLE PRECISION,
    sl          DOUBLE PRECISION,
    tp          DOUBLE PRECISION,
    rr          DOUBLE PRECISION,
    quality     INT,
    reason      TEXT,
    ctx         JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome     TEXT,            -- TP HIT / SL HIT / EXPIRED (NULL = open)
    resolved_price DOUBLE PRECISION,
    closed_at   TIMESTAMPTZ
);
"""


async def init_db() -> None:
    global _pool
    if not settings.DATABASE_URL:
        return  # allow running without a DB (alerts still flow to Telegram)
    _pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=4)
    async with _pool.acquire() as con:
        await con.execute(SCHEMA)
        # Migration: verdict action (TAKE/REDUCE/SKIP) as its own column so it
        # is queryable. Backfill once from existing claude_read text.
        await con.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS verdict TEXT")
        await con.execute(
            r"""
            UPDATE trades
            SET verdict = upper((regexp_match(claude_read, 'VERDICT:\s*(TAKE|REDUCE|SKIP)', 'i'))[1])
            WHERE verdict IS NULL AND claude_read IS NOT NULL
            """
        )


async def close_db() -> None:
    if _pool:
        await _pool.close()


async def log_raw(kind: str, body: str) -> None:
    if not _pool:
        return
    async with _pool.acquire() as con:
        await con.execute(
            "INSERT INTO raw_alerts (kind, body) VALUES ($1, $2)", kind, body
        )


async def open_trade(a: ParsedAlert) -> bool:
    """Atomically *claim* a signal by life_id. Returns True only when this call
    inserted a new row; False when the life_id already existed (a webhook
    replay/retry) or there is no DB. Callers MUST gate expensive/irreversible
    side effects (Claude call, Telegram, future MT5 order) on a True return so
    replays do not double-fire. The Claude read/verdict are attached later via
    set_verdict() once the signal is confirmed new.
    """
    if not _pool or not a.life_id:
        return False
    async with _pool.acquire() as con:
        row = await con.fetchrow(
            """
            INSERT INTO trades (life_id, side, grade, setup, regime, entry, sl, tp, rr, ctx)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (life_id) DO NOTHING
            RETURNING life_id
            """,
            a.life_id, a.side, a.grade, a.setup, a.regime,
            a.entry, a.sl, a.tp, a.rr, json.dumps(a.ctx),
        )
        return row is not None


async def set_verdict(life_id: str, claude_read: str | None, verdict: str | None) -> None:
    """Attach Claude's read + extracted verdict to an already-claimed trade."""
    if not _pool or not life_id:
        return
    async with _pool.acquire() as con:
        await con.execute(
            "UPDATE trades SET claude_read = $2, verdict = $3 WHERE life_id = $1",
            life_id, claude_read, verdict,
        )


async def close_trade(life_id: str, outcome: str) -> dict:
    """Idempotently close a trade. Returns {"status": ..., "row": ...} where
    status is one of:
      - "closed"        : this call transitioned an open trade (notify the user)
      - "already_closed": a replay/duplicate lifecycle alert (do nothing)
      - "not_found"     : no journaled trade for this life_id (orphan — notify once)
      - "no_db"         : journal disabled
    Only an open trade (outcome IS NULL) is transitioned, so duplicate alerts
    cannot re-notify or (later) re-flatten a position.
    """
    if not _pool:
        return {"status": "no_db", "row": None}
    async with _pool.acquire() as con:
        row = await con.fetchrow(
            """
            UPDATE trades SET outcome = $2, closed_at = now()
            WHERE life_id = $1 AND outcome IS NULL
            RETURNING life_id, side, grade, setup, regime, entry, sl, tp, rr, opened_at
            """,
            life_id, outcome,
        )
        if row:
            return {"status": "closed", "row": dict(row)}
        exists = await con.fetchval("SELECT 1 FROM trades WHERE life_id = $1", life_id)
        return {"status": "already_closed" if exists else "not_found", "row": None}


async def live_stats() -> list[dict]:
    """Win-rate by regime x direction from the live journal (closed trades only)."""
    if not _pool:
        return []
    async with _pool.acquire() as con:
        rows = await con.fetch(
            """
            SELECT regime, side,
                   count(*)                                   AS trades,
                   count(*) FILTER (WHERE outcome = 'TP HIT') AS wins
            FROM trades
            WHERE outcome IN ('TP HIT', 'SL HIT')
            GROUP BY regime, side
            ORDER BY regime, side
            """
        )
        return [dict(r) for r in rows]


async def live_summary() -> dict:
    """Live journal aggregates across several dimensions, in realised R.

    Realised R: TP HIT -> |tp-entry| / |entry-sl|, SL HIT -> -1. For each
    {dim: {key: {...}}} bucket we report the metrics the validation rule judges
    on — profit_factor and expectancy_r — not just win rate. Costs: the Pine
    backtest models zero costs/bar-close fills, so an after-cost view is added
    when COST_R_PER_TRADE is set. Dims: overall, side, setup, grade, bucket
    (regime+side), verdict (Claude's own past calls), and alignment.
    """
    if not _pool:
        return {}
    async with _pool.acquire() as con:
        rows = await con.fetch(
            """
            SELECT side, grade, setup, regime, verdict, ctx, entry, sl, tp, outcome
            FROM trades
            WHERE outcome IN ('TP HIT', 'SL HIT')
              AND entry IS NOT NULL AND sl IS NOT NULL AND tp IS NOT NULL
            """
        )

    out: dict[str, dict] = {}

    def add(dim: str, key: str | None, r: float, win: int) -> None:
        if not key:
            return
        b = out.setdefault(dim, {}).setdefault(
            key, {"trades": 0, "wins": 0, "net_r": 0.0, "_gross_win_r": 0.0, "_gross_loss_r": 0.0}
        )
        b["trades"] += 1
        b["wins"] += win
        b["net_r"] = round(b["net_r"] + r, 2)
        if r >= 0:
            b["_gross_win_r"] += r
        else:
            b["_gross_loss_r"] += -r

    for row in rows:
        risk = abs(row["entry"] - row["sl"])
        if not risk:
            continue
        win = 1 if row["outcome"] == "TP HIT" else 0
        r = abs(row["tp"] - row["entry"]) / risk if win else -1.0
        ctx = row["ctx"]
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except ValueError:
                ctx = None
        add("overall", "ALL", r, win)
        add("side", row["side"], r, win)
        add("setup", row["setup"], r, win)
        add("grade", row["grade"], r, win)
        if row["regime"]:
            add("bucket", f"{row['regime']} {row['side']}", r, win)
        add("verdict", row["verdict"], r, win)
        add("alignment", alignment.classify(row["side"], ctx), r, win)

    cost = settings.COST_R_PER_TRADE
    for dim in out.values():
        for b in dim.values():
            n = b["trades"]
            gw = b.pop("_gross_win_r")
            gl = b.pop("_gross_loss_r")
            b["win_rate_pct"] = round(b["wins"] / n * 100, 1) if n else 0.0
            b["profit_factor"] = round(gw / gl, 2) if gl else None  # None = no losers yet
            b["expectancy_r"] = round(b["net_r"] / n, 3) if n else 0.0
            if cost:
                # Each trade pays `cost` R; net falls by n*cost, gross loss rises.
                net_ac = b["net_r"] - n * cost
                gl_ac = gl + n * cost
                b["net_r_after_cost"] = round(net_ac, 2)
                b["expectancy_r_after_cost"] = round(net_ac / n, 3) if n else 0.0
                b["profit_factor_after_cost"] = round(gw / gl_ac, 2) if gl_ac else None
    return out


async def insert_shadow(a: ParsedAlert, entry: float, dedup_key: str) -> bool:
    """Journal a WATCH/blocked setup as an open shadow trade. Returns True if a
    new row was inserted (False = duplicate of the same blocked setup)."""
    if not _pool:
        return False
    async with _pool.acquire() as con:
        row = await con.fetchrow(
            """
            INSERT INTO shadow_trades
                (dedup_key, side, grade, setup, regime, entry, sl, tp, rr, quality, reason, ctx)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT (dedup_key) DO NOTHING
            RETURNING id
            """,
            dedup_key, a.side, a.grade, a.setup, a.regime,
            entry, a.sl, a.tp, a.rr, a.watch_quality, a.watch_reason, json.dumps(a.ctx),
        )
        return row is not None


async def open_shadows() -> list[dict]:
    """Unresolved shadow trades (outcome IS NULL) for the poller to evaluate."""
    if not _pool:
        return []
    async with _pool.acquire() as con:
        rows = await con.fetch(
            """
            SELECT id, side, entry, sl, tp, created_at
            FROM shadow_trades WHERE outcome IS NULL
            """
        )
        return [dict(r) for r in rows]


async def resolve_shadow(shadow_id: int, outcome: str, price: float | None) -> None:
    """Close a shadow trade with a simulated outcome (TP HIT / SL HIT / EXPIRED)."""
    if not _pool:
        return
    async with _pool.acquire() as con:
        await con.execute(
            """
            UPDATE shadow_trades
            SET outcome = $2, resolved_price = $3, closed_at = now()
            WHERE id = $1 AND outcome IS NULL
            """,
            shadow_id, outcome, price,
        )


async def shadow_summary() -> dict:
    """Realised-R aggregates for resolved shadow trades (TP/SL HIT only;
    EXPIRED excluded). Same shape/metrics as live_summary so blocked-bucket
    performance is directly comparable to the live journal."""
    if not _pool:
        return {}
    async with _pool.acquire() as con:
        rows = await con.fetch(
            """
            SELECT side, grade, setup, regime, ctx, entry, sl, tp, outcome
            FROM shadow_trades
            WHERE outcome IN ('TP HIT', 'SL HIT')
              AND entry IS NOT NULL AND sl IS NOT NULL AND tp IS NOT NULL
            """
        )

    out: dict[str, dict] = {}

    def add(dim: str, key: str | None, r: float, win: int) -> None:
        if not key:
            return
        b = out.setdefault(dim, {}).setdefault(
            key, {"trades": 0, "wins": 0, "net_r": 0.0, "_gw": 0.0, "_gl": 0.0}
        )
        b["trades"] += 1
        b["wins"] += win
        b["net_r"] = round(b["net_r"] + r, 2)
        if r >= 0:
            b["_gw"] += r
        else:
            b["_gl"] += -r

    for row in rows:
        risk = abs(row["entry"] - row["sl"])
        if not risk:
            continue
        win = 1 if row["outcome"] == "TP HIT" else 0
        r = abs(row["tp"] - row["entry"]) / risk if win else -1.0
        ctx = row["ctx"]
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except ValueError:
                ctx = None
        add("overall", "ALL", r, win)
        add("side", row["side"], r, win)
        add("setup", row["setup"], r, win)
        add("grade", row["grade"], r, win)
        if row["regime"]:
            add("bucket", f"{row['regime']} {row['side']}", r, win)
        add("alignment", alignment.classify(row["side"], ctx), r, win)

    for dim in out.values():
        for b in dim.values():
            n = b["trades"]
            gw, gl = b.pop("_gw"), b.pop("_gl")
            b["win_rate_pct"] = round(b["wins"] / n * 100, 1) if n else 0.0
            b["profit_factor"] = round(gw / gl, 2) if gl else None
            b["expectancy_r"] = round(b["net_r"] / n, 3) if n else 0.0
    return out


_VERDICT_RE = re.compile(r"VERDICT:\s*(TAKE|REDUCE|SKIP)", re.IGNORECASE)


async def import_old_journal(old_url: str) -> dict:
    """One-shot merge of the previous deployment's journal into this one.

    Maps the old schema flexibly (direction->side, result->outcome,
    created_at->opened_at), imports only closed trades with a life_id,
    extracts verdicts from any stored Claude text, and skips duplicates
    via ON CONFLICT DO NOTHING. Safe to run repeatedly.
    """
    if not _pool:
        return {"error": "no db"}
    old = await asyncpg.connect(old_url, timeout=20)
    try:
        rows = await old.fetch("SELECT * FROM trades")
    finally:
        await old.close()

    inserted, skipped, dupes = 0, 0, 0
    async with _pool.acquire() as con:
        for raw in rows:
            d = dict(raw)
            life_id = d.get("life_id")
            outcome = d.get("outcome") or d.get("result")
            if not life_id or outcome not in ("TP HIT", "SL HIT"):
                skipped += 1  # zombie OPEN rows, test payloads, no lifecycle key
                continue
            ctx = d.get("ctx")
            if isinstance(ctx, (dict, list)):
                ctx = json.dumps(ctx)
            read = d.get("claude_read") or d.get("claude_verdict")
            verdict = d.get("verdict")
            if not verdict and read:
                m = _VERDICT_RE.search(read)
                verdict = m.group(1).upper() if m else None
            result = await con.execute(
                """
                INSERT INTO trades (life_id, side, grade, setup, regime, entry, sl,
                                    tp, rr, ctx, claude_read, verdict, outcome,
                                    opened_at, closed_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                ON CONFLICT (life_id) DO NOTHING
                """,
                str(life_id),
                d.get("side") or d.get("direction"),
                d.get("grade"), d.get("setup"), d.get("regime") or None,
                d.get("entry"), d.get("sl"), d.get("tp"), d.get("rr"),
                ctx, read, verdict, outcome,
                d.get("opened_at") or d.get("created_at"),
                d.get("closed_at"),
            )
            if result.endswith("1"):
                inserted += 1
            else:
                dupes += 1
    return {"old_rows": len(rows), "inserted": inserted,
            "skipped_open_or_no_life_id": skipped, "already_present": dupes}


async def delete_test_rows(entry: float = 3345.6) -> str:
    """One-off cleanup of known bogus test payload rows (impossible price)."""
    if not _pool:
        return "no db"
    async with _pool.acquire() as con:
        return await con.execute("DELETE FROM trades WHERE entry = $1", entry)


async def raw_alerts_recent(limit: int = 50) -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as con:
        rows = await con.fetch(
            """
            SELECT id, received_at, kind, body
            FROM raw_alerts ORDER BY received_at DESC LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]


async def all_trades(limit: int = 200) -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as con:
        rows = await con.fetch(
            """
            SELECT life_id, side, grade, setup, regime, entry, sl, tp, rr,
                   opened_at, outcome, closed_at, verdict, claude_read
            FROM trades ORDER BY opened_at DESC LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]