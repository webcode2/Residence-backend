import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from sqlalchemy import select
from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from app.models.admin_audit import AdminAuditLog
from app.services.auth import create_saas_admin_access_token, create_access_token
from app.services.payments import add_months
from app.services.plans import quote

@pytest.fixture
async def catalogue(db_session):
    estate=Estate(id=uuid.uuid4(),app_id='PLAN-'+uuid.uuid4().hex[:10],name='Pricing estate')
    owner=User(email=uuid.uuid4().hex+'@owner.test',hashed_password='unused',roles=[UserRole.SAAS_OWNER],app_id='GLOBAL')
    caretaker=User(email=uuid.uuid4().hex+'@estate.test',hashed_password='unused',roles=[UserRole.CARETAKER],app_id=estate.app_id)
    db_session.add_all([estate,owner,caretaker]);await db_session.flush()
    db_session.add(Subscription(estate_id=estate.id,status='active',tier='standard',expiry_date=date(2030,1,31),price_monthly=30))
    await db_session.commit()
    owner_headers={'Authorization':'Bearer '+create_saas_admin_access_token({'sub':owner.email,'app_id':'GLOBAL','roles':['saas_owner']})}
    tenant_headers={'Authorization':'Bearer '+create_access_token({'sub':caretaker.email,'app_id':estate.app_id,'roles':['caretaker']}),'X-App-Id':estate.app_id}
    return estate,owner_headers,tenant_headers


def edit(plan,**values):
    return {key:values.get(key,plan[key]) for key in ('name','description','price_monthly','monthly_verifications_limit','rfid_enabled','realtime_alerts_enabled','log_retention_days','discount_6_months','discount_12_months','version')}

@pytest.mark.asyncio
async def test_owner_edits_are_persistent_validated_audited_and_not_retroactive(client,db_session,catalogue):
    estate,owner,tenant=catalogue
    plans=(await client.get('/api/v1/admin/subscriptions/plans',headers=owner)).json()
    plan=next(p for p in plans if p['tier']=='standard')
    assert (await client.post('/api/v1/subscriptions/change-tier',headers=tenant,json={'tier':'enterprise'})).status_code==403
    data=edit(plan,price_monthly=40,discount_6_months=10,discount_12_months=20)
    assert (await client.patch('/api/v1/admin/subscriptions/plans/standard',headers=tenant,json=data)).status_code in (401,403)
    for invalid in ({'discount_6_months':100},{'discount_12_months':-1},{'price_monthly':0},{'log_retention_days':0},{'discount_6_months':1.234},{'price_monthly':0.01,'discount_6_months':99.99}):
        assert (await client.patch('/api/v1/admin/subscriptions/plans/standard',headers=owner,json={**data,**invalid})).status_code==422
    saved=await client.patch('/api/v1/admin/subscriptions/plans/standard',headers=owner,json=data)
    assert saved.status_code==200,saved.text
    assert saved.json()['quotes'][1]['amount']==216
    assert saved.json()['quotes'][2]['amount']==384
    assert (await client.patch('/api/v1/admin/subscriptions/plans/standard',headers=owner,json=data)).status_code==409
    public=(await client.get('/api/v1/subscriptions/tiers')).json()
    assert next(p for p in public if p['tier']=='standard')['price_monthly']==40
    sub=(await db_session.execute(select(Subscription).where(Subscription.estate_id==estate.id))).scalar_one()
    assert sub.price_monthly==30 and sub.expiry_date==date(2030,1,31)
    audits=(await db_session.execute(select(AdminAuditLog).where(AdminAuditLog.action=='plan.update'))).scalars().all()
    assert len(audits)==1 and 'discount_12_months' in audits[0].changes

@pytest.mark.asyncio
@pytest.mark.parametrize('months,amount,expiry',[(6,216,date(2030,7,31)),(12,384,date(2031,1,31))])
async def test_discounted_checkout_snapshot_duration_and_idempotency(client,db_session,catalogue,months,amount,expiry):
    estate,owner,tenant=catalogue
    plan=next(p for p in (await client.get('/api/v1/admin/subscriptions/plans',headers=owner)).json() if p['tier']=='standard')
    saved=(await client.patch('/api/v1/admin/subscriptions/plans/standard',headers=owner,json=edit(plan,price_monthly=40,discount_6_months=10,discount_12_months=20))).json()
    response=MagicMock();response.json.return_value={'id':'cs_test_'+str(months),'checkout_url':'https://checkout.bachs.io/test','status':'open'}
    provider=AsyncMock();provider.post.return_value=response
    with patch('app.core.config.settings.BACHS_SECRET_KEY','test-key'),patch('app.services.payments.httpx.AsyncClient') as factory:
        factory.return_value.__aenter__.return_value=provider
        checkout=await client.post('/api/v1/subscriptions/checkout',headers=tenant,json={'tier':'standard','billing_months':months})
    assert checkout.status_code==200,checkout.text
    assert checkout.json()['amount']==amount and checkout.json()['billing_months']==months
    assert provider.post.call_args.kwargs['json']['pricing']['amount']==f'{amount:.2f}'
    # A price/feature change while checkout is open cannot alter that purchase.
    await client.patch('/api/v1/admin/subscriptions/plans/standard',headers=owner,json=edit(saved,price_monthly=99,rfid_enabled=False))
    from app.services.payments import payment_service
    payload={'event':'collection.succeeded','data':{'id':checkout.json()['session_id'],'pricing':{'amount':str(amount),'currency':'USD'}}}
    paid_response=MagicMock();paid_response.json.return_value={'checkout_id':checkout.json()['session_id'],'status':'completed','payment_status':'succeeded','amount':str(amount),'currency':'USD'}
    provider.get.return_value=paid_response
    with patch('app.services.payments.httpx.AsyncClient') as factory:
        factory.return_value.__aenter__.return_value=provider
        result=await payment_service.handle_webhook(db_session,payload)
    assert result['status']=='success' and result['expiry_date']==str(expiry)
    again=await payment_service.handle_webhook(db_session,payload)
    assert again['expiry_date']==str(expiry)
    confirmed=await client.get('/api/v1/subscriptions/checkout/'+checkout.json()['session_id']+'/verify',headers=tenant)
    assert confirmed.status_code==200 and confirmed.json()['subscription']['expiry_date']==str(expiry)
    assert confirmed.json()['subscription']['price_monthly']==40 and confirmed.json()['subscription']['rfid_enabled'] is True
    assert (await client.post('/api/v1/subscriptions/checkout',headers=tenant,json={'tier':'standard','billing_months':3})).status_code==422
    assert (await client.get('/api/v1/subscriptions/checkout/not-real/verify',headers=tenant)).status_code==404
    # Tampered amounts never fulfill another payment.
    payload['data']['pricing']['amount']='1.00'
    from fastapi import HTTPException
    from app.models.plan import SubscriptionCheckout
    with pytest.raises(HTTPException):payment_service._check_amount(await db_session.get(SubscriptionCheckout,checkout.json()['session_id']),payload['data'],required=True)


def test_money_rounding_and_calendar_months():
    plan={'price_monthly':19.99,'discount_6_months':12.5,'discount_12_months':20}
    assert quote(plan,6)['amount']==104.95
    assert add_months(date(2028,8,31),6)==date(2029,2,28)
    assert add_months(date(2028,2,29),12)==date(2029,2,28)

@pytest.mark.asyncio
async def test_bachs_checkout_contract_and_unpaid_completion(client,db_session,catalogue):
    estate,owner,tenant=catalogue
    from app.core.config import settings
    from app.services.payments import payment_service
    response=MagicMock();response.json.return_value={'checkout_id':'chk_actual_contract','checkout_url':'https://sandbox-checkout.bachs.io/c/test','status':'open'}
    provider=AsyncMock();provider.post.return_value=response
    with patch.object(settings,'BACHS_SECRET_KEY','sk_sandbox_test'),patch.object(settings,'BACHS_BASE_URL','https://sandbox-api.bachs.io'),patch('app.services.payments.httpx.AsyncClient') as factory:
        factory.return_value.__aenter__.return_value=provider
        created=await client.post('/api/v1/subscriptions/checkout',headers=tenant,json={'tier':'standard','success_url':'http://127.0.0.1:3001/dashboard/subscription','cancel_url':'http://127.0.0.1:3001/dashboard/subscription'})
    assert created.status_code==200,created.text
    assert created.json()['session_id']=='chk_actual_contract'
    assert created.json()['is_sandbox'] and not created.json()['returns_to_app']
    payload=provider.post.call_args.kwargs['json']
    assert 'success_url' not in payload and payload['reference']
    assert provider.post.call_args.kwargs['headers']['Idempotency-Key']==payload['reference']
    unpaid=MagicMock();unpaid.json.return_value={'checkout_id':'chk_actual_contract','status':'completed','payment_status':'failed','amount':'30.00','currency':'USD'}
    provider.get.return_value=unpaid
    with patch('app.services.payments.httpx.AsyncClient') as factory:
        factory.return_value.__aenter__.return_value=provider
        result=await payment_service.handle_webhook(db_session,{'type':'checkout.completed','data':{'checkout_id':'chk_actual_contract'}})
        assert result['status']=='pending'
        verified=await client.get('/api/v1/subscriptions/checkout/chk_actual_contract/verify',headers=tenant)
    assert verified.status_code==200 and verified.json()['is_active'] is False
    current=await client.get('/api/v1/subscriptions/current',headers=tenant)
    assert current.json()['expiry_date']=='2030-01-31'
    # The event may be in a converted currency; authenticated checkout pricing wins.
    paid=MagicMock();paid.json.return_value={'checkout_id':'chk_actual_contract','status':'completed','payment_status':'succeeded','amount':'30.00','currency':'USD'}
    provider.get.return_value=paid
    with patch('app.services.payments.httpx.AsyncClient') as factory:
        factory.return_value.__aenter__.return_value=provider
        result=await payment_service.handle_webhook(db_session,{'type':'collection.succeeded','data':{'checkout_id':'chk_actual_contract','amount':'43000.00','currency':'NGN'}})
    assert result['status']=='success' and result['expiry_date']=='2030-02-28'


def test_timestamped_webhook_signature_rejects_replay():
    import time,json,hmac,hashlib
    from app.core.config import settings
    from app.services.payments import payment_service
    body=json.dumps({'type':'collection.succeeded','data':{}}).encode()
    secret='test-whsec';timestamp=str(int(time.time()))
    signature=hmac.new(secret.encode(),timestamp.encode()+b'.'+body,hashlib.sha256).hexdigest()
    with patch.object(settings,'BACHS_WEBHOOK_SECRET',secret):
        assert payment_service.verify_webhook_signature(body,v2_header=f't={timestamp},v1={signature}')
        assert not payment_service.verify_webhook_signature(body+b' ',v2_header=f't={timestamp},v1={signature}')
        old=str(int(timestamp)-601)
        signed=hmac.new(secret.encode(),old.encode()+b'.'+body,hashlib.sha256).hexdigest()
        assert not payment_service.verify_webhook_signature(body,v2_header=f't={old},v1={signed}')
