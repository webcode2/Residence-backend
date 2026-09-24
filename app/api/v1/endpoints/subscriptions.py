from fastapi import APIRouter, Depends, HTTPException, status, Request, Header
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
from app.core.database import get_db
from app.core.tiers import SubscriptionTier, TIER_DETAILS, get_tier_config
from app.schemas.subscription import (
    TierInfoSchema,
    SubscriptionDetailSchema,
    ChangeTierRequestSchema,
    CheckoutSessionCreateSchema,
    CheckoutSessionResponseSchema,
    PaymentVerificationResponseSchema
)
from app.services import estates as estate_service
from app.dependencies import validate_tenant, check_roles, get_current_user
from app.models.user import User, UserRole


router = APIRouter()

@router.get("/tiers", response_model=List[TierInfoSchema])
async def list_available_tiers():
    """
    List all available subscription plans, pricing, quotas, and feature entitlements.
    """
    return [TierInfoSchema(**details) for details in TIER_DETAILS.values()]

@router.get("/current", response_model=SubscriptionDetailSchema)
async def get_current_subscription(
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_roles([UserRole.CARETAKER]))
):
    """
    View the current estate's subscription tier, usage, and limits.
    Only accessible by Caretakers (Estate Admins).
    """
    estate = await estate_service.get_estate_by_app_id(db, app_id)
    if not estate or not estate.subscription:
        raise HTTPException(status_code=404, detail="Subscription not found for this estate")

    sub = estate.subscription
    remaining = None
    if sub.monthly_verifications_limit is not None:
        remaining = max(0, sub.monthly_verifications_limit - sub.current_month_verifications)

    return SubscriptionDetailSchema(
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

@router.post("/change-tier", response_model=SubscriptionDetailSchema)
async def change_subscription_tier(
    req: ChangeTierRequestSchema,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_roles([UserRole.CARETAKER]))
):
    """
    Upgrade or switch the current estate's subscription tier.
    Only accessible by Caretakers (Estate Admins).
    """
    sub = await estate_service.change_estate_tier(db, app_id, req.tier)
    remaining = None
    if sub.monthly_verifications_limit is not None:
        remaining = max(0, sub.monthly_verifications_limit - sub.current_month_verifications)

    return SubscriptionDetailSchema(
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

@router.post("/checkout", response_model=CheckoutSessionResponseSchema)
async def create_bachs_checkout(
    checkout_in: CheckoutSessionCreateSchema,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_roles([UserRole.CARETAKER]))
):
    """
    Initiate a subscription checkout session using the BACHS payment gateway.
    Returns a hosted checkout URL for the estate admin to complete payment.
    """
    from app.services.payments import payment_service
    return await payment_service.create_checkout_session(db, app_id, current_user, checkout_in)

@router.get("/checkout/{session_id}/verify", response_model=PaymentVerificationResponseSchema)
async def verify_bachs_checkout(
    session_id: str,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_roles([UserRole.CARETAKER]))
):
    """
    Verify payment status for a Bachs checkout session and fulfill the estate subscription.
    """
    from app.services.payments import payment_service
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

@router.post("/webhook/bachs")
async def bachs_subscription_webhook(
    request: Request,
    x_bachs_signature: Optional[str] = Header(None, alias="X-Bachs-Signature"),
    db: AsyncSession = Depends(get_db)
):
    """
    Inbound webhook from BACHS payment gateway.
    Verifies HMAC-SHA256 signature and activates/renews subscription upon payment success.
    """
    from app.services.payments import payment_service
    raw_body = await request.body()
    if not payment_service.verify_webhook_signature(raw_body, x_bachs_signature):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid BACHS webhook signature"
        )
    payload = await request.json()
    return await payment_service.handle_webhook(db, payload)

