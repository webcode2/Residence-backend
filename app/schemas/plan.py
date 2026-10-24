from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict, model_validator

class PlanUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80, pattern=r'.*\S.*')
    description: str = Field(min_length=1, max_length=1000, pattern=r'.*\S.*')
    price_monthly: Decimal = Field(gt=0, le=99999999, max_digits=10, decimal_places=2)
    monthly_verifications_limit: Optional[int] = Field(default=None, ge=0, le=2147483647)
    rfid_enabled: bool
    log_retention_days: int = Field(ge=1, le=3650)
    realtime_alerts_enabled: bool
    discount_6_months: Decimal = Field(ge=0, lt=100, max_digits=5, decimal_places=2)
    discount_12_months: Decimal = Field(ge=0, lt=100, max_digits=5, decimal_places=2)
    version: int = Field(ge=0)

    @model_validator(mode='after')
    def positive_discounted_totals(self):
        for months in (6,12):
            discount = getattr(self,f'discount_{months}_months')
            total = (self.price_monthly*months*(100-discount)/100).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
            if total <= 0:
                raise ValueError('Each discounted payment total must be at least $0.01.')
        return self
