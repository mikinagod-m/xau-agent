"""Historical performance context fed to Claude with every signal.

Combines the static backtest seed (seed/backtest_stats.json, generated from the
TradingView Strategy Tester export) with the live trade journal in Postgres.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import alignment, db

_SEED_PATH = Path(__file__).resolve().parent.parent / "seed" / "backtest_stats.json"
_seed: dict = json.loads(_SEED_PATH.read_text()) if _SEED_PATH.exists() else {}


def backtest_seed() -> dict:
    return _seed


def bucket_stats(regime: str | None, side: str | None) -> dict | None:
    """Backtest stats for this exact regime x direction bucket, if known."""
    if not regime or not side:
        return None
    return _seed.get("by_regime_direction", {}).get(f"{regime}|{side}")


def _fmt(label: str, s: dict | None) -> str | None:
    """One compact line per live bucket, with an explicit small-sample tag."""
    if not s or not s["trades"]:
        return None
    n, w, net = s["trades"], s["wins"], s["net_r"]
    tag = " (SMALL SAMPLE)" if n < 10 else ""
    return f"{label}: {w}/{n} TP ({w / n * 100:.0f}% WR), net {net:+.2f}R{tag}"


async def stats_block(a) -> str:
    """Human-readable stats context for the Claude prompt and Telegram footer.

    `a` is the parsed entry alert (needs .regime .side .setup .grade).
    Combines the static backtest seed with live journal aggregates, including
    how Claude's own past TAKE/REDUCE/SKIP verdicts have performed.
    """
    regime, side = a.regime, a.side
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

    live = await db.live_summary()
    if live:
        lines.append("LIVE JOURNAL (same engine, running live; R-based, SL = -1R):")
        for line in filter(None, [
            _fmt("  Overall", live.get("overall", {}).get("ALL")),
            _fmt(f"  This direction ({side})", live.get("side", {}).get(side)),
            _fmt(f"  This setup ({a.setup})", live.get("setup", {}).get(a.setup)),
            _fmt(f"  This grade ({a.grade})", live.get("grade", {}).get(a.grade)),
            _fmt(f"  This bucket ({regime} {side})",
                 live.get("bucket", {}).get(f"{regime} {side}") if regime else None),
        ]):
            lines.append(line)
        align = live.get("alignment", {})
        if align:
            lines.append("  HTF alignment (direction vs CTX Bias/Flow/Momentum at entry):")
            for k in ("ALIGNED", "MIXED", "COUNTER"):
                line = _fmt(f"    {k}", align.get(k))
                if line:
                    lines.append(line)
        verdicts = live.get("verdict", {})
        if verdicts:
            lines.append("  Your past verdicts on signals that then ran to TP/SL:")
            for v in ("TAKE", "REDUCE", "SKIP"):
                line = _fmt(f"    {v}", verdicts.get(v))
                if line:
                    lines.append(line)

    this_align = alignment.describe(side, a.ctx)
    if this_align:
        lines.append(f"THIS SIGNAL HTF alignment: {this_align}")
    return "\n".join(lines)
