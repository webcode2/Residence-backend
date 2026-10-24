from datetime import datetime, timedelta, UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from app.models.estate import Estate, Subscription
from app.models.plan import EstateCardTrial
from app.services import card_trials
from app.services.payments import payment_service
from app.schemas.subscription import CheckoutSessionCreateSchema
from app.api.v1.endpoints.subscriptions import CardActivationRequest
from app.services.estates import register_new_estate
from app.schemas.estate import EstateRegistrationRequestSchema

@pytest.fixture
async def pending(db_session, monkeypatch):
    monkeypatch.setattr(card_trials,'invalidate_subscription_cache',AsyncMock())
    estate=Estate(name='Trial estate',app_id='TRIAL-TEST',is_active=True)
    db_session.add(estate)
    await db_session.flush()
    sub=Subscription(estate_id=estate.id,status='pending_activation',expiry_date=datetime.now(UTC).date(),tier='starter')
    db_session.add(sub)
    await db_session.commit()
    return sub,SimpleNamespace(id=uuid.uuid4(),email='trial@example.com',full_name='Trial caretaker')

async def setup_card(db_session,pending,monkeypatch):
    mock=AsyncMock(side_effect=[{'id':'prod_trial'},{'id':'chk_trial','checkout_url':'https://checkout.bachs.io/session/chk_trial'}])
    monkeypatch.setattr(card_trials,'gateway',mock)
    request=CardActivationRequest(consent=True,expected_price_monthly=15)
    result=await card_trials.start(db_session,'TRIAL-TEST',pending[1],request)
    return mock,result,request

@pytest.mark.asyncio
async def test_registration_requires_activation(db_session):
    result=await register_new_estate(db_session,EstateRegistrationRequestSchema(estateName='New estate',adminName='Caretaker',email='newtrial@example.com',password='StrongTestPassword123!',terms=True))
    sub=await card_trials.estate_subscription(db_session,result['app_id'])
    assert sub.status=='pending_activation'
    assert sub.expiry_date==datetime.now(UTC).date()

@pytest.mark.asyncio
async def test_consent_and_price(db_session,pending):
    with pytest.raises(ValidationError): CardActivationRequest(consent=False,expected_price_monthly=15)
    with pytest.raises(HTTPException) as exc:
        await card_trials.start(db_session,'TRIAL-TEST',pending[1],CardActivationRequest(consent=True,expected_price_monthly=1))
    assert exc.value.status_code==409
    assert await db_session.get(EstateCardTrial,'TRIAL-TEST') is None

@pytest.mark.asyncio
async def test_checkout_no_access_and_idempotent(db_session,pending,monkeypatch):
    mock,result,request=await setup_card(db_session,pending,monkeypatch)
    assert pending[0].status=='pending_activation'
    assert mock.call_args_list[0].args[2]['trial_period']=={'interval':'day','frequency':14}
    assert mock.call_args_list[1].args[2]['payment_method_types']==['USD_CARD']
    monkeypatch.setattr(card_trials,'gateway',AsyncMock(return_value={'status':'open'}))
    assert await card_trials.start(db_session,'TRIAL-TEST',pending[1],request)==result
    assert mock.await_count==2
    monkeypatch.setattr(card_trials,'gateway',AsyncMock(return_value={'status':'open'}))
    assert (await card_trials.confirm(db_session,'TRIAL-TEST'))['activation_required']

@pytest.mark.asyncio
async def test_webhook_trial_renew_failure_cancel(db_session,pending,monkeypatch):
    await setup_card(db_session,pending,monkeypatch)
    provider={'subscription_id':'sub_trial','product':{'id':'prod_trial'},'metadata':{'estate_activation':'TRIAL-TEST'},'currency':'USD','amount':'15.00','status':'trialing','current_period_end':(datetime.now(UTC)+timedelta(days=14)).isoformat()}
    mock=AsyncMock(return_value=provider)
    monkeypatch.setattr(card_trials,'gateway',mock)
    event={'type':'customer.subscription.created','data':provider}
    await payment_service.handle_webhook(db_session,event)
    assert pending[0].status=='active'
    assert (await card_trials.status(db_session,'TRIAL-TEST'))['status']=='trialing'
    provider['status']='active';provider['current_period_end']=(datetime.now(UTC)+timedelta(days=44)).isoformat()
    await payment_service.handle_webhook(db_session,{'type':'invoice.paid','data':{'subscription_id':'sub_trial'}})
    await payment_service.handle_webhook(db_session,event)
    assert pending[0].expiry_date==(datetime.now(UTC)+timedelta(days=44)).date()
    provider['status']='past_due'
    await payment_service.handle_webhook(db_session,{'type':'invoice.payment_failed','data':{'subscription_id':'sub_trial'}})
    assert pending[0].status=='expired'
    provider['status']='active';provider['cancel_at_period_end']=True
    assert (await card_trials.cancel(db_session,'TRIAL-TEST'))['cancel_at_period_end']
    assert pending[0].status=='active'
    assert any(c.args[0]=='DELETE' for c in mock.call_args_list)

@pytest.mark.asyncio
async def test_wrong_subscription_and_bypass(db_session,pending,monkeypatch):
    await setup_card(db_session,pending,monkeypatch)
    row=await db_session.get(EstateCardTrial,'TRIAL-TEST')
    monkeypatch.setattr(card_trials,'gateway',AsyncMock(return_value={'product_id':'prod_wrong','metadata':{'estate_activation':'TRIAL-TEST'}}))
    with pytest.raises(HTTPException): await card_trials.reconcile(db_session,row,'sub_foreign')
    with pytest.raises(HTTPException) as exc:
        await payment_service.create_checkout_session(db_session,'TRIAL-TEST',pending[1],CheckoutSessionCreateSchema(tier='starter'))
    assert exc.value.status_code==409

@pytest.mark.asyncio
async def test_confirm_can_recover_delayed_webhook(db_session,pending,monkeypatch):
    await setup_card(db_session,pending,monkeypatch)
    provider={'id':'sub_delayed','product':{'id':'prod_trial'},'metadata':{'estate_activation':'TRIAL-TEST'},'currency':'USD','amount':'15.00','status':'trialing','current_period_end':(datetime.now(UTC)+timedelta(days=14)).isoformat()}
    monkeypatch.setattr(card_trials,'gateway',AsyncMock(side_effect=[{'status':'completed','customer':{'id':'cust_trial'}},{'items':[provider]},provider]))
    result=await card_trials.confirm(db_session,'TRIAL-TEST')
    assert result['managed'] and not result['activation_required']
    assert result['status']=='trialing'

@pytest.mark.asyncio
async def test_expired_checkout_can_be_replaced(db_session,pending,monkeypatch):
    _,_,request=await setup_card(db_session,pending,monkeypatch)
    mock=AsyncMock(side_effect=[{'status':'expired'},{'id':'chk_replacement','checkout_url':'https://checkout.bachs.io/session/replacement'}])
    monkeypatch.setattr(card_trials,'gateway',mock)
    result=await card_trials.start(db_session,'TRIAL-TEST',pending[1],request)
    assert result['session_id']=='chk_replacement'
    assert mock.call_args_list[1].args[2]['product_cart'][0]['product_id']=='prod_trial'
