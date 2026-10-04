"""Deterministic PTO (vacation) accrual calculation. The LLM never produces or changes these numbers.

The rules live in pto_policy.json, transcribed from the Employee Handbook, section
V.B "Vacation Benefits", and reviewed by HR. When the handbook changes, update the
JSON and its handbook_sha256 (the ingestion pipeline prints the new hash); until then
/pto returns a warning.

How the handbook is applied:
- Only the eligible employment types (regular full-time) accrue vacation.
- Accrual is per biweekly pay period: one period completes every 14 days from the
  start date. The tier (hours per period) is set by completed years of continuous
  service at the start of each period ("date of hire through end of year 5", ...).
- Each tier also has a maximum number of days per year, counted per fiscal year.
- The total balance may not exceed cap_multiple x the current annual allotment; once
  there, accrual stops (no leave is taken in this calculation, so it stays there).
- Accrued vacation can't be used during the waiting period after the start date.
Exact fractions are used throughout; only the results are rounded (2 decimals).
"""
import calendar
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from pathlib import Path

POLICY_PATH = Path(__file__).with_name("pto_policy.json")


@dataclass(frozen=True)
class Tier:
    min_years: int
    hours_per_period: Fraction
    max_days_per_year: Fraction


@dataclass(frozen=True)
class PtoPolicy:
    version: str
    handbook_doc_id: str
    handbook_sha256: str
    eligible_employment_types: frozenset[str]
    accrual_period_days: int
    hours_per_day: Fraction
    tiers: tuple[Tier, ...]
    fiscal_year_start_month: int
    cap_multiple: Fraction | None
    waiting_period_days: int

    def tier_for(self, completed_years: int) -> Tier:
        return [t for t in self.tiers if t.min_years <= completed_years][-1]


@dataclass(frozen=True)
class PtoResult:
    as_of: date
    eligible: bool
    completed_months: int
    completed_years: int
    periods_completed: int
    annual_allotment_days: Decimal   # current tier's max days per year
    hours_per_period: Decimal        # current tier's biweekly accrual
    accrued_hours: Decimal
    accrued_days: Decimal
    available_hours: Decimal
    available_days: Decimal
    cap_days: Decimal | None
    at_cap: bool
    in_waiting_period: bool
    waiting_period_ends: date
    not_started: bool


def _frac(v) -> Fraction:
    return Fraction(str(v))


def load_policy(path: Path = POLICY_PATH) -> PtoPolicy:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    tiers = tuple(sorted(
        (Tier(int(t["min_years"]), _frac(t["hours_per_period"]), _frac(t["max_days_per_year"]))
         for t in raw["tiers"]),
        key=lambda t: t.min_years,
    ))
    if not tiers or tiers[0].min_years != 0:
        raise ValueError("The PTO policy needs a tier with min_years = 0.")
    if any(t.hours_per_period < 0 or t.max_days_per_year < 0 for t in tiers):
        raise ValueError("PTO accrual values cannot be negative.")
    month = int(raw.get("fiscal_year_start_month", 1))
    if not 1 <= month <= 12:
        raise ValueError("fiscal_year_start_month must be 1-12.")
    cap = raw.get("cap_multiple")
    return PtoPolicy(
        version=str(raw["version"]),
        handbook_doc_id=raw["handbook_doc_id"],
        handbook_sha256=raw.get("handbook_sha256", ""),
        eligible_employment_types=frozenset(raw["eligible_employment_types"]),
        accrual_period_days=int(raw["accrual_period_days"]),
        hours_per_day=_frac(raw["hours_per_day"]),
        tiers=tiers,
        fiscal_year_start_month=month,
        cap_multiple=_frac(cap) if cap is not None else None,
        waiting_period_days=int(raw.get("waiting_period_days", 0)),
    )


def add_months(d: date, n: int) -> date:
    """Same day n months later, clamped to the month's last day (Jan 31 + 1 -> Feb 28/29)."""
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def completed_months(start: date, as_of: date) -> int:
    if as_of < start:
        return 0
    n = (as_of.year - start.year) * 12 + as_of.month - start.month
    return n - 1 if add_months(start, n) > as_of else n


def fiscal_year(d: date, start_month: int) -> int:
    """Fiscal years are labelled by the calendar year they start in."""
    return d.year if d.month >= start_month else d.year - 1


def _round(x: Fraction) -> Decimal:
    return (Decimal(x.numerator) / Decimal(x.denominator)).quantize(Decimal("0.01"), ROUND_HALF_UP)


def calculate(start_date: date, as_of: date, employment_type: str, policy: PtoPolicy) -> PtoResult:
    """Vacation available as of `as_of`, assuming none has been taken."""
    eligible = employment_type in policy.eligible_employment_types
    months = completed_months(start_date, as_of)
    years = months // 12
    tier_now = policy.tier_for(years)
    hpd = policy.hours_per_day
    waiting_end = start_date + timedelta(days=policy.waiting_period_days)

    periods = max((as_of - start_date).days // policy.accrual_period_days, 0) if eligible else 0
    balance = Fraction(0)
    per_fiscal_year: dict[int, Fraction] = defaultdict(Fraction)
    for k in range(periods):
        p_start = start_date + timedelta(days=k * policy.accrual_period_days)
        p_end = p_start + timedelta(days=policy.accrual_period_days)
        tier = policy.tier_for(completed_months(start_date, p_start) // 12)
        fy = fiscal_year(p_end, policy.fiscal_year_start_month)
        add = min(tier.hours_per_period, tier.max_days_per_year * hpd - per_fiscal_year[fy])
        if policy.cap_multiple is not None:
            add = min(add, tier.max_days_per_year * hpd * policy.cap_multiple - balance)
        add = max(add, Fraction(0))
        balance += add
        per_fiscal_year[fy] += add

    cap_hours = tier_now.max_days_per_year * hpd * policy.cap_multiple if policy.cap_multiple is not None else None
    in_waiting = as_of < waiting_end
    usable = Fraction(0) if in_waiting else balance
    return PtoResult(
        as_of=as_of,
        eligible=eligible,
        completed_months=months,
        completed_years=years,
        periods_completed=periods,
        annual_allotment_days=_round(tier_now.max_days_per_year) if eligible else Decimal("0.00"),
        hours_per_period=(Decimal(tier_now.hours_per_period.numerator) / tier_now.hours_per_period.denominator
                          if eligible else Decimal("0")),   # exact handbook value, e.g. 3.077
        accrued_hours=_round(balance),
        accrued_days=_round(balance / hpd),
        available_hours=_round(usable),
        available_days=_round(usable / hpd),
        cap_days=_round(cap_hours / hpd) if cap_hours is not None and eligible else None,
        at_cap=eligible and cap_hours is not None and balance >= cap_hours,
        in_waiting_period=in_waiting,
        waiting_period_ends=waiting_end,
        not_started=as_of < start_date,
    )
