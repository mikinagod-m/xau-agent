"""Historical performance context fed to Claude with every signal.

Combines the static backtest seed (seed/backtest_stats.json, generated from the
TradingView Strategy Tester export) with the live trade journal in Postgres.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import db

_SEED_PATH = Path(__file__).resolve().parent.parent / "seed" / "backtest_stats.json"
_seed: dict = json.loads(_SEED_PATH.read_text()) if _SEED_PATH.exists() else {}


def backtest_seed() -> dict:
    return _seed


def bucket_stats(regime: str | None, side: str | None) -> dict | None:
    """Backtest stats for this exact regime x direction bucket, if known."""
    if not regime or not side:
        return None
    return _seed.get("by_regime_direction", {}).get(f"{regime}|{side}")


async def stats_block(regime: str | None, side: str | None) -> str:
    """Human-readable stats context for the Claude prompt and Telegram footer."""
    lines: list[str] = []

    b = bucket_stats(regime, side)
    if b:
        lines.append(
            f"Backtest, this bucket ({regime} {side}): {b['trades']} trades, "
            f"{b['win_rate_pct']}% WR, net {b['net_pnl_usd']:+.2f} USD."
        )
    if _seed.get("overall"):
        o = _seed["overall"]
        lines.append(
            f"Backtest overall: {o['trades']} trades, {o['win_rate_pct']}% WR, "
            f"PF {o['profit_factor']} (zero costs modelled)."
        )
    for note in _seed.get("notes", []):
        lines.append(f"Note: {note}")

    live = await db.live_stats()
    if live:
        lines.append("Live journal so far: " + "; ".join(
            f"{r['regime']} {r['side']}: {r['wins']}/{r['trades']} TP"
            for r in live
        ))
    return "\n".join(lines)
