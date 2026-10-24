"""Repeatable, non-destructive demo seed. Passwords are generated and stored locally."""
import argparse
import asyncio
import json
import os
import secrets
import uuid
from datetime import timedelta
from pathlib import Path
from sqlalchemy import select
from app.core.database import SessionLocal, engine
from app.models.base import utc_now
from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from app.models.token import VisitorToken
from app.models.chat import GateSite, ChatThread, ChatMessage
from app.models.access_log import AccessLog
from app.models.admin_audit import AdminAuditLog  # registers audit table for init_db
from app.core.tiers import get_tier_config
from app.services.auth import get_password_hash, get_saas_admin_password_hash, verify_password

SEED_NAMESPACE = uuid.UUID('72d27f91-8c8b-4b4a-9db9-d51888d48a12')
ESTATES = [
    ('GREENVIEW', 'Greenview Demo Estate', 'standard', 'active', 120, True),
    ('RIVERSIDE', 'Riverside Demo Estate', 'premium', 'active', 10, True),
    ('HILLCREST', 'Hillcrest Demo Estate', 'starter', 'expired', -7, True),
    ('CEDAR', 'Cedar Demo Estate', 'enterprise', 'cancelled', 45, False),
]


def seed_id(key: str) -> uuid.UUID:
    return uuid.uuid5(SEED_NAMESPACE, key)


def write_credentials(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(data, handle, indent=2)
            handle.write('\n')
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


async def seed_data(credentials_file: str = '.seed-credentials.json'):
    path = Path(credentials_file).resolve()
    credentials = json.loads(path.read_text()) if path.exists() else {'owner_url': '/owner/login', 'caretaker_url': '/login', 'accounts': {}}
    accounts = credentials.setdefault('accounts', {})
    created = {'estates': 0, 'subscriptions': 0, 'users': 0, 'gates': 0, 'threads': 0, 'messages': 0, 'access_logs': 0, 'visitor_passes': 0}
    now = utc_now()
    async with SessionLocal() as db:
        async def account(email, name, roles, app_id, **details):
            user = await db.scalar(select(User).where(User.email == email))
            owner = UserRole.SAAS_OWNER in roles
            if user:
                if user.app_id != app_id or set(user.roles) != set(roles):
                    raise ValueError(f'Seed account conflicts with an existing account: {email}')
                # Repair only the known legacy seed owner hash; never reset a real password.
                if owner and verify_password('superadmin123', user.hashed_password):
                    password = os.environ.get('SEED_OWNER_PASSWORD') or secrets.token_urlsafe(24)
                    if len(password) < 12:
                        raise ValueError('SEED_OWNER_PASSWORD must contain at least 12 characters')
                    user.hashed_password = get_saas_admin_password_hash(password)
                    accounts[email] = {'password': password, 'roles': [r.value for r in roles], 'app_id': app_id, 'note': 'Legacy seed owner hash repaired'}
                elif email not in accounts:
                    accounts[email] = {'password': None, 'roles': [r.value for r in roles], 'app_id': app_id, 'note': 'Existing account preserved; password unchanged'}
                return user
            password = accounts.get(email, {}).get('password') or (os.environ.get('SEED_OWNER_PASSWORD') if owner else None) or secrets.token_urlsafe(24)
            if len(password) < 12:
                raise ValueError('Seed passwords must contain at least 12 characters')
            user = User(id=seed_id(email), email=email, full_name=name, roles=roles, app_id=app_id,
                        hashed_password=(get_saas_admin_password_hash if owner else get_password_hash)(password), **details)
            db.add(user)
            await db.flush()
            accounts[email] = {'password': password, 'roles': [r.value for r in roles], 'app_id': app_id}
            created['users'] += 1
            return user

        await account(os.environ.get('SEED_OWNER_EMAIL', 'owner@residencesaas.com'), 'Residence Platform Owner', [UserRole.SAAS_OWNER], 'GLOBAL')
        for slug, name, tier, status, expiry, active in ESTATES:
            app_id = f'RP-DEMO-{slug}'
            estate = await db.scalar(select(Estate).where(Estate.app_id == app_id))
            if not estate:
                estate = Estate(id=seed_id(app_id), app_id=app_id, name=name, is_active=active)
                db.add(estate); await db.flush(); created['estates'] += 1
            sub = await db.scalar(select(Subscription).where(Subscription.estate_id == estate.id))
            if not sub:
                cfg = get_tier_config(tier)
                sub = Subscription(id=seed_id(f'{app_id}/subscription'), estate_id=estate.id,
                    tier=cfg['tier'], status=status, expiry_date=(now + timedelta(days=expiry)).date(),
                    monthly_verifications_limit=cfg['monthly_verifications_limit'], current_month_verifications=1200,
                    billing_cycle_start=now.date().replace(day=1), rfid_enabled=cfg['rfid_enabled'],
                    realtime_alerts_enabled=cfg['realtime_alerts_enabled'], log_retention_days=cfg['log_retention_days'], price_monthly=cfg['price_monthly'])
                db.add(sub); created['subscriptions'] += 1
            prefix = slug.lower()
            await account(f'caretaker.{prefix}@example.com', f'{slug.title()} Caretaker', [UserRole.CARETAKER], app_id)
            landlord = await account(f'landlord.{prefix}@example.com', 'John Ade', [UserRole.LANDLORD], app_id)
            resident = await account(f'resident.{prefix}@example.com', 'Ada Obi', [UserRole.RESIDENT], app_id,
                                     landlord_id=landlord.id, house_number='12', street_name='Palm Avenue', rfid_tag=f'DEMO-{slug}-001')
            second = await account(f'resident2.{prefix}@example.com', 'Bola Ade', [UserRole.RESIDENT], app_id,
                                   landlord_id=landlord.id, house_number='3', street_name='Oak Close')
            guard = await account(f'guard.{prefix}@example.com', 'Officer James', [UserRole.SECURITY], app_id)
            await account(f'guard2.{prefix}@example.com', 'Officer Grace', [UserRole.SECURITY], app_id)
            gates = []
            for gate_name in ('Main gate', 'Service gate'):
                gate = await db.get(GateSite, seed_id(f'{app_id}/{gate_name}'))
                if not gate:
                    gate = GateSite(id=seed_id(f'{app_id}/{gate_name}'), app_id=app_id, estate_id=estate.id,
                                    name=gate_name, code='MAIN' if gate_name == 'Main gate' else 'SERVICE', is_active=True)
                    db.add(gate); created['gates'] += 1
                gates.append(gate)
            await db.flush()
            for index, (person, subject, emergency, thread_status) in enumerate([
                (resident, 'Visitor arrival', False, 'open'),
                (second, 'Package at the desk', False, 'resolved'),
                (resident, 'Urgent assistance requested', True, 'open'),
            ]):
                tid = seed_id(f'{app_id}/thread/{index}')
                if await db.get(ChatThread, tid):
                    continue
                start = now - timedelta(hours=3-index)
                first = 'My visitor is arriving shortly.' if index == 0 else 'Has my package arrived?' if index == 1 else 'Please send an officer to Palm Avenue.'
                reply = 'We will verify their code at the main gate.' if index == 0 else 'Your package is at the security desk.' if index == 1 else 'An officer has been dispatched to your unit.'
                thread = ChatThread(id=tid, app_id=app_id, resident_id=person.id, gate_site_id=gates[0].id,
                    resident_name=person.full_name, unit_address=f'{person.house_number}, {person.street_name}',
                    subject=subject, status=thread_status, has_active_emergency=emergency,
                    last_message_at=start+timedelta(minutes=2), last_message_text=reply, last_sender_role='security',
                    resident_unread_count=1, guard_unread_count=0, created_at=start)
                db.add(thread); await db.flush(); created['threads'] += 1
                for number, sender, text, is_guard in [(0, person, first, False), (1, guard, reply, True)]:
                    db.add(ChatMessage(id=seed_id(f'{tid}/{number}'), thread_id=tid, sender_id=sender.id,
                        sender_name=sender.full_name, sender_role='security' if is_guard else 'resident',
                        officer_badge='SEC-012' if is_guard else None, officer_post='Main gate' if is_guard else None,
                        is_from_guard=is_guard, message=text, status_tag='DISPATCHED' if emergency and is_guard else 'EMERGENCY' if emergency else None,
                        is_read=not is_guard, read_at=start+timedelta(minutes=1) if not is_guard else None,
                        created_at=start+timedelta(minutes=number*2)))
                    created['messages'] += 1
            for day in range(7):
                for number in range(3):
                    lid = seed_id(f'{app_id}/log/{day}/{number}')
                    if await db.get(AccessLog, lid): continue
                    db.add(AccessLog(id=lid, app_id=app_id, event_type='visitor_token_verification',
                        identifier=f'DEMO-{slug}-{day}-{number}', status='denied' if number == 2 and day % 2 else 'authorized',
                        resident_id=resident.id, visitor_name=['Tunde Cole','Mary Bello','James Ali'][number],
                        details='Seeded visitor verification', created_at=now-timedelta(days=day, minutes=number*15)))
                    created['access_logs'] += 1
            pid = seed_id(f'{app_id}/visitor')
            if not await db.get(VisitorToken, pid):
                db.add(VisitorToken(id=pid, app_id=app_id, code=f'DEMO{slug[:2]}', resident_id=resident.id,
                    visitor_name='Tunde Cole', expires_at=now+timedelta(hours=8), status='checked_in', is_used=True, checked_in_at=now-timedelta(minutes=30)))
                created['visitor_passes'] += 1
        # Store credentials before commit so a filesystem failure cannot orphan new account passwords.
        write_credentials(path, credentials)
        await db.commit()
    await engine.dispose()
    print(json.dumps({'seed_created': created, 'credentials_file': str(path), 'passwords_printed': False}, indent=2))
    return created


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials-file', default='.seed-credentials.json')
    args = parser.parse_args()
    asyncio.run(seed_data(args.credentials_file))
