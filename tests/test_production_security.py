import pytest
import uuid
from pydantic import ValidationError
from app.core.config import Settings
from app.main import app

def test_production_secrets_guardrail_rejects_defaults():
    # Attempting to start in production with default secrets must raise ValidationError
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            ENVIRONMENT="production",
            SECRET_KEY="supersecretkey" # default
        )
    assert "CRITICAL SECURITY ERROR" in str(exc_info.value)

def test_production_secrets_guardrail_passes_with_strong_secrets():
    # Strong secrets in production must succeed
    cfg = Settings(
        ENVIRONMENT="production",
        SECRET_KEY="a_very_strong_and_secure_jwt_secret_key_123456",
        SAAS_ADMIN_SECRET_KEY="a_very_strong_and_secure_saas_admin_jwt_secret_key_123456",
        SAAS_ADMIN_PASSWORD_PEPPER="a_very_strong_and_secure_saas_admin_pepper_secret_key_123456"
    )
    assert cfg.ENVIRONMENT == "production"

@pytest.mark.asyncio
async def test_health_check_endpoint(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "environment" in data

@pytest.mark.asyncio
async def test_cors_headers_present(client):
    # Send preflight OPTIONS request
    response = await client.options(
        "/api/v1/auth/login",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type,X-App-Id"
        }
    )
    assert response.status_code == 200
    assert "access-control-allow-origin" in response.headers

@pytest.mark.asyncio
async def test_tenant_login_rate_limiting(client, db_session):
    # Repeated failed attempts should trigger rate limit (429)
    app_id = "RP-SECURITY-TEST"
    email = "attacker@malicious.com"

    for _ in range(5):
        fail_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "wrong_password"},
            headers={"X-App-Id": app_id}
        )
        assert fail_resp.status_code == 401

    # 6th attempt must be blocked by rate limiter
    blocked_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "wrong_password"},
        headers={"X-App-Id": app_id}
    )
    assert blocked_resp.status_code == 429
    assert "Too many failed login attempts" in blocked_resp.json()["detail"]

@pytest.mark.asyncio
async def test_visitor_token_creation_rate_limiting(client, db_session):
    from unittest.mock import patch
    from app.models.estate import Estate, Subscription
    from app.models.user import User, UserRole
    from app.services.auth import create_access_token, get_password_hash
    from datetime import datetime, timedelta, UTC

    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Test Estate", is_active=True)
    db_session.add(estate)

    sub = Subscription(
        estate_id=estate_id,
        status="active",
        expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)).date(),
        tier="starter",
        price_monthly=15.00
    )
    db_session.add(sub)

    resident = User(
        email="token_spammer@estate.com",
        hashed_password=get_password_hash("pass"),
        roles=[UserRole.RESIDENT],
        app_id=app_id
    )
    db_session.add(resident)
    await db_session.commit()

    token = create_access_token(data={"sub": resident.email, "app_id": app_id, "roles": ["resident"]})
    headers = {"Authorization": f"Bearer {token}", "X-App-Id": app_id}

    # 1. Resident limit is 30/min. If check_rate_limit returns allowed=False on exceeding 30:
    with patch("app.api.v1.endpoints.tokens.check_rate_limit") as mock_rl:
        mock_rl.return_value = (False, 31, 45) # Exceeded, 45s retry
        resp = await client.post(
            "/api/v1/tokens/visitor",
            json={"visitor_name": "Flooder", "visitor_phone": "1234567890"},
            headers=headers
        )
        assert resp.status_code == 429
        assert "max 30 visitor tokens per minute for residents and landlords" in resp.json()["detail"]
        assert resp.headers["Retry-After"] == "45"

    # 2. Pure Security Guard CANNOT generate visitor tokens
    guard_user = User(
        email="guard_only@estate.com",
        hashed_password=get_password_hash("pass"),
        roles=[UserRole.SECURITY],
        app_id=app_id
    )
    db_session.add(guard_user)
    await db_session.commit()

    guard_token = create_access_token(data={"sub": guard_user.email, "app_id": app_id, "roles": ["security"]})
    guard_headers = {"Authorization": f"Bearer {guard_token}", "X-App-Id": app_id}

    guard_fail = await client.post(
        "/api/v1/tokens/visitor",
        json={"visitor_name": "Guest", "visitor_phone": "1122334455"},
        headers=guard_headers
    )
    assert guard_fail.status_code == 403
    assert "Security guards cannot generate visitor tokens unless they also have a resident role" in guard_fail.json()["detail"]

    # 3. Security Guard who is ALSO a Resident CAN generate tokens
    dual_user = User(
        email="guard_resident@estate.com",
        hashed_password=get_password_hash("pass"),
        roles=[UserRole.SECURITY, UserRole.RESIDENT],
        app_id=app_id
    )
    db_session.add(dual_user)
    await db_session.commit()

    dual_token = create_access_token(data={"sub": dual_user.email, "app_id": app_id, "roles": ["security", "resident"]})
    dual_headers = {"Authorization": f"Bearer {dual_token}", "X-App-Id": app_id}

    with patch("app.api.v1.endpoints.tokens.check_rate_limit") as mock_rl:
        mock_rl.return_value = (True, 1, 0)
        dual_resp = await client.post(
            "/api/v1/tokens/visitor",
            json={"visitor_name": "Guest", "visitor_phone": "1122334455"},
            headers=dual_headers
        )
        assert dual_resp.status_code == 200

    # 4. Caretaker gets max 40 tokens/min limit
    caretaker_user = User(
        email="admin_rate@estate.com",
        hashed_password=get_password_hash("pass"),
        roles=[UserRole.CARETAKER],
        app_id=app_id
    )
    db_session.add(caretaker_user)
    await db_session.commit()

    caretaker_token = create_access_token(data={"sub": caretaker_user.email, "app_id": app_id, "roles": ["caretaker"]})
    caretaker_headers = {"Authorization": f"Bearer {caretaker_token}", "X-App-Id": app_id}

    with patch("app.api.v1.endpoints.tokens.check_rate_limit") as mock_rl:
        mock_rl.return_value = (False, 41, 30)
        ct_resp = await client.post(
            "/api/v1/tokens/visitor",
            json={"visitor_name": "Guest", "visitor_phone": "1122334455"},
            headers=caretaker_headers
        )
        assert ct_resp.status_code == 429
        assert "max 40 visitor tokens per minute for caretakers" in ct_resp.json()["detail"]


@pytest.mark.asyncio
async def test_request_id_and_security_headers_middleware(client):
    # Test that every response carries X-Request-ID, X-Process-Time-Ms, and security headers
    custom_req_id = str(uuid.uuid4())
    resp = await client.get("/healthz", headers={"X-Request-ID": custom_req_id})
    assert resp.status_code == 200
    assert resp.headers["x-request-id"] == custom_req_id
    assert "x-process-time-ms" in resp.headers
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"

@pytest.mark.asyncio
async def test_standardized_error_response_with_request_id(client):
    # 404 Not Found returns JSON with detail, status_code, and request_id
    resp = await client.get("/api/v1/non_existent_route")
    assert resp.status_code == 404
    body = resp.json()
    assert "detail" in body
    assert body["status_code"] == 404
    assert "request_id" in body

@pytest.mark.asyncio
async def test_validation_error_handler_with_request_id(client):
    # Invalid JSON payload returns 422 with structured detail and request_id
    resp = await client.post("/api/v1/auth/login", json={"invalid": "payload"})
    assert resp.status_code == 422
    body = resp.json()
    assert "detail" in body
    assert body["status_code"] == 422
    assert "request_id" in body


