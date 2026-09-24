import enum
from typing import Dict, Any, Optional

class SubscriptionTier(str, enum.Enum):
    STARTER = "starter"         # $15/mo - 20k verifications, no RFID, 30 days retention, basic alerts
    STANDARD = "standard"       # $30/mo - 50k verifications, RFID, 90 days retention, real-time alerts
    PREMIUM = "premium"         # $70/mo - 300k verifications, RFID, 180 days retention, real-time alerts
    ENTERPRISE = "enterprise"   # $100/mo - Unlimited verifications, RFID, 365 days retention, instant alerts

TIER_DETAILS: Dict[SubscriptionTier, Dict[str, Any]] = {
    SubscriptionTier.STARTER: {
        "tier": SubscriptionTier.STARTER.value,
        "name": "Starter",
        "price_monthly": 15.00,
        "monthly_verifications_limit": 20000,
        "rfid_enabled": False,
        "log_retention_days": 30,
        "realtime_alerts_enabled": False,
        "description": "Ideal for small estates using visitor passcodes only (no RFID hardware support)."
    },
    SubscriptionTier.STANDARD: {
        "tier": SubscriptionTier.STANDARD.value,
        "name": "Standard",
        "price_monthly": 30.00,
        "monthly_verifications_limit": 50000,
        "rfid_enabled": True,
        "log_retention_days": 90,
        "realtime_alerts_enabled": True,
        "description": "Designed for mid-sized communities with RFID boom gates and real-time visitor alerts."
    },
    SubscriptionTier.PREMIUM: {
        "tier": SubscriptionTier.PREMIUM.value,
        "name": "Premium",
        "price_monthly": 70.00,
        "monthly_verifications_limit": 300000,
        "rfid_enabled": True,
        "log_retention_days": 180,
        "realtime_alerts_enabled": True,
        "description": "For high-traffic residential estates requiring 300k verifications and 6 months of audit logs."
    },
    SubscriptionTier.ENTERPRISE: {
        "tier": SubscriptionTier.ENTERPRISE.value,
        "name": "Enterprise",
        "price_monthly": 100.00,
        "monthly_verifications_limit": None,  # Unlimited
        "rfid_enabled": True,
        "log_retention_days": 365,
        "realtime_alerts_enabled": True,
        "description": "Full-scale access control with unlimited verifications, 1-year log retention, and instant alerts."
    }
}

def get_tier_config(tier: str | SubscriptionTier) -> Dict[str, Any]:
    """Retrieve tier configuration defaults."""
    if isinstance(tier, str):
        try:
            tier_enum = SubscriptionTier(tier.lower())
        except ValueError:
            tier_enum = SubscriptionTier.STARTER
    else:
        tier_enum = tier
    return TIER_DETAILS.get(tier_enum, TIER_DETAILS[SubscriptionTier.STARTER])
