"""Settings, read from environment variables (and .env via python-dotenv)."""

import os
from dataclasses import dataclass


def _get(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _flag(name: str) -> bool:
    return (_get(name) or "").lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Shared secret Vapi sends with every webhook (Bearer token or X-Vapi-Secret).
    webhook_secret: str | None = None
    # Local development only: accept webhooks with no secret configured.
    allow_insecure: bool = False
    db_path: str = "lineback.db"
    business_name: str = "Tundrel"
    # Owner notifications by email (stdlib SMTP, no extra dependencies).
    owner_email: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_security: str = "starttls"  # starttls | ssl | none
    max_body_bytes: int = 2_000_000

    @classmethod
    def from_env(cls) -> "Settings":
        security = (_get("LINEBACK_SMTP_SECURITY") or "starttls").lower()
        if security not in {"starttls", "ssl", "none"}:
            raise ValueError("LINEBACK_SMTP_SECURITY must be starttls, ssl or none")
        return cls(
            webhook_secret=_get("LINEBACK_WEBHOOK_SECRET"),
            allow_insecure=_flag("LINEBACK_ALLOW_INSECURE"),
            db_path=_get("LINEBACK_DB_PATH") or "lineback.db",
            business_name=_get("LINEBACK_BUSINESS_NAME") or "Tundrel",
            owner_email=_get("LINEBACK_OWNER_EMAIL"),
            smtp_host=_get("LINEBACK_SMTP_HOST"),
            smtp_port=int(_get("LINEBACK_SMTP_PORT") or 587),
            smtp_user=_get("LINEBACK_SMTP_USER"),
            smtp_password=_get("LINEBACK_SMTP_PASSWORD"),
            smtp_from=_get("LINEBACK_SMTP_FROM"),
            smtp_security=security,
        )
