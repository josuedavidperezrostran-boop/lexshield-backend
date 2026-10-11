from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# LexShield/backend/app/config.py → repo root .env (not CWD-relative)
ROOT_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT_DIR / ".env"

load_dotenv(ENV_FILE, override=False)


class Settings(BaseSettings):
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    access_token_secret: str = "change-me-in-production"
    frontend_origin: str = "http://localhost:3000"
    backend_origin: str = "http://localhost:8000"
    subscription_amount_cents: int = 100_000
    max_upload_bytes: int = 10 * 1024 * 1024
    # One gpt-4o pass. Leaves room inside the 128k window for the system prompt and the reply.
    max_contract_chars: int = 320_000
    dev_bypass_billing: bool = False

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    def openai_key(self) -> str:
        return self.openai_api_key.strip()


settings = Settings()
