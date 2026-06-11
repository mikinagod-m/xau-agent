"""Central config. Everything comes from environment variables (Railway -> Variables tab)."""
import os


class Settings:
    # Shared secret embedded in the webhook URL path. TradingView cannot send
    # custom headers, so the secret lives in the URL: /webhook/<WEBHOOK_TOKEN>
    WEBHOOK_TOKEN: str = os.environ.get("WEBHOOK_TOKEN", "change-me")

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

    # Send WATCH alerts through Claude too? Default off to save tokens -
    # WATCH alerts are forwarded raw with stats attached.
    ANALYZE_WATCH_ALERTS: bool = os.environ.get("ANALYZE_WATCH_ALERTS", "false").lower() == "true"


settings = Settings()
