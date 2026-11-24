"""Hosted card activation. Bachs owns card storage, scheduling and retries."""
from datetime import datetime, UTC
from decimal import Decimal
from urllib.parse import urlparse, quote
import hashlib
import httpx
from fastapi import HTTPException
from sqlalchemy import select
from app.core.config import settings
from app.core.redis import invalidate_subscription_cache
from app.models.estate import Estate, Subscription
from app.models.plan import EstateCardTrial
from app.services.plans import get_plan


def now():
    return datetime.now(UTC).replace(tzinfo=None)


async def gateway(method, path, body=None, key=None):
    if not settings.BACHS_SECRET_KEY:
        raise HTTPException(503, 'Card activation is unavailable until the payment gateway is configured.')
    headers = {'Authorization': f'Bearer {settings.BACHS_SECRET_KEY}'}
    if key:
        headers['Idempotency-Key'] = key
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.request(method, settings.BACHS_BASE_URL.rstrip('/') + '/v1/' + path,
                                     json=body, headers=headers)
            if r.status_code == 404:
                raise HTTPException(404, 'The card provider has no record of this checkout.')
            r.raise_for_status()
            data = r.json()
            return data.get('data', data)
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, 'Unable to reach the card provider. Please retry.')


async def estate_subscription(db, app_id, lock=False):
    stmt = select(Subscription).join(Estate).where(Estate.app_id == app_id)
    if lock:
        stmt = stmt.with_for_update()
    sub = (await db.execute(stmt)).scalar_one_or_none()
    if not sub:
        raise HTTPException(404, 'Estate subscription not found.')
    return sub


async def start(db, app_id, user, request):
    sub = await estate_subscription(db, app_id, lock=True)
    row = await db.get(EstateCardTrial, app_id)
    if sub.status != 'pending_activation' or (row and row.subscription_id):
        raise HTTPException(409, 'This estate has already been activated. A second free trial is unavailable.')
    previous_session = None
    if row and row.session_id:
        try:
            existing = await gateway('GET', 'checkout-sessions/' + row.session_id)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            # Unknown to the configured Bachs account (keys switched): its product is gone too.
            existing, row.product_id = {'status': 'expired'}, None
        if existing.get('status') not in ('expired', 'cancelled'):
            return {'checkout_url': row.checkout_url, 'session_id': row.session_id}
        previous_session = row.session_id
    if row and (request.tier.value != row.plan_snapshot['tier'] or Decimal(str(request.expected_price_monthly)) != Decimal(str(row.plan_snapshot['price_monthly']))):
        raise HTTPException(409, 'Resume the original plan and price accepted for this estate activation.')
    plan = await get_plan(db, request.tier)
    if not row and Decimal(str(plan['price_monthly'])) != Decimal(str(request.expected_price_monthly)):
        raise HTTPException(409, 'The plan price changed. Refresh and review the new amount before consenting.')
    if not row:
        row = EstateCardTrial(app_id=app_id, plan_snapshot=plan, consent_at=now(),
            consent_user_id=str(user.id), trial_days=settings.ESTATE_TRIAL_DAYS,
            status='pending', cancel_at_period_end=False)
        db.add(row)
        await db.flush()
    # Persist the consent and immutable price before creating remote resources.
    await db.commit()
    digest = hashlib.sha256(f'{app_id}:{row.consent_at.isoformat()}'.encode()).hexdigest()[:32]
    if previous_session:
        digest = hashlib.sha256((digest + previous_session).encode()).hexdigest()[:32]
    if not row.product_id:
        product = await gateway('POST', 'products', {
            'name': f'Residence {row.plan_snapshot["name"]} — {app_id}',
            'price': {'price_type': 'fixed', 'amount': f'{row.plan_snapshot["price_monthly"]:.2f}', 'currency': 'USD'},
            'billing_cycle': {'interval': 'month', 'frequency': 1},
            'trial_period': {'interval': 'day', 'frequency': row.trial_days},
        }, 'trial-product-' + digest)
        row.product_id = product.get('product_id') or product.get('id')
        if not row.product_id:
            raise HTTPException(502, 'The provider did not return a recurring product.')
        await db.commit()
    from app.services.payments import payment_service
    data = await gateway('POST', 'checkout-sessions', {
        'product_cart': [{'product_id': row.product_id, 'quantity': 1}],
        'customer': {'email': user.email, 'name': user.full_name or app_id},
        'billing_currency': 'USD', 'payment_method_types': ['USD_CARD'],
        'metadata': {'estate_activation': app_id}, 'reference': 'trial-' + digest,
        **payment_service._return_urls(request),
    }, 'trial-checkout-' + digest)
    session_id = data.get('checkout_id') or data.get('id')
    url = data.get('checkout_url', '')
    if not session_id or urlparse(url).scheme != 'https' or urlparse(url).hostname not in ('checkout.bachs.io', 'sandbox-checkout.bachs.io'):
        raise HTTPException(502, 'The provider returned invalid card checkout details.')
    row.session_id, row.checkout_url = session_id, url
    await db.commit()
    return {'checkout_url': url, 'session_id': session_id}


def parse_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(UTC).replace(tzinfo=None)


async def reconcile(db, row, subscription_id):
    # Always retrieve current state, making replayed and out-of-order events harmless.
    data = await gateway('GET', 'subscriptions/' + subscription_id)
    if (data.get('product_id') or (data.get('product') or {}).get('id')) != row.product_id or (data.get('metadata') or {}).get('estate_activation') != row.app_id:
        raise HTTPException(400, 'Subscription does not match estate activation.')
    if row.subscription_id and row.subscription_id != subscription_id:
        raise HTTPException(409, 'Estate already has a different automatic subscription.')
    if data.get('currency') != 'USD' or Decimal(str(data.get('amount'))) != Decimal(str(row.plan_snapshot['price_monthly'])):
        raise HTTPException(400, 'Recurring subscription price does not match consent.')
    state = data.get('status')
    if state not in ('trialing', 'active', 'past_due', 'unpaid', 'canceled'):
        raise HTTPException(502, 'Unknown provider subscription state.')
    end = data.get('current_period_end') or data.get('trial_end')
    if not end:
        raise HTTPException(502, 'Provider subscription is missing its access period.')
    sub = await estate_subscription(db, row.app_id, lock=True)
    row.subscription_id, row.status = subscription_id, state
    row.period_end = parse_time(end)
    row.cancel_at_period_end = bool(data.get('cancel_at_period_end'))
    # Existing access checks use active plus expiry; trial status is exposed separately.
    sub.status = 'active' if state in ('trialing', 'active') else ('cancelled' if state == 'canceled' else 'expired')
    sub.expiry_date = row.period_end.date()
    for field in ('tier', 'monthly_verifications_limit', 'rfid_enabled', 'log_retention_days', 'realtime_alerts_enabled', 'price_monthly'):
        setattr(sub, field, row.plan_snapshot[field])
    await db.commit()
    await invalidate_subscription_cache(row.app_id)
    return row


async def status(db, app_id):
    sub = await estate_subscription(db, app_id)
    row = await db.get(EstateCardTrial, app_id)
    return {'activation_required': sub.status == 'pending_activation',
        'managed': bool(row and row.subscription_id and row.status != 'canceled'), 'trial_days': row.trial_days if row else settings.ESTATE_TRIAL_DAYS,
        'status': row.status if row else sub.status, 'tier': row.plan_snapshot['tier'] if row else sub.tier,
        'period_end': row.period_end if row else None,
        'cancel_at_period_end': row.cancel_at_period_end if row else False,
        'price_monthly': row.plan_snapshot['price_monthly'] if row else float(sub.price_monthly),
        'checkout_url': row.checkout_url if row and not row.subscription_id else None}


async def handle_trial_event(db, payload):
    event = payload.get('type') or payload.get('event', '')
    data = payload.get('data') or {}
    if event.startswith('customer.subscription.') or event.startswith('invoice.'):
        sid = data.get('subscription_id')
        if isinstance(sid, dict):
            sid = sid.get('id') or sid.get('subscription_id')
        if not sid:
            return None
        row = (await db.execute(select(EstateCardTrial).where(EstateCardTrial.subscription_id == sid))).scalar_one_or_none()
        if not row:
            app_id = (data.get('metadata') or {}).get('estate_activation')
            row = await db.get(EstateCardTrial, app_id) if app_id else None
        if not row:
            return None
        await reconcile(db, row, sid)
        return {'status': 'success', 'app_id': row.app_id}
    return None


async def confirm(db, app_id):
    row = await db.get(EstateCardTrial, app_id)
    if not row or not row.session_id:
        raise HTTPException(404, 'No card activation checkout found.')
    if row.subscription_id:
        await reconcile(db, row, row.subscription_id)
    else:
        checkout = await gateway('GET', 'checkout-sessions/' + row.session_id)
        customer = checkout.get('customer') or {}
        customer_id = customer.get('id') or customer.get('customer_id')
        if checkout.get('status') == 'completed' and customer_id:
            offset = 0
            while True:
                page = await gateway('GET', 'subscriptions?limit=100&offset=' + str(offset) + '&customer_id=' + quote(customer_id, safe=''))
                items = page.get('items', [])
                match = next((item for item in items if
                    (item.get('product_id') or (item.get('product') or {}).get('id')) == row.product_id and
                    (item.get('metadata') or {}).get('estate_activation') == app_id), None)
                if match:
                    await reconcile(db, row, match.get('id') or match.get('subscription_id'))
                    break
                if len(items) < 100:
                    break
                offset += 100
    return await status(db, app_id)


async def cancel(db, app_id):
    row = await db.get(EstateCardTrial, app_id)
    if not row or not row.subscription_id:
        raise HTTPException(404, 'No automatic subscription found.')
    if row.status != 'canceled' and not row.cancel_at_period_end:
        await gateway('DELETE', 'subscriptions/' + row.subscription_id,
            {'cancel_at_period_end': True, 'reason': 'Estate caretaker canceled automatic renewal'})
    await reconcile(db, row, row.subscription_id)
    return await status(db, app_id)
