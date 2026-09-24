import httpx
import uuid
import hmac
import hashlib
from datetime import date, timedelta
from typing import Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from fastapi import HTTPException, status
import logging

from app.core.config import settings
from app.core.tiers import SubscriptionTier, get_tier_config
from app.core.redis import invalidate_subscription_cache
from app.models.estate import Estate, Subscription
from app.models.user import User
from app.schemas.subscription import CheckoutSessionCreateSchema
from app.services.notifications import notification_service

logger = logging.getLogger(__name__)

class BachsPaymentService:
    """
    Integration client for BACHS Payment Gateway (https://bachs.io).
    Manages Checkout Sessions, Webhook Verification, and Subscription Fulfillment for Estate Admins.
    """
    def __init__(self):
        self.secret_key = settings.BACHS_SECRET_KEY
        self.public_key = settings.BACHS_PUBLIC_KEY
        self.webhook_secret = settings.BACHS_WEBHOOK_SECRET
        self.base_url = settings.BACHS_BASE_URL.rstrip("/")

    async def create_checkout_session(
        self,
        db: AsyncSession,
        app_id: str,
        user: User,
        checkout_in: CheckoutSessionCreateSchema
    ) -> Dict[str, Any]:
        """
        Creates a hosted Bachs checkout session for the estate admin to subscribe or upgrade tier.
        """
        # 1. Fetch estate details
        estate_result = await db.execute(
            select(Estate).where(Estate.app_id == app_id).options(selectinload(Estate.subscription))
        )
        estate = estate_result.scalar_one_or_none()
        if not estate or not estate.subscription:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Estate or subscription not found"
            )

        tier_cfg = get_tier_config(checkout_in.tier)
        amount = float(tier_cfg["price_monthly"])

        payload = {
            "customer": {
                "email": user.email,
                "name": user.full_name or f"Admin for {estate.name}"
            },
            "pricing": {
                "currency": "USD",
                "amount": f"{amount:.2f}"
            },
            "metadata": {
                "estate_id": str(estate.id),
                "app_id": estate.app_id,
                "tier": tier_cfg["tier"],
                "admin_id": str(user.id),
                "admin_email": user.email,
                "estate_name": estate.name
            },
            "success_url": checkout_in.success_url or "https://residence.com/dashboard/billing/success",
            "cancel_url": checkout_in.cancel_url or "https://residence.com/dashboard/billing/cancel"
        }

        # 2. Live API Call if secret key is configured
        if self.secret_key:
            async with httpx.AsyncClient() as client:
                try:
                    response = await client.post(
                        f"{self.base_url}/v1/checkout-sessions",
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {self.secret_key}",
                            "Content-Type": "application/json"
                        },
                        timeout=15.0
                    )
                    response.raise_for_status()
                    data = response.json()
                    session_id = data.get("id") or data.get("data", {}).get("id")
                    checkout_url = data.get("checkout_url") or data.get("data", {}).get("checkout_url") or f"https://checkout.bachs.io/{session_id}"
                    return {
                        "session_id": session_id,
                        "checkout_url": checkout_url,
                        "tier": tier_cfg["tier"],
                        "amount": amount,
                        "currency": "USD",
                        "status": data.get("status", "open"),
                        "is_simulated": False
                    }
                except Exception as e:
                    logger.error(f"Bachs checkout session creation failed: {e}")
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=f"Bachs payment gateway error: {str(e)}"
                    )

        # 3. Simulated Sandbox Mode (when no live API key is configured)
        sim_id = f"cs_sim_{uuid.uuid4().hex[:12]}"
        sim_url = f"https://checkout.bachs.io/session/{sim_id}"
        logger.info(
            f"[BACHS SIMULATION] Checkout session created for {user.email} (Estate: {estate.app_id}, Tier: {tier_cfg['tier']}, ${amount})"
        )
        return {
            "session_id": sim_id,
            "checkout_url": sim_url,
            "tier": tier_cfg["tier"],
            "amount": amount,
            "currency": "USD",
            "status": "open",
            "is_simulated": True
        }

    def verify_webhook_signature(self, raw_body: bytes, signature_header: Optional[str]) -> bool:
        """
        Verifies HMAC-SHA256 signature sent by Bachs in the webhook header.
        """
        if not self.webhook_secret:
            # Sandbox/dev without webhook secret configured
            return True
        if not signature_header:
            return False

        computed = hmac.new(
            self.webhook_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(computed, signature_header)

    async def fulfill_subscription(
        self,
        db: AsyncSession,
        app_id: str,
        new_tier: str | SubscriptionTier
    ) -> Subscription:
        """
        Applies payment fulfillment: upgrades tier, extends expiry by 30 days, resets quota, and refreshes cache.
        """
        estate_result = await db.execute(
            select(Estate).where(Estate.app_id == app_id).options(selectinload(Estate.subscription))
        )
        estate = estate_result.scalar_one_or_none()
        if not estate or not estate.subscription:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Estate {app_id} or subscription not found for fulfillment"
            )

        sub = estate.subscription
        tier_cfg = get_tier_config(new_tier)

        # Update tier and limits
        sub.tier = tier_cfg["tier"]
        sub.status = "active"
        sub.monthly_verifications_limit = tier_cfg["monthly_verifications_limit"]
        sub.rfid_enabled = tier_cfg["rfid_enabled"]
        sub.log_retention_days = tier_cfg["log_retention_days"]
        sub.realtime_alerts_enabled = tier_cfg["realtime_alerts_enabled"]
        sub.price_monthly = tier_cfg["price_monthly"]

        # Extend expiry date by 30 days from now (or current expiry if still active in future)
        today = date.today()
        base_date = sub.expiry_date if (sub.expiry_date and sub.expiry_date > today) else today
        sub.expiry_date = base_date + timedelta(days=30)

        # Reset verifications for new billing cycle
        sub.current_month_verifications = 0
        sub.billing_cycle_start = today

        await db.commit()
        await invalidate_subscription_cache(app_id)
        from app.core.redis import reset_monthly_quota_redis
        await reset_monthly_quota_redis(app_id)
        await db.refresh(sub)

        logger.info(
            f"[BACHS FULFILLMENT] Estate {app_id} successfully upgraded/renewed to {sub.tier} (Expires: {sub.expiry_date})"
        )
        return sub

    async def handle_webhook(self, db: AsyncSession, payload: dict) -> Dict[str, Any]:
        """
        Processes inbound Bachs webhook notifications (e.g. collection.succeeded, checkout.session.completed).
        """
        event_type = payload.get("event") or payload.get("type", "")
        data = payload.get("data", {})
        metadata = data.get("metadata", {}) or payload.get("metadata", {})

        app_id = metadata.get("app_id")
        target_tier = metadata.get("tier")

        if not app_id:
            # Check if estate_id was provided
            estate_id_str = metadata.get("estate_id")
            if estate_id_str:
                res = await db.execute(select(Estate).where(Estate.id == uuid.UUID(estate_id_str)))
                est = res.scalar_one_or_none()
                if est:
                    app_id = est.app_id

        if not app_id:
            logger.warning(f"Bachs webhook event {event_type} received without recognizable app_id or estate_id")
            return {"status": "ignored", "reason": "Missing tenant metadata"}

        valid_events = [
            "collection.succeeded",
            "checkout.session.completed",
            "payment.succeeded",
            "payment.completed",
            "charge.successful"
        ]

        if event_type in valid_events:
            tier_to_apply = target_tier or SubscriptionTier.STANDARD.value
            sub = await self.fulfill_subscription(db, app_id, tier_to_apply)
            return {
                "status": "success",
                "event": event_type,
                "app_id": app_id,
                "tier": sub.tier,
                "expiry_date": str(sub.expiry_date)
            }

        return {"status": "ignored", "reason": f"Unhandled event type: {event_type}"}

    async def verify_session(
        self,
        db: AsyncSession,
        session_id: str,
        app_id: str
    ) -> Dict[str, Any]:
        """
        Verifies a checkout session with Bachs or simulates activation upon user return.
        """
        # If simulated session
        if session_id.startswith("cs_sim_") or not self.secret_key:
            estate_result = await db.execute(
                select(Estate).where(Estate.app_id == app_id).options(selectinload(Estate.subscription))
            )
            estate = estate_result.scalar_one_or_none()
            if not estate or not estate.subscription:
                raise HTTPException(status_code=404, detail="Estate subscription not found")

            # In simulation, activate to standard if starter or extend current
            sub = await self.fulfill_subscription(db, app_id, estate.subscription.tier)
            return {
                "session_id": session_id,
                "status": "paid",
                "tier": sub.tier,
                "is_active": True,
                "message": "Payment verified and subscription activated successfully (Simulated).",
                "subscription": sub
            }

        # Live verification via Bachs API
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.base_url}/v1/checkout-sessions/{session_id}",
                    headers={"Authorization": f"Bearer {self.secret_key}"},
                    timeout=10.0
                )
                response.raise_for_status()
                data = response.json()
                payment_status = data.get("status") or data.get("data", {}).get("status")
                metadata = data.get("metadata") or data.get("data", {}).get("metadata", {})
                target_tier = metadata.get("tier", SubscriptionTier.STANDARD.value)

                if payment_status in ["paid", "completed", "succeeded"]:
                    sub = await self.fulfill_subscription(db, app_id, target_tier)
                    return {
                        "session_id": session_id,
                        "status": "paid",
                        "tier": sub.tier,
                        "is_active": True,
                        "message": "Payment verified and subscription activated successfully.",
                        "subscription": sub
                    }

                return {
                    "session_id": session_id,
                    "status": payment_status,
                    "tier": target_tier,
                    "is_active": False,
                    "message": f"Payment is currently {payment_status}."
                }
            except Exception as e:
                logger.error(f"Failed to verify session {session_id} on Bachs: {e}")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Bachs verification error: {str(e)}"
                )

payment_service = BachsPaymentService()
