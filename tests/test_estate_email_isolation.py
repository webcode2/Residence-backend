import uuid
from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, patch
import pytest
from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from app.services.auth import create_access_token

LANDLORD = {'email': 'shared.landlord@example.com', 'name': 'Shared Landlord', 'phone': '+2348012345678',
            'house_number': '9', 'street_name': 'Salvation Street'}


async def estate_with_caretaker(db_session):
    estate_id = uuid.uuid4()
    app_id = f'RP-{estate_id.hex[:6].upper()}'
    db_session.add(Estate(id=estate_id, app_id=app_id, name=f'Estate {app_id}'))
    db_session.add(Subscription(estate_id=estate_id, status='active',
                                expiry_date=(datetime.now(UTC) + timedelta(days=30)).date()))
    caretaker = User(email=f'caretaker-{app_id.lower()}@example.com', full_name='Caretaker', hashed_password='x',
                     roles=[UserRole.CARETAKER], app_id=app_id)
    db_session.add(caretaker)
    await db_session.commit()
    token = create_access_token({'sub': caretaker.email, 'roles': ['caretaker'], 'app_id': app_id})
    return app_id, {'X-App-Id': app_id, 'Authorization': f'Bearer {token}'}


@pytest.mark.asyncio
async def test_same_email_can_join_different_estates_but_not_twice_in_one(client, db_session):
    first, first_headers = await estate_with_caretaker(db_session)
    second, second_headers = await estate_with_caretaker(db_session)
    with patch('app.services.notifications.notification_service.send_registration_email', new_callable=AsyncMock, return_value=True):
        assert (await client.post('/api/v1/landlords/', json=LANDLORD, headers=first_headers)).status_code == 201
        other_estate = await client.post('/api/v1/landlords/', json=LANDLORD, headers=second_headers)
        assert other_estate.status_code == 201, other_estate.text
        same_estate = await client.post('/api/v1/landlords/', json={**LANDLORD, 'email': 'Shared.Landlord@example.com'}, headers=first_headers)
        assert same_estate.status_code == 409
