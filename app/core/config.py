from pydantic_settings import BaseSettings
from pydantic import ConfigDict, model_validator
from typing import List, Union

class Settings(BaseSettings):
    PROJECT_NAME: str = "Residence SaaS"
    API_V1_STR: str = "/api/v1"
    ENVIRONMENT: str = "development" # "development", "staging", "production"
    ALLOWED_ORIGINS: List[str] = ["*"]

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
    DATABASE_URL: str | None = None
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




    @property
    def async_database_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")
        return f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_SERVER}/{self.POSTGRES_DB}"

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

