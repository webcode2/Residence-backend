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
from app.core.redis import get_monthly_quota_redis


router = APIRouter()

async def _usage_count(app_id: str, db_count: int) -> int:
    """Prefer live Redis quota; fall back to last flushed DB value."""
    redis_count = await get_monthly_quota_redis(app_id)
    return redis_count if redis_count is not None else db_count

@router.get("/tiers", response_model=List[TierInfoSchema])
async def list_available_tiers(db: AsyncSession = Depends(get_db)):
    """
    List all available subscription plans, pricing, quotas, and feature entitlements.
    """
    from app.services.plans import list_plans
    return await list_plans(db)

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
    used = await _usage_count(app_id, sub.current_month_verifications)
    remaining = None
    if sub.monthly_verifications_limit is not None:
        remaining = max(0, sub.monthly_verifications_limit - used)

    return SubscriptionDetailSchema(
        id=sub.id,
        estate_id=sub.estate_id,
        status=sub.status,
        expiry_date=sub.expiry_date,
        tier=sub.tier,
        monthly_verifications_limit=sub.monthly_verifications_limit,
        current_month_verifications=used,
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
    """Plan changes require confirmed payment or a platform-owner override."""
    raise HTTPException(status_code=403, detail="Use checkout to change plans, or contact the platform owner for an adjustment.")

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
    if not payment_service.verify_webhook_signature(
        raw_body,
        x_bachs_signature,
        request.headers.get("x-bachs-timestamp"),
        request.headers.get("x-bachs-signature-v2"),
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid BACHS webhook signature"
        )
    payload = await request.json()
    return await payment_service.handle_webhook(db, payload)


# Card activation is separate from one-time estate subscription purchases.
from pydantic import BaseModel, Field
from typing import Literal

class CardActivationRequest(BaseModel):
    tier: SubscriptionTier = SubscriptionTier.STARTER
    expected_price_monthly: float = Field(gt=0)
    consent: Literal[True]
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None

@router.get('/activation')
async def card_activation_status(app_id: str = Depends(validate_tenant), db: AsyncSession = Depends(get_db),
                                 user: User = Depends(check_roles([UserRole.CARETAKER]))):
    from app.services import card_trials
    return await card_trials.status(db, app_id)

@router.post('/activation')
async def activate_with_card(request: CardActivationRequest, app_id: str = Depends(validate_tenant),
                             db: AsyncSession = Depends(get_db), user: User = Depends(check_roles([UserRole.CARETAKER]))):
    from app.services import card_trials
    return await card_trials.start(db, app_id, user, request)

@router.post('/activation/confirm')
async def confirm_card_activation(app_id: str = Depends(validate_tenant), db: AsyncSession = Depends(get_db),
                                  user: User = Depends(check_roles([UserRole.CARETAKER]))):
    from app.services import card_trials
    return await card_trials.confirm(db, app_id)

@router.post('/activation/cancel')
async def cancel_card_renewal(app_id: str = Depends(validate_tenant), db: AsyncSession = Depends(get_db),
                             user: User = Depends(check_roles([UserRole.CARETAKER]))):
    from app.services import card_trials
    return await card_trials.cancel(db, app_id)
