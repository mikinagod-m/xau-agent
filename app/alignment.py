"""HTF alignment classification.

Live finding (Jun 8-12 journal + chart review): losing trades cluster where
the signal direction opposes the engine's own dashboard context — Bias, Flow,
and Momentum from the CTX payload. This module scores that alignment so it can
be (a) aggregated over the live journal and (b) flagged on incoming signals.

Vote per field: bull-ish -> +1, bear-ish -> -1, else 0. The trade direction
dots against the net vote: ALIGNED (net agrees by >= 2), COUNTER (net opposes
by >= 2), MIXED otherwise. Mirrors the optional Pine-side filter so the two
layers agree on what "counter-bias" means.
"""
from __future__ import annotations

_FIELDS = ("Bias", "Flow", "Momentum")


def _vote(value: str | None) -> int:
    if not value:
        return 0
    v = value.upper()
    if "BULL" in v or v.startswith("LONG"):
        return 1
    if "BEAR" in v or v.startswith("SHORT"):
        return -1
    return 0


def classify(side: str | None, ctx: dict | None) -> str | None:
    """Return ALIGNED / COUNTER / MIXED, or None if unknowable."""
    if side not in ("LONG", "SHORT") or not ctx:
        return None
    votes = [_vote(ctx.get(f)) for f in _FIELDS]
    if not any(votes):
        return None  # ctx present but carries no directional info
    net = sum(votes)
    d = 1 if side == "LONG" else -1
    dot = net * d
    if dot >= 2:
        return "ALIGNED"
    if dot <= -2:
        return "COUNTER"
    return "MIXED"


def describe(side: str | None, ctx: dict | None) -> str | None:
    """One-line human/Claude-readable summary, e.g. for the prompt."""
    a = classify(side, ctx)
    if a is None:
        return None
    parts = ", ".join(f"{f}={ctx.get(f, '?')}" for f in _FIELDS)
    return f"{a} ({parts})"
