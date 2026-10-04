"""GET /pto/{employee_id}: validate -> deterministic lookup -> compute -> retrieve -> explain.

Guardrail order matters: the ID is checked against the database BEFORE any retrieval
or LLM call, and an unknown or malformed ID stops the request with EmployeeNotFound.
The model never sees a request for an employee that doesn't exist, so it can't guess.
"""
import hashlib
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from common.pii import mask_email, mask_name

from .employees import EmployeeRepository
from .pto import PtoPolicy, calculate
from .pto_explainer import PtoExplainer
from .rag import HandbookRetriever
from .schemas import Citation, PtoResponse, Tenure

log = logging.getLogger(__name__)

PTO_QUERY = ("Vacation benefits: who is eligible, rate of accrual by years of continuous service "
             "(hours biweekly, maximum days per year), cap on accrued vacation, waiting period before use.")


class EmployeeNotFound(Exception):
    pass


def _ref(employee_id: str) -> str:
    """Log-safe reference for an employee ID."""
    return hashlib.sha256(employee_id.encode()).hexdigest()[:12]


class PtoService:
    def __init__(self, repo: EmployeeRepository, retriever: HandbookRetriever | None,
                 explainer: PtoExplainer, policy: PtoPolicy, timezone: str, id_pattern: str):
        self.repo, self.retriever, self.explainer, self.policy = repo, retriever, explainer, policy
        self.tz = ZoneInfo(timezone)
        self.id_re = re.compile(id_pattern)

    def get(self, employee_id: str) -> PtoResponse:
        # 1. Validate format, then look the ID up in the database. No match -> stop.
        if not self.id_re.fullmatch(employee_id):
            raise EmployeeNotFound
        emp = self.repo.find(employee_id)
        if emp is None:
            log.info("PTO lookup: employee %s not found", _ref(employee_id))
            raise EmployeeNotFound

        # 2. Compute the balance in code.
        r = calculate(emp.start_date, datetime.now(self.tz).date(), emp.employment_type, self.policy)

        # 3. Retrieve the handbook policy text (for citations and the explanation).
        warnings: list[str] = []
        citations: list[Citation] = []
        if self.retriever is not None:
            try:
                citations = self.retriever.search(PTO_QUERY, self.policy.handbook_doc_id)
            except Exception:
                log.exception("Handbook retrieval failed")
        if not citations:
            warnings.append("Handbook excerpts are unavailable; the balance is still computed from the PTO policy.")
        try:
            sha = self.repo.handbook_sha256(self.policy.handbook_doc_id)
            if sha and self.policy.handbook_sha256 and sha != self.policy.handbook_sha256:
                warnings.append("The handbook has changed since the PTO rules in this system were last reviewed. "
                                "HR should confirm the accrual policy.")
        except Exception:
            log.exception("Handbook version check failed")
        if r.not_started:
            warnings.append("The employee's start date is in the future.")

        # 4. Explain (LLM, grounded) or fall back to a template.
        years, months = divmod(r.completed_months, 12)
        facts = {
            "as_of": r.as_of.isoformat(),
            "employment_type": emp.employment_type,
            "eligible": r.eligible,
            "completed_years_of_service": years,
            "extra_months_of_service": months,
            "biweekly_periods_completed": r.periods_completed,
            "hours_per_biweekly_period": str(r.hours_per_period),
            "annual_allotment_days": str(r.annual_allotment_days),
            "accrued_hours": str(r.accrued_hours),
            "accrued_days": str(r.accrued_days),
            "available_hours": str(r.available_hours),
            "available_days": str(r.available_days),
            "balance_cap_days": str(r.cap_days) if r.cap_days is not None else None,
            "at_cap": r.at_cap,
            "in_waiting_period": r.in_waiting_period,
            "waiting_period_days": self.policy.waiting_period_days,
            "waiting_period_ends": r.waiting_period_ends.isoformat(),
            "not_started": r.not_started,
            "hours_per_day": str(self.policy.hours_per_day),
            "assumes_no_leave_taken": True,
        }
        explanation, source = self.explainer.explain(facts, citations)

        return PtoResponse(
            employee_id=emp.employee_id,
            name_masked=mask_name(emp.full_name),
            email_masked=mask_email(emp.email),
            start_date=emp.start_date,
            as_of=r.as_of,
            employment_type=emp.employment_type,
            eligible=r.eligible,
            tenure=Tenure(years=years, months=months, completed_months=r.completed_months),
            periods_completed=r.periods_completed,
            annual_allotment_days=float(r.annual_allotment_days),
            hours_per_period=float(r.hours_per_period),
            accrued_hours=float(r.accrued_hours),
            accrued_days=float(r.accrued_days),
            available_hours=float(r.available_hours),
            available_days=float(r.available_days),
            cap_days=float(r.cap_days) if r.cap_days is not None else None,
            at_cap=r.at_cap,
            in_waiting_period=r.in_waiting_period,
            waiting_period_ends=r.waiting_period_ends,
            policy_version=self.policy.version,
            explanation=explanation,
            explanation_source=source,
            citations=citations,
            warnings=warnings,
        )
