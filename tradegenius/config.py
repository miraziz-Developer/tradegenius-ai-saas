"""Runtime settings, read once from environment variables. See .env.example."""

import os
import shlex
import sys


def _bool(name, default=False):
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name, default):
    v = os.getenv(name)
    return int(v) if v and v.strip() else default


def _ids(name):
    raw = os.getenv(name, "")
    return {int(x) for x in raw.replace(" ", "").split(",") if x.lstrip("-").isdigit()}


class Settings:
    def __init__(self):
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.admin_ids = _ids("ADMIN_IDS")

        # 32-byte urlsafe base64 Fernet key used to encrypt MT5 passwords at rest.
        self.encryption_key = os.getenv("ENCRYPTION_KEY", "").strip()

        self.gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
        # Tried in order when the main model is overloaded or retired.
        self.gemini_fallback_models = [m.strip() for m in os.getenv(
            "GEMINI_FALLBACK_MODELS", "gemini-3.7-flash,gemini-3.5-flash").split(",") if m.strip()]

        # Billing is OFF while testing: everyone uses the cloud bot for free.
        # Admins are always free, even after billing is switched on.
        self.billing_enabled = _bool("BILLING_ENABLED", False)
        self.payment_provider_token = os.getenv("PAYMENT_PROVIDER_TOKEN", "").strip()
        self.price_amount = _int("PRICE_AMOUNT", 3900)          # minor units (cents)
        self.price_currency = os.getenv("PRICE_CURRENCY", "USD").strip()
        self.subscription_days = _int("SUBSCRIPTION_DAYS", 30)
        self.trial_days = _int("TRIAL_DAYS", 0)

        # While testing, optionally restrict the bot to an allowlist of users.
        self.allowed_user_ids = _ids("ALLOWED_USER_IDS")

        # On Windows the engine runs natively (no Wine): same Python, same machine as MT5.
        windows = os.name == "nt"
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        self.data_dir = os.getenv("DATA_DIR", os.path.join(repo, "data") if windows else "/data")
        self.max_active_engines = _int("MAX_ACTIVE_ENGINES", 3)
        self.max_accounts_per_user = _int("MAX_ACCOUNTS_PER_USER", 1)
        self.backtest_bars = _int("BACKTEST_BARS", 3000)

        # Wine / MT5 locations. An empty WINE_CMD means native Windows. On macOS it can point at
        # CrossOver: '/Applications/CrossOver.app/.../bin/wine --bottle TradeGenius'.
        self.wine_cmd = shlex.split(os.getenv("WINE_CMD", "" if windows else "wine"), posix=not windows)
        self.wine_python = os.getenv("WINE_PYTHON", sys.executable if windows else r"C:\Python311\python.exe")
        self.mt5_base_dir = os.getenv("MT5_BASE_DIR", r"C:\Program Files\MetaTrader 5" if windows else "")
        self.engine_script = os.getenv("ENGINE_SCRIPT", os.path.join(repo, "engine", "engine.py")
                                       if windows else r"Z:\app\engine\engine.py")
        self.engines_enabled = _bool("ENGINES_ENABLED", True)
        self.setup_failed = _bool("SETUP_FAILED", False)
        self.ai_daily_limit = _int("AI_DAILY_LIMIT", 20)   # strategy analyses per user per day

    @property
    def db_path(self):
        return os.path.join(self.data_dir, "tradegenius.db")

    @property
    def clients_dir(self):
        return os.path.join(self.data_dir, "clients")

    def is_admin(self, user_id):
        return int(user_id) in self.admin_ids

    def problems(self):
        """Configuration errors that must be fixed before the bot can start."""
        out = []
        if not self.telegram_bot_token:
            out.append("TELEGRAM_BOT_TOKEN is not set")
        if not self.encryption_key:
            out.append("ENCRYPTION_KEY is not set (generate with: python -m tradegenius.crypto)")
        if self.billing_enabled and not self.payment_provider_token:
            out.append("BILLING_ENABLED=true requires PAYMENT_PROVIDER_TOKEN")
        return out


settings = Settings()
