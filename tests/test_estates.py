import pytest
import uuid
from app.models.user import User, UserRole

@pytest.mark.asyncio
async def test_register_estate_success(client, db_session):
    reg_data = {
        "estateName": f"Estate-{uuid.uuid4().hex[:4].upper()}",
        "adminName": "Peterson",
        "email": f"peterson-{uuid.uuid4().hex[:4]}@example.com",
        "password": "password123",
        "terms": True
    }
    response = await client.post("/api/v1/estates/register", json=reg_data)
    assert response.status_code == 200
    assert response.json()["estate_name"] == reg_data["estateName"]
    assert response.json()["app_id"].startswith("RP-")
    
    # Verify Admin User created
    from sqlalchemy import select
    from app.models.user import User
    result = await db_session.execute(select(User).where(User.email == reg_data["email"]))
    user = result.scalar_one_or_none()
    assert user is not None
    assert user.full_name == "Peterson"
    assert UserRole.CARETAKER in user.roles

@pytest.mark.asyncio
async def test_list_estates(client, db_session):
    response = await client.get("/api/v1/estates/")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
