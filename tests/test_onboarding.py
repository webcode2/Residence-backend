import io
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch
import pytest
from sqlalchemy import select
from app.models.base import utc_now
from app.models.user import User, UserRole
from app.models.estate import Estate, Subscription
from app.models.onboarding import LandlordOnboardingJob as Job
from app.models.token import RegistrationToken
from app.services.auth import get_password_hash, create_access_token, verify_password
from app.services.landlords import process_one
from app.services.landlord_files import parse_file

@pytest.fixture
async def onboarding_estate(db_session):
    app_id='ONBOARD-'+uuid.uuid4().hex[:8]
    estate=Estate(name='Onboarding test',app_id=app_id,is_active=True)
    db_session.add(estate);await db_session.flush()
    db_session.add(Subscription(estate_id=estate.id,status='active',expiry_date=(utc_now()+timedelta(days=30)).date()))
    admin=User(email=uuid.uuid4().hex+'@example.com',hashed_password=get_password_hash('admin-test-password'),app_id=app_id,roles=[UserRole.CARETAKER])
    db_session.add(admin);await db_session.commit()
    headers={'X-App-Id':app_id,'Authorization':'Bearer '+create_access_token({'sub':admin.email,'app_id':app_id})}
    return app_id,headers

def row():
    return {'name':'A Landlord','email':uuid.uuid4().hex+'@example.com','phone':'+2348012345678','house_number':'12A','street_name':'Palm Ave'}

@pytest.mark.asyncio
async def test_queue_password_gate_and_host_trace(client,db_session,onboarding_estate):
    app_id,headers=onboarding_estate;entry=row()
    queued=await client.post('/api/v1/landlords/import',headers=headers,json=[entry])
    assert queued.status_code==200,queued.text
    assert queued.json()[0]['status']=='queued'
    assert not (await db_session.execute(select(User).where(User.email==entry['email']))).scalar_one_or_none()
    with patch('app.services.landlords.notification_service.send_registration_email',new_callable=AsyncMock,return_value=True) as send:
        assert await process_one(db_session)
        temporary=send.call_args.kwargs['password']
        assert send.call_args.kwargs['app_id']==app_id
    job=(await db_session.execute(select(Job).where(Job.email==entry['email']))).scalar_one()
    assert job.status=='sent' and job.password_ciphertext is None
    landlord=await db_session.get(User,job.user_id)
    assert landlord.phone==entry['phone'] and landlord.must_change_password
    landlord_id=landlord.id
    login=await client.post('/api/v1/auth/login',headers={'X-App-Id':app_id},json={'email':entry['email'],'password':temporary})
    assert login.status_code==200,login.text
    restricted={'X-App-Id':app_id,'Authorization':'Bearer '+login.json()['token']['access_token']}
    assert login.json()['user']['must_change_password']
    assert (await client.post('/api/v1/tokens/registration',headers=restricted,json={})).status_code==403
    assert (await client.get('/api/v1/chat/gates',headers=restricted)).status_code==403
    assert (await client.post('/api/v1/auth/change-password',headers=restricted,json={'current_password':temporary,'new_password':temporary})).status_code==422
    changed=await client.post('/api/v1/auth/change-password',headers=restricted,json={'current_password':temporary,'new_password':'my-new-private-password'})
    assert changed.status_code==200,changed.text
    active={'X-App-Id':app_id,'Authorization':'Bearer '+changed.json()['token']['access_token']}
    assert (await client.get('/api/v1/tokens/registration',headers=restricted)).status_code in (401,403)
    assert (await client.post('/api/v1/tokens/registration',headers=active,json={'landlord_id':str(uuid.uuid4())})).status_code==403
    invite=await client.post('/api/v1/tokens/registration',headers=active,json={'relationship_type':'tenant','house_number':'FORGED'})
    assert invite.status_code==200,invite.text
    assert invite.json()['house_number']=='12A'
    code=invite.json()['code']
    body={'email':uuid.uuid4().hex+'@example.com','password':'new-resident-password','full_name':'Tenant Person','roles':['security'],'app_id':app_id,'house_number':'FORGED'}
    registered=await client.post('/api/v1/users/register',params={'registration_code':code},headers={'X-App-Id':app_id},json=body)
    assert registered.status_code==200,registered.text
    user=registered.json()
    assert user['landlord_id']==str(landlord_id) and user['house_number']=='12A'
    assert user['relationship_type']=='tenant' and user['invitation_id']==invite.json()['id'] and user['roles']==['resident']
    assert (await client.post('/api/v1/users/register',params={'registration_code':code},headers={'X-App-Id':app_id},json={**body,'email':uuid.uuid4().hex+'@example.com'})).status_code==400
    tracker=await client.get('/api/v1/landlords/onboarding-jobs',headers=headers)
    assert 'password' not in tracker.text and tracker.json()[0]['status']=='sent'

@pytest.mark.asyncio
async def test_failed_job_retries_same_password_and_recovers(client,db_session,onboarding_estate):
    app_id,headers=onboarding_estate;entry=row()
    queued=await client.post('/api/v1/landlords/import',headers=headers,json=[entry]);job_id=queued.json()[0]['job_id']
    with patch('app.services.landlords.notification_service.send_registration_email',new_callable=AsyncMock,return_value=False) as send:
        await process_one(db_session);password=send.call_args.kwargs['password'];key=send.call_args.kwargs['idempotency_key']
    tracker=(await client.get('/api/v1/landlords/onboarding-jobs',headers=headers)).json()[0]
    assert tracker['status']=='retrying' and tracker['attempts']==1 and tracker['last_error']
    assert not await process_one(db_session) # backoff honored
    assert (await client.post(f'/api/v1/landlords/onboarding-jobs/{job_id}/retry',headers=headers)).status_code==200
    with patch('app.services.landlords.notification_service.send_registration_email',new_callable=AsyncMock,return_value=True) as send:
        await process_one(db_session)
        assert send.call_args.kwargs['password']==password and send.call_args.kwargs['idempotency_key']==key
    account=(await db_session.execute(select(User).where(User.email==entry['email']))).scalars().all()
    assert len(account)==1 and verify_password(password,account[0].hashed_password)
    assert (await client.post(f'/api/v1/landlords/onboarding-jobs/{job_id}/retry',headers=headers)).status_code==409

@pytest.mark.asyncio
async def test_queue_is_estate_scoped_and_duplicate_safe(client,db_session,onboarding_estate):
    app_id,headers=onboarding_estate;entry=row()
    response=await client.post('/api/v1/landlords/import',headers=headers,json=[entry,entry])
    assert [r['status'] for r in response.json()]==['queued','skipped']
    db_session.add(Estate(name='Other estate',app_id='OTHER-'+app_id,is_active=True))
    other=User(email=uuid.uuid4().hex+'@example.com',hashed_password='unused',app_id='OTHER-'+app_id,roles=[UserRole.CARETAKER]);db_session.add(other);await db_session.commit()
    foreign={'X-App-Id':other.app_id,'Authorization':'Bearer '+create_access_token({'sub':other.email,'app_id':other.app_id})}
    assert (await client.get('/api/v1/landlords/onboarding-jobs',headers=foreign)).json()==[]
    assert (await client.post('/api/v1/landlords/onboarding-jobs/'+response.json()[0]['job_id']+'/retry',headers=foreign)).status_code==404

@pytest.mark.asyncio
async def test_worker_reclaims_expired_lease_and_caps_failures(client,db_session,onboarding_estate):
    _,headers=onboarding_estate;entry=row()
    await client.post('/api/v1/landlords/import',headers=headers,json=[entry])
    job=(await db_session.execute(select(Job).where(Job.email==entry['email']))).scalar_one()
    job.status='processing';job.lease_until=utc_now()-timedelta(seconds=1);job.attempts=5;await db_session.commit()
    assert await process_one(db_session)
    await db_session.refresh(job)
    assert job.status=='failed'

@pytest.mark.asyncio
async def test_import_file_validates_before_queueing(client,db_session,onboarding_estate):
    _,headers=onboarding_estate
    bad=b'Name,Email,Phone,House Number\nBad,not-an-email,+2348012345678,12\n'
    response=await client.post('/api/v1/landlords/import-file',headers=headers,files={'file':('bad.csv',bad,'text/csv')})
    assert response.status_code==422 and 'Row 2' in response.text
    assert (await client.get('/api/v1/landlords/onboarding-jobs',headers=headers)).json()==[]
    valid=f"Name,Email,Phone,House Number\nTest,{uuid.uuid4().hex}@example.com,+2348012345678,012\n".encode()
    response=await client.post('/api/v1/landlords/import-file',headers=headers,files={'file':('good.csv',valid,'text/csv')})
    assert response.status_code==200 and response.json()[0]['status']=='queued'

def test_excel_parser_preserves_text_identifiers():
    from openpyxl import Workbook
    workbook=Workbook();sheet=workbook.active
    sheet.append(['Name','Email','Phone','House Number']);sheet.append(['Landlord','person@example.com','08012345678','001A'])
    stream=io.BytesIO();workbook.save(stream)
    data=parse_file('landlords.xlsx',stream.getvalue())
    assert data[0].phone=='08012345678' and data[0].house_number=='001A'

@pytest.mark.asyncio
async def test_expired_invite_cannot_register_or_relink_existing_account(client,db_session,onboarding_estate):
    app_id,_=onboarding_estate
    host=User(email=uuid.uuid4().hex+'@example.com',app_id=app_id,roles=[UserRole.LANDLORD],hashed_password='unused')
    db_session.add(host);await db_session.flush()
    token=RegistrationToken(code='REG'+uuid.uuid4().hex[:6].upper(),landlord_id=host.id,app_id=app_id,expires_at=utc_now()-timedelta(seconds=1))
    db_session.add(token);await db_session.commit()
    body={'email':uuid.uuid4().hex+'@example.com','password':'a-new-private-password','roles':['resident'],'app_id':app_id}
    url='/api/v1/users/register?registration_code='+token.code
    assert (await client.post(url,headers={'X-App-Id':app_id},json=body)).status_code==400
    token.expires_at=utc_now()+timedelta(days=1);await db_session.commit()
    assert (await client.post(url,headers={'X-App-Id':app_id},json={**body,'email':host.email})).status_code==400
    await db_session.refresh(token)
    assert not token.is_used

@pytest.mark.asyncio
async def test_invitation_is_consumed_atomically(engine,db_session,onboarding_estate):
    import asyncio
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.schemas.user import UserCreateSchema
    from app.services.users import register_resident
    app_id,_=onboarding_estate
    host=User(email=uuid.uuid4().hex+'@example.com',app_id=app_id,roles=[UserRole.LANDLORD],hashed_password='unused')
    db_session.add(host);await db_session.flush()
    code='REG'+uuid.uuid4().hex[:6].upper()
    db_session.add(RegistrationToken(code=code,landlord_id=host.id,app_id=app_id,expires_at=utc_now()+timedelta(days=7)))
    await db_session.commit()
    factory=async_sessionmaker(engine,expire_on_commit=False)
    async def register():
        async with factory() as session:
            return await register_resident(session,UserCreateSchema(email=uuid.uuid4().hex+'@example.com',password='safe-resident-password',roles=[UserRole.RESIDENT],app_id=app_id),code,app_id)
    results=await asyncio.gather(register(),register())
    assert sum(result is not None for result in results)==1

@pytest.mark.asyncio
async def test_landlords_cannot_be_created_outside_queue(client,onboarding_estate):
    app_id,headers=onboarding_estate
    response=await client.post('/api/v1/users/',headers=headers,json={'email':uuid.uuid4().hex+'@example.com','password':'a-temporary-password','roles':['landlord'],'app_id':app_id})
    assert response.status_code==409

@pytest.mark.asyncio
async def test_original_file_retained_downloadable_and_deletable_after_processing(client,db_session,onboarding_estate):
    from app.models.onboarding import LandlordImportFile
    app_id,headers=onboarding_estate
    email=uuid.uuid4().hex+'@example.com'
    original=f'Name,Email,Phone,House Number\nFile Landlord,{email},+2348012345678,012A\n'.encode()
    uploaded=await client.post('/api/v1/landlords/import-file',headers=headers,files={'file':('landlords.csv',original,'text/csv')})
    assert uploaded.status_code==200,uploaded.text
    files=(await client.get('/api/v1/landlords/import-files',headers=headers)).json()
    assert len(files)==1 and files[0]['pending_jobs']==1 and not files[0]['can_delete']
    file_id=files[0]['id']
    assert 'content' not in files[0] and files[0]['size_bytes']==len(original)
    saved=(await db_session.execute(select(LandlordImportFile).where(LandlordImportFile.id==uuid.UUID(file_id)))).scalar_one()
    assert saved.content==original and saved.batch_id==uuid.UUID(uploaded.json()[0]['batch_id'])
    response=await client.get(f'/api/v1/landlords/import-files/{file_id}/download',headers=headers)
    assert response.status_code==200 and response.content==original
    assert 'attachment;' in response.headers['content-disposition'] and response.headers['cache-control']=='no-store'
    assert (await client.delete(f'/api/v1/landlords/import-files/{file_id}',headers=headers)).status_code==409
    with patch('app.services.landlords.notification_service.send_registration_email',new_callable=AsyncMock,return_value=True):
        await process_one(db_session)
    files=(await client.get('/api/v1/landlords/import-files',headers=headers)).json()
    assert files[0]['can_delete']
    assert (await client.delete(f'/api/v1/landlords/import-files/{file_id}',headers=headers)).status_code==204
    await db_session.refresh(saved)
    assert saved.content is None and saved.deleted_at is not None
    assert (await client.get('/api/v1/landlords/import-files',headers=headers)).json()==[]
    assert (await client.get(f'/api/v1/landlords/import-files/{file_id}/download',headers=headers)).status_code==404
    job=(await db_session.execute(select(Job).where(Job.email==email))).scalar_one()
    assert job.status=='sent' and (await db_session.get(User,job.user_id)).email==email

@pytest.mark.asyncio
async def test_saved_files_are_private_to_estate_caretakers(client,db_session,onboarding_estate):
    app_id,headers=onboarding_estate
    content=f'Name,Email,Phone,House Number\nPerson,{uuid.uuid4().hex}@example.com,+2348012345678,12\n'.encode()
    await client.post('/api/v1/landlords/import-file',headers=headers,files={'file':('../../private.csv',content,'text/csv')})
    item=(await client.get('/api/v1/landlords/import-files',headers=headers)).json()[0]
    assert item['filename']=='private.csv'
    other_app='OTHER-'+app_id
    db_session.add(Estate(name='Other',app_id=other_app,is_active=True))
    other=User(email=uuid.uuid4().hex+'@example.com',hashed_password='unused',app_id=other_app,roles=[UserRole.CARETAKER])
    resident=User(email=uuid.uuid4().hex+'@example.com',hashed_password='unused',app_id=app_id,roles=[UserRole.RESIDENT])
    db_session.add_all([other,resident]);await db_session.commit()
    for user,expected in [(other,404),(resident,403)]:
        h={'X-App-Id':user.app_id,'Authorization':'Bearer '+create_access_token({'sub':user.email,'app_id':user.app_id})}
        assert (await client.get(f"/api/v1/landlords/import-files/{item['id']}/download",headers=h)).status_code==expected
        assert (await client.delete(f"/api/v1/landlords/import-files/{item['id']}",headers=h)).status_code==expected
        response=await client.get('/api/v1/landlords/import-files',headers=h)
        assert response.json()==[] if user is other else response.status_code==403
    assert (await client.get(f"/api/v1/landlords/import-files/{item['id']}/download",headers={'X-App-Id':app_id})).status_code==401

@pytest.mark.asyncio
async def test_failed_job_can_retry_after_original_file_deleted(client,db_session,onboarding_estate):
    _,headers=onboarding_estate
    content=f'Name,Email,Phone,House Number\nRetry Person,{uuid.uuid4().hex}@example.com,+2348012345678,12\n'.encode()
    response=await client.post('/api/v1/landlords/import-file',headers=headers,files={'file':('retry.csv',content,'text/csv')})
    job_id=response.json()[0]['job_id']
    job=await db_session.get(Job,uuid.UUID(job_id));job.status='failed';await db_session.commit()
    saved=(await client.get('/api/v1/landlords/import-files',headers=headers)).json()[0]
    assert (await client.delete(f"/api/v1/landlords/import-files/{saved['id']}",headers=headers)).status_code==204
    assert (await client.post(f'/api/v1/landlords/onboarding-jobs/{job_id}/retry',headers=headers)).status_code==200
    with patch('app.services.landlords.notification_service.send_registration_email',new_callable=AsyncMock,return_value=True):
        assert await process_one(db_session)
    await db_session.refresh(job)
    assert job.status=='sent' and job.user_id is not None

@pytest.mark.asyncio
async def test_long_upload_filename_keeps_extension(client,onboarding_estate):
    _,headers=onboarding_estate
    content=f'Name,Email,Phone,House Number\nLong File,{uuid.uuid4().hex}@example.com,+2348012345678,12\n'.encode()
    response=await client.post('/api/v1/landlords/import-file',headers=headers,files={'file':('x'*300+'.csv',content,'text/csv')})
    assert response.status_code==200,response.text
    item=(await client.get('/api/v1/landlords/import-files',headers=headers)).json()[0]
    assert len(item['filename'])<=255 and item['filename'].endswith('.csv')

@pytest.mark.asyncio
async def test_registration_derives_estate_from_host_without_header(client, db_session, onboarding_estate):
    app_id, _ = onboarding_estate
    host = User(email=uuid.uuid4().hex+'@example.com', app_id=app_id, full_name='Host',
                roles=[UserRole.LANDLORD], hashed_password='unused', house_number='20')
    db_session.add(host)
    await db_session.flush()
    token = RegistrationToken(code='REG'+uuid.uuid4().hex[:6].upper(), app_id=app_id,
                              landlord_id=host.id, house_number='20', expires_at=utc_now()+timedelta(days=1))
    db_session.add(token)
    await db_session.commit()
    response = await client.post('/api/v1/users/register', params={'registration_code': token.code}, json={
        'email': uuid.uuid4().hex+'@example.com', 'password': 'private-household-password',
        'full_name': 'Invited member', 'app_id': 'FORGED-ESTATE', 'roles': ['saas_owner'], 'house_number': 'FORGED',
    })
    assert response.status_code == 200, response.text
    user = response.json()
    assert user['app_id'] == app_id and user['landlord_id'] == str(host.id)
    assert user['house_number'] == '20' and user['roles'] == ['resident']
    assert (await client.post('/api/v1/users/register', params={'registration_code': token.code}, json={
        'email': uuid.uuid4().hex+'@example.com', 'password': 'private-household-password',
    })).status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['expired', 'revoked', 'used', 'inactive_estate', 'revoked_host', 'wrong_header', 'mismatched_host'])
async def test_headerless_registration_preserves_invitation_checks(client, db_session, onboarding_estate, invalid):
    app_id, _ = onboarding_estate
    host = User(email=uuid.uuid4().hex+'@example.com', app_id=app_id if invalid != 'mismatched_host' else 'OTHER',
                roles=[UserRole.LANDLORD], hashed_password='unused', is_revoked=invalid == 'revoked_host')
    db_session.add(host)
    await db_session.flush()
    token = RegistrationToken(code='REG'+uuid.uuid4().hex[:6].upper(), app_id=app_id, landlord_id=host.id,
                              expires_at=utc_now()+timedelta(days=-1 if invalid == 'expired' else 1),
                              is_revoked=invalid == 'revoked', is_used=invalid == 'used')
    db_session.add(token)
    if invalid == 'inactive_estate':
        estate = (await db_session.execute(select(Estate).where(Estate.app_id == app_id))).scalar_one()
        estate.is_active = False
    await db_session.commit()
    result = await client.post('/api/v1/users/register', params={'registration_code': token.code},
        headers={'X-App-Id': 'FORGED'} if invalid == 'wrong_header' else {},
        json={'email': uuid.uuid4().hex+'@example.com', 'password': 'private-household-password'})
    assert result.status_code == 400, result.text
