"""Runtime settings, read once from environment variables. See .env.example."""

import os


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
        self.gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()

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

        self.data_dir = os.getenv("DATA_DIR", "/data")
        self.max_active_engines = _int("MAX_ACTIVE_ENGINES", 3)
        self.max_accounts_per_user = _int("MAX_ACCOUNTS_PER_USER", 1)
        self.backtest_bars = _int("BACKTEST_BARS", 3000)

        # Wine / MT5 locations inside the worker container.
        self.wine_python = os.getenv("WINE_PYTHON", r"C:\Python311\python.exe")
        self.mt5_base_dir = os.getenv("MT5_BASE_DIR", "")  # set by entrypoint
        self.engine_script = os.getenv("ENGINE_SCRIPT", r"Z:\app\engine\engine.py")
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
