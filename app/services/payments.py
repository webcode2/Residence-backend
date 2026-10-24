import calendar
import hashlib
import hmac
import uuid
import ipaddress
from urllib.parse import urlparse
from datetime import date, datetime, UTC
from decimal import Decimal
from typing import Optional
import httpx
from fastapi import HTTPException
from sqlalchemy import select, or_
from sqlalchemy.orm import selectinload
from app.core.config import settings
from app.core.redis import invalidate_subscription_cache, reset_monthly_quota_redis
from app.models.estate import Estate, Subscription
from app.models.plan import SubscriptionCheckout
from app.services.plans import get_plan, quote


def add_months(value: date, months: int) -> date:
    month = value.month-1+months
    year = value.year+month//12
    month = month%12+1
    return date(year,month,min(value.day,calendar.monthrange(year,month)[1]))


class BachsPaymentService:
    async def create_checkout_session(self, db, app_id, user, checkout_in):
        estate = (await db.execute(select(Estate).where(Estate.app_id==app_id))).scalar_one_or_none()
        if not estate or not (await db.execute(select(Subscription.id).where(Subscription.estate_id==estate.id))).scalar_one_or_none():
            raise HTTPException(404,'Estate subscription not found')
        from app.models.plan import EstateCardTrial
        sub = (await db.execute(select(Subscription).where(Subscription.estate_id == estate.id))).scalar_one()
        recurring = await db.get(EstateCardTrial, app_id)
        if sub.status == 'pending_activation' or (recurring and recurring.subscription_id and recurring.status != 'canceled'):
            raise HTTPException(409, 'Use card activation or manage your existing automatic subscription first.')
        plan = await get_plan(db,checkout_in.tier)
        price = quote(plan,checkout_in.billing_months)
        reference = str(uuid.uuid4())
        payload = {
            'customer':{'email':user.email,'name':user.full_name or estate.name},
            'pricing':{'currency':'USD','amount':f"{price['amount']:.2f}"},
            'metadata':{'checkout_reference':reference,'app_id':app_id,'estate_id':str(estate.id),
                        'tier':plan['tier'],'billing_months':str(checkout_in.billing_months)},
            'reference':reference,
            **self._return_urls(checkout_in),
        }
        simulated = not bool(settings.BACHS_SECRET_KEY)
        if simulated:
            session_id = f'cs_sim_{uuid.uuid4().hex}'
            url = f'https://checkout.bachs.io/session/{session_id}'
        else:
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(f'{settings.BACHS_BASE_URL.rstrip("/")}/v1/checkout-sessions',
                        json=payload,headers={'Authorization':f'Bearer {settings.BACHS_SECRET_KEY}','Idempotency-Key':reference},timeout=15)
                    response.raise_for_status()
                    data = response.json()
                    data = data.get('data',data)
                    session_id = data.get('checkout_id') or data.get('id')
                    if not session_id or not data.get('checkout_url'):
                        raise ValueError('Missing gateway checkout details')
                    url = data['checkout_url']
                    parsed_url = urlparse(url)
                    if parsed_url.scheme != 'https' or parsed_url.hostname not in ('checkout.bachs.io','sandbox-checkout.bachs.io'):
                        raise ValueError('Unexpected gateway checkout address')
            except Exception:
                raise HTTPException(502,'The payment provider could not create checkout. Please retry.')
        db.add(SubscriptionCheckout(session_id=session_id,reference=reference,app_id=app_id,tier=plan['tier'],
            billing_months=checkout_in.billing_months,amount=price['amount'],plan_snapshot=plan,is_simulated=simulated))
        await db.commit()
        return {'session_id':session_id,'checkout_url':url,'tier':plan['tier'],**price,'status':'open','is_simulated':simulated,'is_sandbox':self.is_sandbox,'returns_to_app':bool(self._return_urls(checkout_in))}

    @property
    def is_sandbox(self):
        return settings.BACHS_SECRET_KEY.startswith('sk_sandbox_') and urlparse(settings.BACHS_BASE_URL).hostname=='sandbox-api.bachs.io'

    def _return_urls(self,checkout_in):
        result = {}
        for field in ('success_url','cancel_url'):
            value = getattr(checkout_in,field)
            if not value: continue
            host = urlparse(value).hostname or ''
            local = host.lower()=='localhost'
            try: local = local or not ipaddress.ip_address(host).is_global
            except ValueError: pass
            if local and self.is_sandbox: continue
            if local: raise HTTPException(422,'Payment return URLs must use your public app address.')
            result[field] = value
        return result

    def verify_webhook_signature(
        self,
        raw_body: bytes,
        signature_header: Optional[str] = None,
        timestamp_header: Optional[str] = None,
        v2_header: Optional[str] = None,
    ) -> bool:
        """
        Accept Bachs V2 (`t=<unix>,v1=<hex>` over `timestamp.body`), the legacy
        timestamp header plus signature, and a raw-body HMAC.
        """
        secret = settings.BACHS_WEBHOOK_SECRET
        if not secret:
            return False
        try:
            body = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            return False
        key = secret.encode()
        if v2_header:
            parts = [part.strip().split("=", 1) for part in v2_header.split(",") if "=" in part]
            timestamps = [value for name, value in parts if name == "t"]
            signatures = [value for name, value in parts if name == "v1"]
            if len(timestamps) != 1:
                return False
            return self._signed(key, f"{timestamps[0]}.{body}", signatures, timestamps[0])
        if timestamp_header and signature_header:
            return self._signed(key, f"{timestamp_header}.{body}", [signature_header], timestamp_header)
        if signature_header:
            digest = hmac.new(key, raw_body, hashlib.sha256).hexdigest()
            presented = signature_header.strip()
            if len(presented) != len(digest):
                return False
            return hmac.compare_digest(digest, presented)
        return False

    def _signed(self, key: bytes, message: str, signatures: list, timestamp: str) -> bool:
        if not timestamp.isdigit() or abs(datetime.now(UTC).timestamp() - int(timestamp)) > 300:
            return False
        expected = hmac.new(key, message.encode(), hashlib.sha256).digest()
        for signature in signatures:
            if not isinstance(signature, str) or len(signature) != 64:
                continue
            try:
                candidate = bytes.fromhex(signature)
            except ValueError:
                continue
            if hmac.compare_digest(expected, candidate):
                return True
        return False

    async def fulfill_subscription(self, db, app_id, new_tier, billing_months=1, snapshot=None, commit=True):
        estate = (await db.execute(select(Estate).where(Estate.app_id==app_id))).scalar_one_or_none()
        if not estate: raise HTTPException(404,'Estate not found')
        sub = (await db.execute(select(Subscription).where(Subscription.estate_id==estate.id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
        if not sub: raise HTTPException(404,'Subscription not found')
        plan = snapshot or await get_plan(db,new_tier)
        for field in ('tier','monthly_verifications_limit','rfid_enabled','log_retention_days','realtime_alerts_enabled','price_monthly'):
            setattr(sub,field,plan[field])
        sub.status = 'active'
        today = date.today()
        sub.expiry_date = add_months(max(sub.expiry_date or today,today),billing_months)
        sub.current_month_verifications = 0
        sub.billing_cycle_start = today
        if commit:
            await db.commit()
            await self._invalidate(app_id)
        return sub

    async def _invalidate(self,app_id):
        await invalidate_subscription_cache(app_id)
        await reset_monthly_quota_redis(app_id)

    async def _fulfill_checkout(self, db, session_id, app_id=None):
        checkout = (await db.execute(select(SubscriptionCheckout).where(SubscriptionCheckout.session_id==session_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
        if not checkout or (app_id and checkout.app_id!=app_id):
            raise HTTPException(404,'Checkout not found for this estate')
        if checkout.is_simulated and settings.ENVIRONMENT.lower()=='production':
            raise HTTPException(400,'Simulated payments cannot activate production subscriptions.')
        if checkout.fulfilled_at:
            estate = (await db.execute(select(Estate).where(Estate.app_id==checkout.app_id).options(selectinload(Estate.subscription)))).scalar_one()
            sub = estate.subscription
        else:
            sub = await self.fulfill_subscription(db,checkout.app_id,checkout.tier,checkout.billing_months,checkout.plan_snapshot,commit=False)
            checkout.fulfilled_at = datetime.now(UTC).replace(tzinfo=None)
            await db.commit()
            await self._invalidate(checkout.app_id)
        return {'session_id':checkout.session_id,'status':'paid','tier':checkout.tier,'is_active':True,
                'message':'Payment verified and subscription activated.','subscription':sub}

    async def verify_session(self, db, session_id, app_id):
        checkout = await db.get(SubscriptionCheckout,session_id)
        if not checkout or checkout.app_id != app_id:
            raise HTTPException(404,'Checkout not found for this estate')
        if checkout.fulfilled_at or checkout.is_simulated:
            return await self._fulfill_checkout(db,session_id,app_id)
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f'{settings.BACHS_BASE_URL.rstrip("/")}/v1/checkout-sessions/{session_id}',
                    headers={'Authorization':f'Bearer {settings.BACHS_SECRET_KEY}'},timeout=15)
                response.raise_for_status()
                data = response.json()
                data = data.get('data',data)
        except Exception:
            raise HTTPException(502,'Unable to verify payment with the provider. Please retry.')
        if str(data.get('payment_status','')).lower()=='succeeded' or str((data.get('charge') or {}).get('status','')).lower()=='succeeded' or str(data.get('status','')).lower()=='paid':
            self._check_amount(checkout,data,required=True)
            return await self._fulfill_checkout(db,session_id,app_id)
        return {'session_id':session_id,'status':data.get('status','pending'),'tier':checkout.tier,
                'is_active':False,'message':'Payment is awaiting confirmation.'}

    def _check_amount(self,checkout,data,required=False):
        pricing = data.get('pricing') or {}
        amount = pricing.get('amount',data.get('amount'))
        currency = pricing.get('currency',data.get('currency'))
        if required and (amount is None or currency is None):
            raise HTTPException(400,'Payment confirmation is missing its amount or currency.')
        if amount is not None and Decimal(str(amount)) != Decimal(str(checkout.amount)):
            raise HTTPException(400,'Payment amount does not match the saved checkout.')
        if currency is not None and currency.upper()!='USD':
            raise HTTPException(400,'Payment currency does not match the saved checkout.')

    async def handle_webhook(self, db, payload):
        event = payload.get('event') or payload.get('type')
        from app.services.card_trials import handle_trial_event
        trial_result = await handle_trial_event(db, payload)
        if trial_result is not None:
            return trial_result
        if event not in ('collection.succeeded','checkout.completed','checkout.session.completed','payment.succeeded','payment.completed','charge.successful'):
            return {'status':'ignored','reason':'Event does not confirm payment'}
        data = payload.get('data',{})
        metadata = data.get('metadata') or payload.get('metadata',{})
        checkout = (await db.execute(select(SubscriptionCheckout).where(or_(
            SubscriptionCheckout.session_id==str(data.get('checkout_id') or data.get('checkout_session_id') or data.get('id','')),
            SubscriptionCheckout.reference==str(metadata.get('checkout_reference') or data.get('reference','')),
        )))).scalar_one_or_none()
        if not checkout:
            return {'status':'ignored','reason':'Unknown checkout'}
        # Reconcile with the authenticated checkout object. Event amounts may be
        # in a buyer's converted currency; the checkout carries our quoted currency.
        result = await self.verify_session(db,checkout.session_id,checkout.app_id)
        if not result['is_active']:
            if event in ('checkout.completed','checkout.session.completed'):
                return {'status':'pending','reason':'Checkout completed without confirmed payment'}
            raise HTTPException(503,'Payment confirmation is not available yet. Retry this event.')
        return {'status':'success','event':event,'app_id':checkout.app_id,'tier':checkout.tier,
                'expiry_date':str(result['subscription'].expiry_date)}

payment_service = BachsPaymentService()
