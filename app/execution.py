"""Execution gate — the single chokepoint that decides whether a signal is ever
allowed to become a real broker order.

Phase A scaffolding: today the system is observation-only, so `can_execute()`
returns False unless explicitly promoted. The actual MT5 order path (Phase C)
must call `can_execute()` and `verdict_allows_execution()` before emitting any
execution intent. Everything here fails CLOSED by design.
"""
from __future__ import annotations

from .config import settings

# Modes in increasing capability. OFF/SHADOW never touch a broker.
_ORDER_PLACING_MODES = {"DEMO", "LIVE"}
_VALID_MODES = {"OFF", "SHADOW", "DEMO", "LIVE"}

# Only an explicit TAKE (full size) or REDUCE (reduced size) may trade.
# A missing/None/unknown verdict must be treated as SKIP (fail closed).
_EXECUTABLE_VERDICTS = {"TAKE", "REDUCE"}


def mode() -> str:
    m = settings.EXECUTION_MODE
    return m if m in _VALID_MODES else "OFF"


def is_killed() -> bool:
    return settings.KILL_SWITCH


def can_execute() -> bool:
    """True only when a real order is permitted: a broker-placing mode AND the
    kill switch is off. SHADOW/OFF and any kill-switch state return False."""
    return mode() in _ORDER_PLACING_MODES and not is_killed()


def is_shadow() -> bool:
    """SHADOW mode: run the full intent pipeline but log instead of sending."""
    return mode() == "SHADOW" and not is_killed()


def verdict_allows_execution(verdict: str | None) -> bool:
    """Fail-closed verdict gate. None/unknown -> not executable (SKIP)."""
    return (verdict or "").upper() in _EXECUTABLE_VERDICTS


def size_multiplier(verdict: str | None) -> float:
    """Position-size factor for an executable verdict. REDUCE = half size."""
    v = (verdict or "").upper()
    if v == "TAKE":
        return 1.0
    if v == "REDUCE":
        return 0.5
    return 0.0


def status() -> dict:
    """Diagnostic snapshot for /health and the (future) execution audit log."""
    return {
        "execution_mode": mode(),
        "kill_switch": is_killed(),
        "can_place_orders": can_execute(),
        "shadow": is_shadow(),
    }
