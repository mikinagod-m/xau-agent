"""Claude analysis layer. One call per qualifying entry signal."""
from __future__ import annotations

import anthropic

from .config import settings
from .parser import ParsedAlert

_client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

SYSTEM = """You are the analysis layer of an XAUUSD trading agent. Signals come
from a Pine Script engine (XAU-U10) that gates on TIME -> LOCATION -> EVENT:
liquidity sweeps, structure shifts, VWAP context, session weighting, and a
0-16 score with A+/A/B/C grading. Signals are bar-close confirmed.

Your job is NOT to predict price. Your job is to grade the setup against the
engine's own historical performance and the context payload, then recommend
TAKE, REDUCE, or SKIP with a one-line reason.

Rules:
- Be blunt. If the historical bucket is net negative (e.g. TREND SHORT),
  say so and lean toward SKIP or REDUCE unless the context is exceptional.
- Note anything in the CTX that conflicts with the trade direction
  (bias, flow, VWAP, premium/discount location, session quality).
- Confirm the maths: risk vs reward in points, where invalidation sits.
- Never invent data not present in the payload.
- Output format, max ~120 words total:
  Line 1: VERDICT: TAKE | REDUCE | SKIP - <one-line reason>
  Then 2-4 short lines of supporting read (context alignment, risk, level quality).
- This is decision support, not financial advice, and the human always decides."""


async def analyze_entry(a: ParsedAlert, stats_context: str, spot_note: str = "") -> str:
    payload = (
        f"SIGNAL: {a.side} grade {a.grade} setup {a.setup}\n"
        f"Entry {a.entry}  SL {a.sl}  TP {a.tp}  RR {a.rr}R\n"
        f"CTX: {a.ctx}\n"
        f"Asia range: H={a.asia_high} L={a.asia_low}\n"
        f"{spot_note}\n\n"
        f"HISTORICAL PERFORMANCE:\n{stats_context}"
    )
    try:
        msg = await _client.messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=400,
            system=SYSTEM,
            messages=[{"role": "user", "content": payload}],
        )
        return "".join(b.text for b in msg.content if b.type == "text").strip()
    except Exception as exc:  # never let an API hiccup eat the alert
        return f"(Claude analysis unavailable: {exc})"
