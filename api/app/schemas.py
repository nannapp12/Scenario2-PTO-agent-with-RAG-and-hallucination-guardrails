from datetime import date
from typing import Literal

from pydantic import BaseModel


class Citation(BaseModel):
    section: str
    excerpt: str
    score: float


class Tenure(BaseModel):
    years: int
    months: int
    completed_months: int


class PtoResponse(BaseModel):
    employee_id: str
    name_masked: str | None             # PII is masked; full values never leave the API
    email_masked: str | None
    start_date: date
    as_of: date
    employment_type: str
    eligible: bool
    tenure: Tenure
    periods_completed: int              # biweekly accrual periods since the start date
    annual_allotment_days: float        # current tier's maximum per year
    hours_per_period: float             # current tier's biweekly accrual
    accrued_hours: float
    accrued_days: float
    available_hours: float
    available_days: float
    cap_days: float | None
    at_cap: bool
    in_waiting_period: bool
    waiting_period_ends: date
    assumes_no_leave_taken: bool = True
    policy_version: str
    explanation: str
    explanation_source: Literal["llm", "template"]
    citations: list[Citation] = []
    warnings: list[str] = []
