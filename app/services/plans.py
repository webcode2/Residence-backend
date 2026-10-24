from decimal import Decimal, ROUND_HALF_UP
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.core.tiers import SubscriptionTier, get_tier_config
from app.models.plan import SubscriptionPlan
from app.models.admin_audit import AdminAuditLog

FIELDS = ('name','description','price_monthly','monthly_verifications_limit','rfid_enabled',
          'log_retention_days','realtime_alerts_enabled','discount_6_months','discount_12_months','version')

def quote(plan, months):
    if months not in (1,6,12):
        raise HTTPException(422, 'Choose 1, 6, or 12 months.')
    discount = Decimal(str(plan.get(f'discount_{months}_months', 0))) if months != 1 else Decimal(0)
    subtotal = Decimal(str(plan['price_monthly'])) * months
    total = (subtotal * (100-discount)/100).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    return {'billing_months':months,'discount_percent':float(discount),'subtotal':float(subtotal),
            'amount':float(total),'savings':float(subtotal-total),'currency':'USD'}

async def get_plan(db, tier):
    try: key = SubscriptionTier(tier).value
    except ValueError: raise HTTPException(404,'Plan not found')
    row = await db.get(SubscriptionPlan,key)
    if row:
        result = {'tier':key, **{field:getattr(row,field) for field in FIELDS}}
    else:
        result = {**get_tier_config(key), 'discount_6_months':0,'discount_12_months':0,'version':0}
    for field in ('price_monthly','discount_6_months','discount_12_months'):
        result[field] = float(result[field])
    result['quotes'] = [quote(result,months) for months in (1,6,12)]
    return result

async def list_plans(db):
    return [await get_plan(db,tier) for tier in SubscriptionTier]

async def update_plan(db, tier, data, actor):
    before = await get_plan(db,tier)
    row = (await db.execute(select(SubscriptionPlan).where(SubscriptionPlan.tier==tier).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if (row.version if row else 0) != data.version:
        raise HTTPException(409,'This plan changed while you were editing. Refresh and try again.')
    if row is None:
        row = SubscriptionPlan(tier=tier)
        db.add(row)
    for field,value in data.model_dump(exclude={'version'}).items():
        setattr(row,field,value.strip() if isinstance(value,str) else value)
    row.version = data.version+1
    after = data.model_dump(mode='json')
    changes = {key:{'before':before[key],'after':value} for key,value in after.items() if key!='version' and str(before[key])!=str(value)}
    db.add(AdminAuditLog(actor_id=actor.id,actor_email=actor.email,action='plan.update',target_type='plan',target_id=tier,changes=changes))
    try: await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409,'This plan changed while you were editing. Refresh and try again.')
    return await get_plan(db,tier)
