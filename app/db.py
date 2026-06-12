"""Postgres journal. Railway injects DATABASE_URL when you attach the Postgres plugin."""
from __future__ import annotations

import json

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


async def open_trade(a: ParsedAlert, claude_read: str | None = None, verdict: str | None = None) -> None:
    if not _pool or not a.life_id:
        return
    async with _pool.acquire() as con:
        await con.execute(
            """
            INSERT INTO trades (life_id, side, grade, setup, regime, entry, sl, tp, rr, ctx, claude_read, verdict)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT (life_id) DO NOTHING
            """,
            a.life_id, a.side, a.grade, a.setup, a.regime,
            a.entry, a.sl, a.tp, a.rr, json.dumps(a.ctx), claude_read, verdict,
        )


async def close_trade(life_id: str, outcome: str) -> dict | None:
    """Mark a trade closed; return the row so Telegram can summarise it."""
    if not _pool:
        return None
    async with _pool.acquire() as con:
        row = await con.fetchrow(
            """
            UPDATE trades SET outcome = $2, closed_at = now()
            WHERE life_id = $1
            RETURNING life_id, side, grade, setup, regime, entry, sl, tp, rr, opened_at
            """,
            life_id, outcome,
        )
        return dict(row) if row else None


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

    Realised R: TP HIT -> |tp-entry| / |entry-sl|, SL HIT -> -1. Returns
    {dim: {key: {trades, wins, net_r}}} for dims: overall, side, setup,
    grade, bucket (regime+side), verdict (Claude's own past calls), and
    alignment (trade direction vs CTX Bias/Flow/Momentum at entry).
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
        b = out.setdefault(dim, {}).setdefault(key, {"trades": 0, "wins": 0, "net_r": 0.0})
        b["trades"] += 1
        b["wins"] += win
        b["net_r"] = round(b["net_r"] + r, 2)

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
    return out


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