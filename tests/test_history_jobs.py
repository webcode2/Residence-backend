import csv
import io
import uuid
from datetime import timedelta
from unittest.mock import patch
import pytest
from sqlalchemy import select, func
from app.models.base import utc_now
from app.models.estate import Estate,Subscription
from app.models.user import User,UserRole
from app.models.access_log import AccessLog
from app.models.history import HistoryJob,HistoryExportChunk
from app.models.admin_audit import AdminAuditLog
from app.services.auth import create_access_token
from app.services.history_jobs import process_one,cleanup_exports

@pytest.fixture
async def history_estates(db_session):
    result=[]
    for days in (30,365):
        app='HIST-'+uuid.uuid4().hex[:8]
        estate=Estate(app_id=app,name='History test',is_active=True);db_session.add(estate);await db_session.flush()
        db_session.add(Subscription(estate_id=estate.id,tier='starter' if days==30 else 'enterprise',status='active',log_retention_days=days,expiry_date=(utc_now()+timedelta(days=365)).date()))
        user=User(app_id=app,email=uuid.uuid4().hex+'@example.com',hashed_password='unused',roles=[UserRole.CARETAKER])
        db_session.add(user);await db_session.flush()
        now=utc_now()
        for i in range(5):
            db_session.add(AccessLog(app_id=app,event_type='visitor_token_verification',identifier=f'CODE{i}',status='authorized',visitor_name='=1+1' if i==0 else f'Person{i}',created_at=now-timedelta(days=1)))
        db_session.add(AccessLog(app_id=app,event_type='visitor_token_verification',identifier='OLD',status='denied',created_at=now-timedelta(days=40)))
        result.append((app,user,{'X-App-Id':app,'Authorization':'Bearer '+create_access_token({'sub':user.email,'app_id':app})}))
    await db_session.commit();return result

async def finish(db,job_id,size=2):
    for _ in range(20):
        job=await db.get(HistoryJob,uuid.UUID(job_id),populate_existing=True)
        if job.status in ['completed','awaiting_confirmation','failed','expired']:return job
        assert await process_one(db,size=size)
    raise AssertionError('Job did not finish in bounded steps')

@pytest.mark.asyncio
async def test_history_cursor_is_stable_private_and_retention_scoped(client,db_session,history_estates):
    app,_,headers=history_estates[0]
    first=(await client.get('/api/v1/access-logs/?limit=2',headers=headers)).json()
    assert first['has_more'] and len(first['logs'])==2
    db_session.add(AccessLog(app_id=app,event_type='rfid_verification',identifier='NEW',status='authorized',created_at=utc_now()))
    await db_session.commit()
    seen=[row['id'] for row in first['logs']];cursor=first['next_cursor']
    while cursor:
        response=await client.get('/api/v1/access-logs/',params={'limit':2,'cursor':cursor},headers=headers)
        assert response.status_code==200,response.text
        data=response.json();seen.extend(row['id'] for row in data['logs']);cursor=data['next_cursor']
        assert all(row['identifier']!='NEW' for row in data['logs'])
    assert len(seen)==5 and len(set(seen))==5
    assert (await client.get('/api/v1/access-logs/',params={'cursor':first['next_cursor']},headers=history_estates[1][2])).status_code==422
    assert (await client.get('/api/v1/access-logs/',params={'cursor':first['next_cursor']+'bad'},headers=headers)).status_code==422
    assert (await client.get('/api/v1/access-logs/',params={'cursor':first['next_cursor'],'status':'denied'},headers=headers)).status_code==422
    year=(await client.get('/api/v1/access-logs/',headers=history_estates[1][2])).json()
    assert year['total_returned']==6 and year['log_retention_days']==365

@pytest.mark.asyncio
async def test_export_is_chunked_formula_safe_and_estate_private(client,db_session,history_estates):
    app,_,headers=history_estates[0]
    response=await client.post('/api/v1/access-logs/exports',json={},headers=headers)
    assert response.status_code==202,response.text
    job_id=response.json()['id']
    assert (await client.get(f'/api/v1/access-logs/jobs/{job_id}/download',headers=headers)).status_code==409
    job=await finish(db_session,job_id)
    assert job.status=='completed' and job.processed_count==5
    assert await db_session.scalar(select(func.count()).select_from(HistoryExportChunk).where(HistoryExportChunk.job_id==job.id))==3
    response=await client.get(f'/api/v1/access-logs/jobs/{job_id}/download',headers=headers)
    assert response.status_code==200 and response.headers['cache-control']=='no-store'
    rows=list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
    assert len(rows)==5 and all(row['app_id']==app for row in rows)
    assert any(row['visitor_name']=="'=1+1" for row in rows)
    assert (await client.get(f'/api/v1/access-logs/jobs/{job_id}/download',headers=history_estates[1][2])).status_code==404
    subscription=(await db_session.execute(select(Subscription).join(Estate,Estate.id==Subscription.estate_id).where(Estate.app_id==app))).scalar_one()
    subscription.log_retention_days=1;await db_session.commit()
    listed=(await client.get('/api/v1/access-logs/jobs',headers=headers)).json()[0]
    assert listed['status']=='expired' and not listed['can_download']
    assert (await client.get(f'/api/v1/access-logs/jobs/{job_id}/download',headers=headers)).status_code==409
    subscription.log_retention_days=30
    job.expires_at=utc_now()-timedelta(seconds=1);await db_session.commit()
    assert (await client.get(f'/api/v1/access-logs/jobs/{job_id}/download',headers=headers)).status_code==409
    assert await cleanup_exports(db_session,size=1)==1

@pytest.mark.asyncio
async def test_purge_requires_preview_confirmation_and_preserves_other_estates(client,db_session,history_estates):
    app,user,headers=history_estates[0]
    exported=await client.post('/api/v1/access-logs/exports',json={},headers=headers)
    export_id=exported.json()['id'];await finish(db_session,export_id)
    before=(utc_now()-timedelta(hours=1)).isoformat()
    response=await client.post('/api/v1/access-logs/purge-preview',json={'end_at':before},headers=headers)
    assert response.status_code==202,response.text
    job_id=response.json()['id'];job=await finish(db_session,job_id)
    assert job.status=='awaiting_confirmation' and job.preview_count==6
    assert await db_session.scalar(select(func.count()).select_from(AccessLog).where(AccessLog.app_id==app))==6
    assert (await client.post(f'/api/v1/access-logs/jobs/{job_id}/confirm',json={'confirmation':'yes'},headers=headers)).status_code==422
    assert (await client.post(f'/api/v1/access-logs/jobs/{job_id}/confirm',json={'confirmation':'PURGE'},headers=history_estates[1][2])).status_code==404
    response=await client.post(f'/api/v1/access-logs/jobs/{job_id}/confirm',json={'confirmation':'PURGE'},headers=headers)
    assert response.status_code==200,response.text
    assert (await client.get(f'/api/v1/access-logs/jobs/{export_id}/download',headers=headers)).status_code==409
    assert (await finish(db_session,job_id)).processed_count==6
    assert await db_session.scalar(select(func.count()).select_from(AccessLog).where(AccessLog.app_id==app))==0
    assert await db_session.scalar(select(func.count()).select_from(AccessLog).where(AccessLog.app_id==history_estates[1][0]))==6
    actions=(await db_session.execute(select(AdminAuditLog).where(AdminAuditLog.target_id==job_id))).scalars().all()
    assert {a.action for a in actions}>={'history.purge.preview','history.purge.confirm','history.purge.complete'}
    assert all(a.actor_email==user.email for a in actions)
    assert (await client.post(f'/api/v1/access-logs/jobs/{job_id}/confirm',json={'confirmation':'PURGE'},headers=headers)).status_code==409

@pytest.mark.asyncio
async def test_purge_preview_expires_and_guards_cannot_purge(client,db_session,history_estates):
    app,_,headers=history_estates[0]
    response=await client.post('/api/v1/access-logs/purge-preview',json={'end_at':utc_now().isoformat()},headers=headers)
    job=await finish(db_session,response.json()['id'])
    job.expires_at=utc_now()-timedelta(seconds=1);await db_session.commit()
    assert (await client.post(f'/api/v1/access-logs/jobs/{job.id}/confirm',json={'confirmation':'PURGE'},headers=headers)).status_code==422
    guard=User(app_id=app,email=uuid.uuid4().hex+'@example.com',hashed_password='unused',roles=[UserRole.SECURITY]);db_session.add(guard);await db_session.commit()
    gh={'X-App-Id':app,'Authorization':'Bearer '+create_access_token({'sub':guard.email,'app_id':app})}
    assert (await client.post('/api/v1/access-logs/purge-preview',json={'end_at':utc_now().isoformat()},headers=gh)).status_code==403
    assert (await client.post('/api/v1/access-logs/exports',json={},headers=gh)).status_code==403

@pytest.mark.asyncio
async def test_failed_export_retry_keeps_cursor_without_duplicate_rows(client,db_session,history_estates):
    _,_,headers=history_estates[0]
    response=await client.post('/api/v1/access-logs/exports',json={},headers=headers);job_id=response.json()['id']
    await process_one(db_session,size=2)
    job=await db_session.get(HistoryJob,uuid.UUID(job_id));job.attempts=4;await db_session.commit()
    with patch('app.services.history_jobs.csv_cell',side_effect=RuntimeError('isolated failure')):
        await process_one(db_session,size=2)
    job=await db_session.get(HistoryJob,uuid.UUID(job_id),populate_existing=True)
    assert job.status=='failed' and job.processed_count==2
    assert (await client.post(f'/api/v1/access-logs/jobs/{job_id}/retry',headers=headers)).status_code==200
    await finish(db_session,job_id)
    response=await client.get(f'/api/v1/access-logs/jobs/{job_id}/download',headers=headers)
    rows=list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
    assert len(rows)==5 and len({r['id'] for r in rows})==5

@pytest.mark.asyncio
async def test_retention_batches_and_token_cleanup_preserve_active_visitors(db_session,history_estates):
    from app.services.history_policy import delete_log_batch
    from app.services.maintenance import cleanup_expired_tokens
    from app.models.token import VisitorToken
    app,user,_=history_estates[0];year,year_user,_=history_estates[1]
    for estate_app,resident in [(app,user),(year,year_user)]:
        for state in ['expired','checked_out','checked_in']:
            db_session.add(VisitorToken(app_id=estate_app,resident_id=resident.id,code='TOKENX',visitor_name='Old visitor',status=state,expires_at=utc_now()-timedelta(days=40)))
    await db_session.commit()
    assert await delete_log_batch(db_session,app,utc_now()-timedelta(days=30),size=1)==1
    await cleanup_expired_tokens(db_session)
    remaining=(await db_session.execute(select(VisitorToken).where(VisitorToken.app_id==app))).scalars().all()
    assert [r.status for r in remaining]==['checked_in']
    assert await db_session.scalar(select(func.count()).select_from(VisitorToken).where(VisitorToken.app_id==year))==3

@pytest.mark.asyncio
async def test_old_token_volume_uses_live_index_and_history_uses_cursor_index(db_session,history_estates):
    import json
    from pathlib import Path
    from sqlalchemy import insert,text
    from app.models.token import VisitorToken
    app,user,_=history_estates[0];now=utc_now()
    old=now-timedelta(days=400)
    payload=[dict(id=uuid.uuid4(),app_id=app,resident_id=user.id,code='SAMEAA',visitor_name='Historical visitor',status='expired',expires_at=old,created_at=old,updated_at=old,is_used=True) for _ in range(20000)]
    await db_session.execute(insert(VisitorToken),payload)
    db_session.add(VisitorToken(app_id=app,resident_id=user.id,code='SAMEAA',visitor_name='Live visitor',status='pending',expires_at=now+timedelta(hours=1)))
    await db_session.execute(insert(AccessLog),[dict(id=uuid.uuid4(),app_id=app,event_type='rfid_verification',identifier='historical',status='authorized',created_at=now-timedelta(minutes=i),updated_at=now) for i in range(20000)])
    await db_session.commit()
    await db_session.execute(text('ANALYZE visitor_tokens'));await db_session.execute(text('ANALYZE access_logs'))
    token_plan=(await db_session.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT id FROM visitor_tokens WHERE app_id=:app AND code='SAMEAA' AND status='pending' AND is_used=false AND expires_at>:now"),{'app':app,'now':now})).scalar_one()
    history_plan=(await db_session.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT id,created_at FROM access_logs WHERE app_id=:app AND created_at>=:cutoff ORDER BY created_at DESC,id DESC LIMIT 50"),{'app':app,'cutoff':now-timedelta(days=30)})).scalar_one()
    def parse(value):return json.loads(value) if isinstance(value,str) else value
    token_plan=parse(token_plan);history_plan=parse(history_plan)
    def indexes(plan):
        found=[]
        def walk(node):
            if 'Index Name' in node:found.append(node['Index Name'])
            for child in node.get('Plans',[]):walk(child)
        walk(plan[0]['Plan']);return found
    assert any(name.startswith('ix_vis_tokens_live_') or name=='ix_vis_tokens_expiry_cleanup' for name in indexes(token_plan)),token_plan
    assert 'ix_access_logs_estate_cursor' in indexes(history_plan),history_plan
    Path('/tmp/residence-history-query-plans.json').write_text(json.dumps({'historical_token_rows':20000,'history_rows':20000,'verification':token_plan,'history':history_plan},default=str,indent=2))

@pytest.mark.asyncio
async def test_late_queued_events_cannot_resurrect_confirmed_purge(client,db_session,history_estates):
    from app.services.access_logs import drain_access_log_queue
    from unittest.mock import AsyncMock
    app,_,headers=history_estates[0]
    response=await client.post('/api/v1/access-logs/purge-preview',json={'end_at':(utc_now()-timedelta(hours=1)).isoformat()},headers=headers)
    job_id=response.json()['id'];await finish(db_session,job_id)
    assert (await client.post(f'/api/v1/access-logs/jobs/{job_id}/confirm',json={'confirmation':'PURGE'},headers=headers)).status_code==200
    await finish(db_session,job_id)
    old={'app_id':app,'event_type':'rfid_verification','identifier':'LATE','status':'authorized','created_at':(utc_now()-timedelta(days=1)).isoformat()}
    new={**old,'identifier':'NEW','created_at':utc_now().isoformat()}
    with patch('app.services.access_logs.dequeue_access_logs',new_callable=AsyncMock,return_value=[('old',old),('new',new)]):
        assert await drain_access_log_queue(db_session)==1
    rows=(await db_session.execute(select(AccessLog).where(AccessLog.app_id==app))).scalars().all()
    assert [row.identifier for row in rows]==['NEW']

@pytest.mark.asyncio
async def test_maintenance_renews_lease_and_preserves_successor_lock():
    import asyncio
    from redis.asyncio import Redis
    from app.core.config import settings
    from app.core.scheduler import _run_with_lease
    redis=Redis.from_url(settings.REDIS_URL,decode_responses=True)
    key='history-lease-test:'+uuid.uuid4().hex
    try:
        await redis.set(key,'first',ex=1)
        async def long_job():await asyncio.sleep(1.2)
        await _run_with_lease(redis,key,'first',1,long_job)
        assert not await redis.exists(key)
        await redis.set(key,'first',ex=1)
        stopped=asyncio.Event()
        async def interrupted_job():
            try: await asyncio.sleep(10)
            finally: stopped.set()
        task=asyncio.create_task(_run_with_lease(redis,key,'first',1,interrupted_job))
        await redis.set(key,'successor',ex=10)
        with pytest.raises(RuntimeError,match='lease lost'):await task
        assert stopped.is_set() and await redis.get(key)=='successor'
    finally:
        await redis.delete(key);await redis.aclose()

@pytest.mark.asyncio
async def test_history_worker_respects_global_database_budget(client,engine,db_session,history_estates):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.services.history_jobs import HISTORY_LOCK_KEY
    _,_,headers=history_estates[0]
    response=await client.post('/api/v1/access-logs/exports',headers=headers,json={})
    job_id=response.json()['id']
    factory=async_sessionmaker(engine,expire_on_commit=False)
    async with factory() as holder:
        await holder.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':HISTORY_LOCK_KEY})
        assert not await process_one(db_session,size=2)
        await holder.rollback()
    assert await process_one(db_session,size=2)
    job=await db_session.get(HistoryJob,uuid.UUID(job_id),populate_existing=True)
    assert job.processed_count==2
