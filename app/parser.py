"""Parser for XAU-U10 (U9 engine) TradingView alert messages.

Three message shapes arrive at the webhook, all plain text:

1. ENTRY (unified signal alert):
   XAU-U9 ▲ LONG (A SWEEP)
   Entry 2345.6  SL 2340.1  TP 2356.0  RR 2.1R
   CTX: Score=11/16|Regime=TREND|HTF=DISCOUNT|Sess=LONDON UK|Bias=LONG|...
   ASIA: H=2350.2 L=2338.9,life_id=XAUUSD-3-1718000000000

2. WATCH (risk-blocked setup):
   XAU-U9 WATCH LONG (B SWP Q 62% RR<min)
   SL 2340.1  TP 2356.0  RR 1.2R
   CTX: ...

3. LIFECYCLE (TP/SL outcome):
   XAU-U9 LIFE,TP HIT,life_id=XAUUSD-3-1718000000000
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ParsedAlert:
    kind: str                      # "entry" | "watch" | "lifecycle" | "unknown"
    raw: str = ""
    side: str | None = None        # LONG / SHORT
    grade: str | None = None       # A+ / A / B / C
    setup: str | None = None       # SWEEP, CONT, etc.
    entry: float | None = None
    sl: float | None = None
    tp: float | None = None
    rr: float | None = None
    ctx: dict = field(default_factory=dict)   # parsed CTX key/values
    asia_high: float | None = None
    asia_low: float | None = None
    life_id: str | None = None
    # lifecycle only
    event: str | None = None       # TP HIT / SL HIT / AMBIGUOUS
    # watch only
    watch_quality: int | None = None
    watch_reason: str | None = None

    @property
    def regime(self) -> str | None:
        return self.ctx.get("Regime")

    @property
    def score(self) -> str | None:
        return self.ctx.get("Score")


_NUM = r"(-?\d+(?:\.\d+)?)"


def _f(m: re.Match | None, group: int = 1) -> float | None:
    return float(m.group(group)) if m else None


def _parse_ctx(text: str) -> dict:
    """Parse the pipe-delimited 'CTX: k=v|k=v|...' line into a dict."""
    m = re.search(r"CTX:\s*(.+)", text)
    if not m:
        return {}
    body = m.group(1).strip()
    out: dict[str, str] = {}
    for pair in body.split("|"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def parse_alert(raw: str) -> ParsedAlert:
    text = raw.strip()

    # --- lifecycle: "XAU-U9 LIFE,<EVT>,life_id=<id>" --------------------------
    m = re.match(r"XAU-U\d+\s+LIFE\s*,\s*([^,]+)\s*,\s*life_id=(\S+)", text)
    if m:
        return ParsedAlert(kind="lifecycle", raw=raw,
                           event=m.group(1).strip(), life_id=m.group(2).strip())

    life = re.search(r"life_id=([^\s,]+)", text)
    ctx = _parse_ctx(text)
    asia = re.search(rf"ASIA:\s*H={_NUM}\s+L={_NUM}", text)

    # --- watch: "XAU-U9 WATCH LONG (B SWP Q 62% reason)" -----------------------
    m = re.search(r"XAU-U\d+\s+WATCH\s+(LONG|SHORT)\s*\(\s*(A\+|A|B|C)\s+(\S+)\s+Q\s+(\d+)%\s*([^)]*)\)", text)
    if m:
        return ParsedAlert(
            kind="watch", raw=raw, side=m.group(1), grade=m.group(2),
            setup=m.group(3), watch_quality=int(m.group(4)),
            watch_reason=m.group(5).strip() or None,
            sl=_f(re.search(rf"SL\s+{_NUM}", text)),
            tp=_f(re.search(rf"TP\s+{_NUM}", text)),
            rr=_f(re.search(rf"RR\s+{_NUM}R", text)),
            ctx=ctx,
            asia_high=_f(asia, 1), asia_low=_f(asia, 2),
            life_id=life.group(1) if life else None,
        )

    # --- entry: "XAU-U9 ▲ LONG (A SWEEP)" --------------------------------------
    m = re.search(r"XAU-U\d+\s+(?:[^\s(]*\s+)?(LONG|SHORT)\s*\(\s*(A\+|A|B|C)\s+([^)]+)\)", text)
    if m:
        return ParsedAlert(
            kind="entry", raw=raw, side=m.group(1), grade=m.group(2),
            setup=m.group(3).strip(),
            entry=_f(re.search(rf"Entry\s+{_NUM}", text)),
            sl=_f(re.search(rf"SL\s+{_NUM}", text)),
            tp=_f(re.search(rf"TP\s+{_NUM}", text)),
            rr=_f(re.search(rf"RR\s+{_NUM}R", text)),
            ctx=ctx,
            asia_high=_f(asia, 1), asia_low=_f(asia, 2),
            life_id=life.group(1) if life else None,
        )

    return ParsedAlert(kind="unknown", raw=raw, ctx=ctx)
