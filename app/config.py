"""
Configuration module for WhatsApp AI Reminder Application.
Uses pydantic-settings to load environment variables and provide typed defaults.
"""
from typing import Literal, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Application Info
    APP_NAME: str = "WhatsApp AI Reminder Automation"
    APP_VERSION: str = "2.0.0"
    ENVIRONMENT: str = "production"
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # JWT Authentication & Multi-Tenancy
    JWT_SECRET_KEY: str = "wh4ts4pp_ai_rem1nder_s44s_jwt_s3cr3t_k3y_2026!"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./reminders.db"

    # Google Gemini LLM Settings
    GEMINI_API_KEY: Optional[str] = None
    GEMINI_MODEL: str = "gemini-1.5-flash"
    GEMINI_TIMEOUT_SECONDS: float = 12.0

    # WhatsApp Gateway Selection: "meta", "bridge", or "mock" (dry-run)
    WHATSAPP_GATEWAY_TYPE: Literal["meta", "bridge", "mock"] = "meta"

    # Meta Cloud API Credentials (Official Graph API)
    META_PHONE_NUMBER_ID: Optional[str] = None
    META_WHATSAPP_TOKEN: Optional[str] = None
    META_GRAPH_VERSION: str = "v21.0"
    META_VERIFY_TOKEN: str = "my_secure_webhook_verify_token"
    WHATSAPP_WEBHOOK_VERIFY_TOKEN: str = "my_secure_webhook_verify_token"

    # Open-Source QR Bridge Settings (e.g. WPPConnect / Baileys / Evolution API)
    BRIDGE_API_URL: Optional[str] = "http://localhost:8080"
    BRIDGE_API_KEY: Optional[str] = None

    # Anti-Ban & Scheduler Controls
    SCHEDULER_INTERVAL_SECONDS: int = 60
    RATE_LIMIT_MIN_JITTER_SECONDS: float = 3.0
    RATE_LIMIT_MAX_JITTER_SECONDS: float = 8.0
    MAX_BATCH_SIZE: int = 20
    DEFAULT_REMINDER_LEAD_HOURS: int = 24
    DEFAULT_COUNTRY_CODE: str = "+91"

    # Multi-tenant Defaults
    DEFAULT_CLIENT_ID: str = "default_tenant"
    DEFAULT_BUSINESS_NAME: str = "Appointments Desk"
    DEFAULT_BUSINESS_TYPE: str = "Clinic / Service"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
