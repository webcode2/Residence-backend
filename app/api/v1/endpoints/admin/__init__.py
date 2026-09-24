from fastapi import APIRouter
from app.api.v1.endpoints.admin.auth import router as auth_router
from app.api.v1.endpoints.admin.analytics import router as analytics_router
from app.api.v1.endpoints.admin.estates import router as estates_router
from app.api.v1.endpoints.admin.subscriptions import router as subscriptions_router
from app.api.v1.endpoints.admin.users import router as users_router
from app.api.v1.endpoints.admin.access_logs import router as access_logs_router

admin_router = APIRouter()

admin_router.include_router(auth_router, prefix="/auth")
admin_router.include_router(analytics_router, prefix="/analytics")
admin_router.include_router(analytics_router, prefix="") # Exposes /overview directly on /admin/overview
admin_router.include_router(estates_router, prefix="/estates")
admin_router.include_router(subscriptions_router, prefix="/subscriptions")
admin_router.include_router(users_router, prefix="/users")
admin_router.include_router(access_logs_router, prefix="/access-logs")

__all__ = ["admin_router"]
