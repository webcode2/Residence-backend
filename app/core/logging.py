import logging
import sys
from app.core.config import settings

def setup_logging():
    """
    Configures structured, production-ready logging.
    In development: DEBUG output.
    In staging/production: INFO output with timestamps and module names.
    """
    is_dev = settings.ENVIRONMENT.lower() == "development"
    log_level = logging.DEBUG if is_dev else logging.INFO
    log_format = "%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d): %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(
        level=log_level,
        format=log_format,
        datefmt=date_format,
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True
    )

    # Silence noisy loggers in production
    if not is_dev:
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)
