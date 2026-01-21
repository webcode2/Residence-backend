from pydantic_settings import BaseSettings
from pydantic import ConfigDict
from typing import List

class Settings(BaseSettings):
    PROJECT_NAME: str = "Residence SaaS"
    API_V1_STR: str = "/api/v1"
    SECRET_KEY: str = "supersecretkey" # Change in production
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 8  # 8 days

    POSTGRES_SERVER: str = "localhost"
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "residence_db"
    DATABASE_URL: str | None = None

    # SendPulse Settings
    SENDPULSE_API_ID: str = ""
    SENDPULSE_API_SECRET: str = ""
    SENDPULSE_SENDER_EMAIL: str = "no-reply@residence.com"
    SENDPULSE_SENDER_NAME: str = "Residence Support"

    @property
    def async_database_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")
        return f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_SERVER}/{self.POSTGRES_DB}"

    model_config = ConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

settings = Settings()
