from datetime import date
from decimal import Decimal

import pytest

from app.pto import add_months, calculate, completed_months, load_policy

POLICY = load_policy()


def test_policy_matches_handbook_section_vb():
    assert [(t.min_years, float(t.hours_per_period), int(t.max_days_per_year)) for t in POLICY.tiers] == [
        (0, 3.077, 10), (5, 4.62, 15), (10, 6.15, 20)]
    assert POLICY.eligible_employment_types == {"FULL_TIME"}
    assert POLICY.accrual_period_days == 14 and POLICY.waiting_period_days == 90
    assert POLICY.cap_multiple == 2


@pytest.mark.parametrize("start,n,expected", [
    (date(2024, 1, 31), 1, date(2024, 2, 29)),
    (date(2023, 1, 31), 1, date(2023, 2, 28)),
    (date(2024, 11, 15), 3, date(2025, 2, 15)),
])
def test_add_months_clamps_month_end(start, n, expected):
    assert add_months(start, n) == expected


def test_completed_months():
    assert completed_months(date(2024, 1, 15), date(2024, 2, 14)) == 0
    assert completed_months(date(2024, 1, 15), date(2024, 2, 15)) == 1
    assert completed_months(date(2024, 1, 31), date(2024, 2, 29)) == 1
    assert completed_months(date(2025, 1, 1), date(2024, 1, 1)) == 0


def test_start_day_has_nothing_and_is_in_waiting_period():
    r = calculate(date(2026, 1, 1), date(2026, 1, 1), "FULL_TIME", POLICY)
    assert r.periods_completed == 0 and r.accrued_days == 0 and r.in_waiting_period


def test_waiting_period_accrues_but_nothing_is_available():
    r = calculate(date(2026, 1, 1), date(2026, 3, 1), "FULL_TIME", POLICY)  # 59 days -> 4 periods
    assert r.periods_completed == 4
    assert r.accrued_hours == Decimal("12.31")      # 4 x 3.077 = 12.308
    assert r.accrued_days == Decimal("1.54")
    assert r.available_days == 0 and r.in_waiting_period
    assert r.waiting_period_ends == date(2026, 4, 1)


def test_first_year_is_ten_days():
    r = calculate(date(2025, 1, 6), date(2026, 1, 5), "FULL_TIME", POLICY)  # 364 days -> 26 periods
    assert r.periods_completed == 26
    assert r.accrued_days == Decimal("10.00")        # 26 x 3.077 h = 80.002 h
    assert r.available_days == Decimal("10.00") and not r.in_waiting_period


def test_fiscal_year_maximum_limits_a_27_period_year():
    # First period ends 2025-01-01, so all 27 period ends fall in fiscal year 2025.
    r = calculate(date(2024, 12, 18), date(2025, 12, 31), "FULL_TIME", POLICY)
    assert r.periods_completed == 27                 # 27 x 3.077 = 83.079 h uncapped
    assert r.accrued_hours == Decimal("80.00")       # max 10 days per year
    assert r.accrued_days == Decimal("10.00")


def test_tier_two_starts_after_five_years_and_cap_is_twice_annual():
    r = calculate(date(2020, 1, 6), date(2026, 1, 5), "FULL_TIME", POLICY)
    assert r.completed_years == 5                    # in year 6 of service
    assert r.hours_per_period == Decimal("4.62") and r.annual_allotment_days == Decimal("15.00")
    assert r.cap_days == Decimal("30.00")
    assert r.at_cap and r.available_days == Decimal("30.00")


def test_long_service_is_capped_at_forty_days():
    r = calculate(date(2014, 3, 17), date(2026, 10, 3), "FULL_TIME", POLICY)
    assert r.hours_per_period == Decimal("6.15") and r.annual_allotment_days == Decimal("20.00")
    assert r.at_cap and r.available_days == Decimal("40.00") and r.available_hours == Decimal("320.00")


def test_rate_switches_on_fifth_anniversary():
    before = calculate(date(2020, 1, 6), date(2025, 1, 5), "FULL_TIME", POLICY)
    after = calculate(date(2020, 1, 6), date(2025, 1, 6), "FULL_TIME", POLICY)
    assert before.hours_per_period == Decimal("3.077")
    assert after.hours_per_period == Decimal("4.62")


@pytest.mark.parametrize("etype", ["PART_TIME", "TEMPORARY"])
def test_only_regular_full_time_is_eligible(etype):
    r = calculate(date(2022, 1, 10), date(2026, 10, 3), etype, POLICY)
    assert not r.eligible and r.available_days == 0 and r.periods_completed == 0 and r.cap_days is None


def test_future_start_date():
    r = calculate(date(2026, 11, 2), date(2026, 10, 3), "FULL_TIME", POLICY)
    assert r.not_started and r.accrued_days == 0 and r.available_days == 0
