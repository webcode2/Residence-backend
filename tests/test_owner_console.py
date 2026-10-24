import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from app.core.config import settings
from app.models.base import utc_now
from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from app.models.admin_audit import AdminAuditLog
from app.schemas.admin import AdminSubscriptionOverrideSchema
from app.services import admin as service
from app.services.auth import get_saas_admin_password_hash, create_saas_admin_access_token, create_access_token
from app.dependencies import ValidateTenant
from sqlalchemy import select


def owner():
    return User(id=uuid.uuid4(),email='owner-unit@example.com',roles=[UserRole.SAAS_OWNER],app_id='GLOBAL',hashed_password='unused')


def test_subscription_overrides_validate_limits_and_status():
    for value in ({'status':'invented'},{'monthly_verifications_limit':-1},{'log_retention_days':0}):
        with pytest.raises(ValidationError): AdminSubscriptionOverrideSchema(**value)
    assert AdminSubscriptionOverrideSchema(monthly_verifications_limit=None).model_fields_set == {'monthly_verifications_limit'}


@pytest.mark.asyncio
async def test_suspended_estate_stays_blocked_on_cache_miss():
    db=AsyncMock();db.execute.return_value.scalar_one_or_none=MagicMock(return_value=Estate(app_id='SUSPENDED',name='Suspended',is_active=False))
    with patch('app.dependencies.get_cached_estate_validity',AsyncMock(return_value=None)),patch('app.dependencies.set_cached_estate_validity',AsyncMock()):
        with pytest.raises(HTTPException) as exc: await ValidateTenant()(x_app_id='SUSPENDED',db=db)
    assert exc.value.status_code==401


@pytest.mark.asyncio
async def test_owner_accounts_cannot_be_revoked():
    db=AsyncMock();db.execute.return_value.scalar_one_or_none=MagicMock(return_value=owner())
    with pytest.raises(HTTPException) as exc: await service.set_user_revocation_admin(db,uuid.uuid4(),True,actor=owner())
    assert exc.value.status_code==403
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_api_boundaries_and_subscription_audit(client,db_session):
    actor=owner();actor.hashed_password=get_saas_admin_password_hash('Owner-Only-Password-123')
    estate=Estate(id=uuid.uuid4(),app_id='OWNER-TEST',name='Owner Test Estate',is_active=True)
    db_session.add_all([actor,estate]);await db_session.flush()
    sub=Subscription(estate_id=estate.id,status='active',expiry_date=(utc_now()+timedelta(days=30)).date(),tier='standard',monthly_verifications_limit=50000)
    db_session.add(sub);await db_session.commit()
    owner_token=create_saas_admin_access_token({'sub':actor.email,'app_id':'GLOBAL','roles':['saas_owner']})
    tenant_token=create_access_token({'sub':actor.email,'app_id':'GLOBAL','roles':['saas_owner']})
    denied=await client.get('/api/v1/admin/overview',headers={'Authorization':f'Bearer {tenant_token}'})
    assert denied.status_code==401
    headers={'Authorization':f'Bearer {owner_token}'}
    plans=await client.get('/api/v1/admin/subscriptions/plans',headers=headers)
    assert plans.status_code==200
    assert {p['tier'] for p in plans.json()}=={'starter','standard','premium','enterprise'}
    update=await client.patch(f'/api/v1/admin/subscriptions/{estate.id}',json={'tier':'enterprise','monthly_verifications_limit':None},headers=headers)
    assert update.status_code==200,update.text
    assert update.json()['monthly_verifications_limit'] is None
    assert update.json()['price_monthly']==100
    events=await client.get('/api/v1/admin/audit/',headers=headers)
    assert events.status_code==200
    assert events.json()[0]['action']=='subscription.update'
    assert events.json()[0]['actor_email']==actor.email
    assert events.json()[0]['target_id']==str(estate.id)


@pytest.mark.asyncio
async def test_unlimited_subscription_stays_unlimited_on_insert(db_session):
    estate = Estate(id=uuid.uuid4(), app_id='UNLIMITED-TEST', name='Unlimited estate', is_active=True)
    db_session.add(estate)
    await db_session.flush()
    subscription = Subscription(estate_id=estate.id, tier='enterprise', status='active',
        expiry_date=(utc_now()+timedelta(days=30)).date(), monthly_verifications_limit=None)
    db_session.add(subscription)
    await db_session.commit()
    await db_session.refresh(subscription)
    assert subscription.monthly_verifications_limit is None


@pytest.mark.asyncio
async def test_global_estate_endpoints_require_owner(client):
    assert (await client.get('/api/v1/estates/')).status_code == 401
    assert (await client.post('/api/v1/estates/', json={'name': 'Forbidden estate'})).status_code == 401
