"""Central config. Everything comes from environment variables (Railway -> Variables tab)."""
import os


class Settings:
    # Shared secret embedded in the webhook URL path. TradingView cannot send
    # custom headers, so the secret lives in the URL: /webhook/<WEBHOOK_TOKEN>
    # No default: an unset token must FAIL CLOSED, never accept a known default.
    WEBHOOK_TOKEN: str = os.environ.get("WEBHOOK_TOKEN", "")

    # Separate secret for read/admin endpoints (/trades, /stats, /admin/*) so a
    # leaked dashboard link can't be replayed against the webhook. Falls back to
    # WEBHOOK_TOKEN only for backward compatibility — set a distinct value in prod.
    ADMIN_TOKEN: str = os.environ.get("ADMIN_TOKEN", "") or os.environ.get("WEBHOOK_TOKEN", "")

    # Execution control. The agent is observation-only until this is explicitly
    # promoted. OFF/SHADOW never place real orders; DEMO/LIVE require the MT5
    # worker AND a clear kill switch. Default OFF = fail closed.
    EXECUTION_MODE: str = os.environ.get("EXECUTION_MODE", "OFF").upper()
    # Hard global kill switch. When true, no execution intent is ever emitted,
    # regardless of EXECUTION_MODE. Anything other than an explicit "false" =
    # killed, so a typo or unset value fails closed.
    KILL_SWITCH: bool = os.environ.get("KILL_SWITCH", "true").lower() != "false"

    # Anthropic
    ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
    CLAUDE_MODEL: str = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")
    # Optional second chat for noisy WATCH/EARLY alerts. Falls back to main chat.
    TELEGRAM_WATCH_CHAT_ID: str = os.environ.get("TELEGRAM_WATCH_CHAT_ID", "") or os.environ.get("TELEGRAM_CHAT_ID", "")

    # Railway Postgres injects DATABASE_URL automatically when you add the plugin.
    DATABASE_URL: str = os.environ.get("DATABASE_URL", "")

    # Optional: metalpriceapi spot-price sanity check on incoming alerts.
    METALPRICE_API_KEY: str = os.environ.get("METALPRICE_API_KEY", "")

    # Estimated round-trip trading cost per trade, expressed in R (cost / risk).
    # The Pine backtest models ZERO costs and bar-close fills; live MT5 pays
    # spread + slippage. Set this (e.g. 0.05) so /stats also shows an honest
    # after-cost expectancy. Default 0.0 = show raw, uncosted numbers.
    COST_R_PER_TRADE: float = float(os.environ.get("COST_R_PER_TRADE", "0") or 0)

    # Send WATCH alerts through Claude too? Default off to save tokens -
    # WATCH alerts are forwarded raw with stats attached.
    ANALYZE_WATCH_ALERTS: bool = os.environ.get("ANALYZE_WATCH_ALERTS", "false").lower() == "true"


settings = Settings()
