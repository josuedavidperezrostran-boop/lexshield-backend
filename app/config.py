import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# LexShield/backend/app/config.py → repo root .env (not CWD-relative)
ROOT_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT_DIR / ".env"

load_dotenv(ENV_FILE, override=False)

DEFAULT_TOKEN_SECRET = "change-me-in-production"


class Settings(BaseSettings):
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""
    partners_db_path: str = ""
    access_token_secret: str = DEFAULT_TOKEN_SECRET
    frontend_origin: str = "https://frontend-1-lex-shield2.vercel.app"
    backend_origin: str = "http://localhost:8000"
    subscription_amount_cents: int = 10_000
    max_upload_bytes: int = 10 * 1024 * 1024
    # One gpt-4o pass. Leaves room inside the 128k window for the system prompt and the reply.
    max_contract_chars: int = 320_000
    dev_bypass_billing: bool = False
    # Seconds a Stripe subscription lookup is reused; capped at 600 in app/subscriptions.py.
    subscription_cache_seconds: int = 300
    # Shared state across processes (app/store.py) and outgoing mail (app/mail.py).
    shared_store_url: str = ""
    mail_backend: str = ""
    # Use the first X-Forwarded-For address for rate limits. Only behind a proxy that sets it.
    trust_forwarded_for: bool = False
    # Optional comma-separated Stripe ids that also count as LexShield Professional.
    stripe_authorized_product_ids: str = ""
    stripe_authorized_price_ids: str = ""
    # "production" turns off every dev shortcut. Render also sets RENDER=true on its hosts.
    environment: str = ""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    def openai_key(self) -> str:
        return self.openai_api_key.strip()

    def is_production(self) -> bool:
        if self.environment.strip().lower() in {"production", "prod"}:
            return True
        return os.environ.get("RENDER", "").strip().lower() == "true"

    def billing_bypass_enabled(self) -> bool:
        """The demo bypass only works off production, whatever DEV_BYPASS_BILLING says."""
        return self.dev_bypass_billing and not self.is_production()

    def token_secret_is_safe(self) -> bool:
        secret = self.access_token_secret.strip()
        return bool(secret) and secret != DEFAULT_TOKEN_SECRET


settings = Settings()
