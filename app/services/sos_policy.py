from datetime import timedelta
LIVE_DELIVERY_SECONDS = 40
RECEIPT_GRACE_SECONDS = 10
REPORT_RETENTION_HOURS = 24 * 28

def alarm_deadline(alert):
    return alert.created_at + timedelta(seconds=LIVE_DELIVERY_SECONDS)
