"""Plain-language explanation of a computed PTO balance, with a grounding guardrail.

The LLM gets the already-computed facts (no name or email) and the retrieved handbook
excerpts. Its reply is accepted only if every number in it appears in those facts or
excerpts and it states the computed balance; otherwise, or on any error, a
deterministic template is used. The numbers in the API response always come from
pto.calculate, never from this text.
"""
import json
import logging
import re
from decimal import Decimal, InvalidOperation

from openai import AzureOpenAI

from .prompts import PTO_EXPLAINER_INSTRUCTIONS
from .schemas import Citation

log = logging.getLogger(__name__)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _norm(s: str) -> str | None:
    try:
        return format(Decimal(s).normalize(), "f")
    except InvalidOperation:
        return None


def allowed_numbers(facts: dict, citations: list[Citation]) -> set[str]:
    text = json.dumps(facts) + " " + " ".join(f"{c.section} {c.excerpt}" for c in citations)
    return {n for n in (_norm(m) for m in _NUMBER.findall(text)) if n is not None}


def is_grounded(text: str, facts: dict, citations: list[Citation]) -> bool:
    """Every number must come from the facts/excerpts, and the headline balance must be stated."""
    found = {_norm(m) for m in _NUMBER.findall(text)}
    headline = facts["accrued_days"] if facts["in_waiting_period"] else facts["available_days"]
    return found <= allowed_numbers(facts, citations) and _norm(headline) in found


def template(facts: dict) -> str:
    if facts["not_started"]:
        return f"This employee's start date is after {facts['as_of']}, so no vacation has accrued yet."
    if not facts["eligible"]:
        return (f"Under the handbook's vacation policy only regular full-time employees accrue vacation. "
                f"This employee is classified as {facts['employment_type']}, so 0 days are available.")
    parts = [
        f"As of {facts['as_of']}, this employee has completed {facts['biweekly_periods_completed']} biweekly "
        f"accrual periods and currently accrues {facts['hours_per_biweekly_period']} hours per period "
        f"(up to {facts['annual_allotment_days']} days per year)."
    ]
    if facts["in_waiting_period"]:
        parts.append(
            f"{facts['accrued_days']} days ({facts['accrued_hours']} hours) have accrued, but vacation can't be "
            f"used until the {facts['waiting_period_days']}-day waiting period ends on {facts['waiting_period_ends']}."
        )
    else:
        parts.append(f"Assuming no vacation has been taken, {facts['available_days']} days "
                     f"({facts['available_hours']} hours) are available.")
    if facts["at_cap"]:
        parts.append(f"The balance has reached the {facts['balance_cap_days']}-day cap, so accrual has stopped.")
    return " ".join(parts)


class PtoExplainer:
    def __init__(self, client: AzureOpenAI | None, deployment: str):
        self.client, self.deployment = client, deployment

    def explain(self, facts: dict, citations: list[Citation]) -> tuple[str, str]:
        """Returns (text, source) where source is "llm" or "template"."""
        if self.client is None or facts["not_started"] or not facts["eligible"]:
            return template(facts), "template"
        excerpts = "\n\n".join(f"[{c.section}]\n{c.excerpt}" for c in citations) or "(none)"
        try:
            resp = self.client.chat.completions.create(
                model=self.deployment,
                temperature=0,
                max_tokens=220,
                timeout=15,
                messages=[
                    {"role": "system", "content": PTO_EXPLAINER_INSTRUCTIONS},
                    {"role": "user", "content": f"FACTS:\n{json.dumps(facts, indent=2)}\n\nHANDBOOK EXCERPTS:\n{excerpts}"},
                ],
            )
            text = (resp.choices[0].message.content or "").strip()
        except Exception:
            log.warning("PTO explanation call failed; using template", exc_info=True)
            return template(facts), "template"
        if not text or not is_grounded(text, facts, citations):
            log.warning("PTO explanation contained ungrounded numbers; using template")
            return template(facts), "template"
        return text, "llm"
