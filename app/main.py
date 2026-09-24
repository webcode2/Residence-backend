import time
import uuid
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, status, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine
from app.core.redis import get_redis_client, close_redis
from app.core.logging import setup_logging
from app.api.v1.endpoints import verify, auth, users, tokens, estates, landlords, subscriptions, access_logs, payments
from app.api.v1.endpoints.admin import admin_router

from app.core.scheduler import start_maintenance_scheduler, stop_maintenance_scheduler

logger = logging.getLogger(__name__)

# --- 1. Lifespan (Startup & Graceful Shutdown) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info(f"Starting {settings.PROJECT_NAME} in [{settings.ENVIRONMENT.upper()}] mode")
    start_maintenance_scheduler()
    yield
    logger.info(f"Initiating graceful shutdown for {settings.PROJECT_NAME}...")
    await stop_maintenance_scheduler()
    await close_redis()
    await engine.dispose()
    logger.info("Maintenance scheduler, Database, and Redis connections successfully closed.")


app = FastAPI(
    title=settings.PROJECT_NAME,
    lifespan=lifespan,
    docs_url="/docs" if settings.ENVIRONMENT.lower() != "production" else None,
    redoc_url="/redoc" if settings.ENVIRONMENT.lower() != "production" else None
)

# --- 2. CORS Middleware ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 3. Request Lifecycle Middleware (Tracing & Security Headers) ---
@app.middleware("http")
async def request_lifecycle_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    start_time = time.perf_counter()

    response = await call_next(request)

    duration_ms = (time.perf_counter() - start_time) * 1000
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time-Ms"] = f"{duration_ms:.2f}"

    # Essential Production Security Headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if settings.ENVIRONMENT.lower() == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    return response

# --- 4. Global Exception Handlers (Standardized & Secure JSON Errors) ---
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    req_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "status_code": exc.status_code,
            "request_id": req_id
        },
        headers=exc.headers or {}
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    req_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": exc.errors(),
            "status_code": 422,
            "request_id": req_id
        }
    )

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    req_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    logger.exception(f"Unhandled Server Error [Request ID: {req_id}]: {exc}")
    detail_msg = (
        str(exc) if settings.ENVIRONMENT.lower() == "development"
        else f"Internal server error. Please quote Request ID: {req_id}"
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": detail_msg,
            "status_code": 500,
            "request_id": req_id
        }
    )

# --- 5. Application Routers ---
app.include_router(auth.router, prefix=f"{settings.API_V1_STR}/auth", tags=["auth"])
app.include_router(verify.router, prefix=f"{settings.API_V1_STR}/verify", tags=["profiling"])
app.include_router(users.router, prefix=f"{settings.API_V1_STR}/users")
app.include_router(tokens.router, prefix=f"{settings.API_V1_STR}/tokens")
app.include_router(estates.router, prefix=f"{settings.API_V1_STR}/estates", tags=["super_admin"])
app.include_router(landlords.router, prefix=f"{settings.API_V1_STR}/landlords", tags=["landlords"])
app.include_router(subscriptions.router, prefix=f"{settings.API_V1_STR}/subscriptions", tags=["subscriptions"])
app.include_router(payments.router, prefix=f"{settings.API_V1_STR}/payments", tags=["payments"])
app.include_router(access_logs.router, prefix=f"{settings.API_V1_STR}/access-logs", tags=["access_logs"])
app.include_router(admin_router, prefix=f"{settings.API_V1_STR}/admin", tags=["saas_admin"])

# --- 6. Health & Readiness Probes (Orchestration & Uptime) ---
@app.get("/", tags=["health"])
@app.get("/healthz", tags=["health"])
async def health_check():
    """Liveness probe: verifies that the HTTP server is responsive."""
    return {
        "status": "healthy",
        "app": settings.PROJECT_NAME,
        "environment": settings.ENVIRONMENT
    }

@app.get("/ready", tags=["health"])
async def readiness_check():
    """Readiness probe: validates live connectivity to PostgreSQL and Redis with 3s timeout."""
    status_report = {"status": "ready", "database": "unknown", "redis": "unknown"}

    # Validate Database connectivity with timeout
    try:
        async with asyncio.timeout(3.0):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        status_report["database"] = "healthy"
    except Exception as e:
        status_report["database"] = f"unhealthy: {str(e)}"
        status_report["status"] = "not_ready"

    # Validate Redis connectivity with timeout
    try:
        redis_client = get_redis_client()
        if redis_client:
            async with asyncio.timeout(3.0):
                await redis_client.ping()
            status_report["redis"] = "healthy"
        else:
            status_report["redis"] = "not_configured"
    except Exception as e:
        status_report["redis"] = f"unhealthy: {str(e)}"
        status_report["status"] = "not_ready"

    if status_report["status"] != "ready":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=status_report
        )

    return status_report


