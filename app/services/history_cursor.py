import base64
import hashlib
import hmac
import json
from fastapi import HTTPException
from app.core.config import settings

def encode(data):
    payload=base64.urlsafe_b64encode(json.dumps(data,separators=(',',':')).encode()).decode().rstrip('=')
    signature=hmac.new(settings.SECRET_KEY.encode(),('history-cursor:'+payload).encode(),hashlib.sha256).hexdigest()
    return payload+'.'+signature

def decode(cursor):
    try:
        payload,signature=cursor.split('.')
        expected=hmac.new(settings.SECRET_KEY.encode(),('history-cursor:'+payload).encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature,expected): raise ValueError()
        return json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))
    except Exception:
        raise HTTPException(422,'Invalid history cursor; refresh the first page') from None
