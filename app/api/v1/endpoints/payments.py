from fastapi import APIRouter, Depends, HTTPException, status, Request, Header
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.core.database import get_db
from app.dependencies import validate_tenant, check_roles
from app.models.user import User, UserRole
from app.schemas.subscription import (
    CheckoutSessionCreateSchema,
    CheckoutSessionResponseSchema,
    PaymentVerificationResponseSchema,
    SubscriptionDetailSchema
)
from app.services.payments import payment_service

router = APIRouter()

@router.post("/checkout", response_model=CheckoutSessionResponseSchema, tags=["payments"])
async def create_checkout_session(
    checkout_in: CheckoutSessionCreateSchema,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_roles([UserRole.CARETAKER]))
):
    """
    Create a hosted checkout session with BACHS payment gateway for the estate admin.
    """
    return await payment_service.create_checkout_session(db, app_id, current_user, checkout_in)

@router.get("/checkout/{session_id}/verify", response_model=PaymentVerificationResponseSchema, tags=["payments"])
async def verify_checkout_session(
    session_id: str,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_roles([UserRole.CARETAKER]))
):
    """
    Verify payment status for a BACHS checkout session upon return redirect and fulfill subscription.
    """
    res = await payment_service.verify_session(db, session_id, app_id)
    sub = res.get("subscription")
    sub_schema = None
    if sub:
        remaining = None
        if sub.monthly_verifications_limit is not None:
            remaining = max(0, sub.monthly_verifications_limit - sub.current_month_verifications)
        sub_schema = SubscriptionDetailSchema(
            id=sub.id,
            estate_id=sub.estate_id,
            status=sub.status,
            expiry_date=sub.expiry_date,
            tier=sub.tier,
            monthly_verifications_limit=sub.monthly_verifications_limit,
            current_month_verifications=sub.current_month_verifications,
            verifications_remaining=remaining,
            billing_cycle_start=sub.billing_cycle_start,
            rfid_enabled=sub.rfid_enabled,
            log_retention_days=sub.log_retention_days,
            realtime_alerts_enabled=sub.realtime_alerts_enabled,
            price_monthly=float(sub.price_monthly)
        )
    return PaymentVerificationResponseSchema(
        session_id=res["session_id"],
        status=res["status"],
        tier=res["tier"],
        is_active=res["is_active"],
        message=res["message"],
        subscription=sub_schema
    )

@router.post("/webhook/bachs", tags=["payments"])
async def bachs_webhook(
    request: Request,
    x_bachs_signature: Optional[str] = Header(None, alias="X-Bachs-Signature"),
    db: AsyncSession = Depends(get_db)
):
    """
    Public webhook receiver for BACHS payment events (e.g., collection.succeeded).
    Verifies HMAC-SHA256 signature and activates or extends the estate subscription.
    """
    raw_body = await request.body()
    if not payment_service.verify_webhook_signature(raw_body, x_bachs_signature):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid BACHS webhook signature"
        )
    payload = await request.json()
    return await payment_service.handle_webhook(db, payload)
