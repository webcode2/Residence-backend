from pydantic_settings import BaseSettings
from pydantic import ConfigDict, model_validator, field_validator
from typing import List, Union, Any
import json

class Settings(BaseSettings):
    PROJECT_NAME: str = "Residence SaaS"
    API_V1_STR: str = "/api/v1"
    ENVIRONMENT: str = "development" # "development", "staging", "production"
    ALLOWED_ORIGINS: Union[List[str], str] = ["*"]

    @field_validator("ALLOWED_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str):
            v_clean = v.strip().strip("'\"")
            if v_clean.startswith("[") and v_clean.endswith("]"):
                try:
                    return json.loads(v_clean)
                except Exception:
                    pass
            return [i.strip() for i in v_clean.split(",") if i.strip()]
        return v

    SECRET_KEY: str = "supersecretkey" # Change in production
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 8  # 8 days

    # Isolated cryptographic secrets for SaaS Admin (Platform Owner)
    SAAS_ADMIN_SECRET_KEY: str = "saas_admin_isolated_super_secret_jwt_key_prod"
    SAAS_ADMIN_PASSWORD_PEPPER: str = "saas_admin_isolated_password_pepper_secret_salt_prod"


    POSTGRES_SERVER: str = "localhost"
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "residence_db"
    # App traffic (prefer PgBouncer in production). Migrations use DIRECT_DATABASE_URL.
    DATABASE_URL: str | None = None
    DIRECT_DATABASE_URL: str | None = None
    DB_USE_PGBOUNCER: bool = False
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 5
    DB_POOL_RECYCLE: int = 300
    REDIS_URL: str = "redis://localhost:6379/0"

    # Resend Settings
    RESEND_API_KEY: str = ""
    RESEND_SENDER_EMAIL: str = "onboarding@resend.dev"
    RESEND_SENDER_NAME: str = "Residence Support"

    # Bachs Payment Gateway Settings
    BACHS_SECRET_KEY: str = ""
    BACHS_PUBLIC_KEY: str = ""
    BACHS_WEBHOOK_SECRET: str = ""
    BACHS_BASE_URL: str = "https://api.bachs.io"

    # Automated Maintenance & Cron Settings
    ENABLE_MAINTENANCE_SCHEDULER: bool = True
    MAINTENANCE_INTERVAL_SECONDS: int = 3600  # Run maintenance routines every hour
    ACCESS_LOG_DRAIN_INTERVAL_SECONDS: float = 2.0
    ACCESS_LOG_DRAIN_BATCH_SIZE: int = 500
    TOKEN_STATE_DRAIN_INTERVAL_SECONDS: float = 2.0
    TOKEN_STATE_DRAIN_BATCH_SIZE: int = 500

    @field_validator("ENABLE_MAINTENANCE_SCHEDULER", mode="before")
    @classmethod
    def assemble_bool_scheduler(cls, v: Any) -> bool:
        if isinstance(v, str):
            v_clean = v.strip().strip("'\"").lower()
            return v_clean in ("true", "1", "yes", "t", "on")
        return bool(v)

    @field_validator("DB_USE_PGBOUNCER", mode="before")
    @classmethod
    def assemble_bool_pgbouncer(cls, v: Any) -> bool:
        if isinstance(v, str):
            v_clean = v.strip().strip("'\"").lower()
            return v_clean in ("true", "1", "yes", "t", "on")
        return bool(v)

    @property
    def async_database_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")
        return f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_SERVER}/{self.POSTGRES_DB}"

    @property
    def async_direct_database_url(self) -> str:
        """Direct Postgres URL for Alembic / admin ops (bypass PgBouncer)."""
        if self.DIRECT_DATABASE_URL:
            return self.DIRECT_DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")
        return self.async_database_url

    @model_validator(mode="after")
    def validate_production_secrets(self):
        """Enforces that secure random secrets are supplied in production environments."""
        if self.ENVIRONMENT.lower() == "production":
            insecure_defaults = [
                ("SECRET_KEY", self.SECRET_KEY, "supersecretkey"),
                ("SAAS_ADMIN_SECRET_KEY", self.SAAS_ADMIN_SECRET_KEY, "saas_admin_isolated_super_secret_jwt_key_prod"),
                ("SAAS_ADMIN_PASSWORD_PEPPER", self.SAAS_ADMIN_PASSWORD_PEPPER, "saas_admin_isolated_password_pepper_secret_salt_prod"),
            ]
            for name, val, default in insecure_defaults:
                if val == default or len(val) < 24:
                    raise ValueError(
                        f"CRITICAL SECURITY ERROR: '{name}' is using a default or insecurely short secret in production! "
                        f"Please generate and set a cryptographically strong secret in the environment."
                    )
        return self

    model_config = ConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

settings = Settings()

