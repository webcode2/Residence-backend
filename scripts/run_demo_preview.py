"""Serve the production database for local testing."""
from pathlib import Path
from sqlalchemy.engine import make_url
from app.core.config import settings

url = make_url(settings.async_direct_database_url)
if url.database != "residence_db":
    raise ValueError("Local testing must use the production database residence_db.")
settings.DATABASE_URL = url.set(host="127.0.0.1", port=5432).render_as_string(hide_password=False)
settings.DIRECT_DATABASE_URL = settings.DATABASE_URL
settings.REDIS_URL = "redis://127.0.0.1:6379/14"
settings.ENABLE_MAINTENANCE_SCHEDULER = False
settings.ENABLE_EMBEDDED_WORKERS = False

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8002, log_level="warning")
