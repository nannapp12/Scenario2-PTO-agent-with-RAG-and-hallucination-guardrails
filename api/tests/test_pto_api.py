"""GET /pto/{employee_id}: the not-found guardrail, masking, explanation grounding, auth."""
import base64
import json
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.employees import Employee
from app.pto import load_policy
from app.pto_explainer import PtoExplainer
from app.pto_service import PtoService
from app.schemas import Citation

CITATION = Citation(section="V. BENEFITS AND LEAVES OF ABSENCE > B. Vacation Benefits > 2. Accrual",
                    excerpt="3.077 hours biweekly, for a maximum of 10 days per year", score=0.8)


class FakeRepo:
    def __init__(self, employees):
        self.employees, self.lookups = employees, []

    def find(self, employee_id):
        self.lookups.append(employee_id)
        return self.employees.get(employee_id)

    def handbook_sha256(self, doc_id):
        return load_policy().handbook_sha256


class SpyRetriever:
    calls = 0

    def search(self, query, doc_id):
        self.calls += 1
        return [CITATION]


class FakeLLM:
    """Stands in for AzureOpenAI: returns a canned reply and counts calls."""
    def __init__(self, reply):
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.reply = reply

    def _create(self, **_):
        self.calls += 1
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))])


EMP = Employee("E1001", "Avery Morgan", "avery.morgan@example.org", date(2014, 3, 17), "FULL_TIME")


def client_for(repo, retriever=None, llm=None):
    service = PtoService(repo, retriever or SpyRetriever(), PtoExplainer(llm, "gpt-4.1"),
                         load_policy(), "UTC", settings.employee_id_pattern)
    main.app.dependency_overrides[main.get_pto_service] = lambda: service
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _reset():
    yield
    main.app.dependency_overrides.clear()
    settings.pto_auth_mode = "none"


def test_unknown_employee_returns_not_found_without_retrieval_or_llm():
    repo, retriever, llm = FakeRepo({}), SpyRetriever(), FakeLLM("anything")
    r = client_for(repo, retriever, llm).get("/pto/E9999")
    assert r.status_code == 404
    assert r.json() == {"detail": "Employee not found"}
    assert repo.lookups == ["E9999"]
    assert retriever.calls == 0 and llm.calls == 0
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("bad_id", ["bad id!", "x" * 40, "E1001'; DROP TABLE hr.employees;--"])
def test_malformed_id_is_not_found_and_never_queried(bad_id):
    repo = FakeRepo({"E1001": EMP})
    r = client_for(repo).get(f"/pto/{bad_id}")
    assert r.status_code == 404 and r.json() == {"detail": "Employee not found"}
    assert repo.lookups == []


def test_known_employee_masked_and_computed():
    r = client_for(FakeRepo({"E1001": EMP})).get("/pto/E1001")
    assert r.status_code == 200
    d = r.json()
    assert d["name_masked"] == "A*** M***" and d["email_masked"] == "a***@example.org"
    assert "Avery" not in r.text and "avery.morgan" not in r.text
    assert d["available_days"] == 40.0 and d["at_cap"] is True   # 11+ years, capped at 2 x 20 days
    assert d["assumes_no_leave_taken"] is True
    assert d["citations"][0]["section"].endswith("2. Accrual")
    assert d["warnings"] == []
    assert d["explanation_source"] == "template"


def test_grounded_llm_explanation_is_used():
    llm = FakeLLM("Assuming no vacation has been taken, 40.00 days are available; the balance has reached "
                  "the 40.00-day cap, so accrual has stopped.")
    d = client_for(FakeRepo({"E1001": EMP}), llm=llm).get("/pto/E1001").json()
    assert d["explanation_source"] == "llm" and llm.calls == 1


@pytest.mark.parametrize("reply", [
    "Assuming no leave, 42.5 days are available.",         # invented number
    "The employee has a healthy vacation balance.",        # omits the computed balance
])
def test_ungrounded_llm_explanation_falls_back_to_template(reply):
    d = client_for(FakeRepo({"E1001": EMP}), llm=FakeLLM(reply)).get("/pto/E1001").json()
    assert d["explanation_source"] == "template"
    assert "40.00 days" in d["explanation"]
    assert d["available_days"] == 40.0  # numbers never come from the LLM


def test_llm_gets_no_pii():
    seen = {}
    llm = FakeLLM("40.00 days are available.")
    original = llm._create
    llm.chat.completions.create = lambda **kw: (seen.update(kw), original(**kw))[1]
    client_for(FakeRepo({"E1001": EMP}), llm=llm).get("/pto/E1001")
    prompt = json.dumps(seen["messages"])
    assert "Avery" not in prompt and "avery.morgan" not in prompt and "E1001" not in prompt


def _principal(roles):
    claims = [{"typ": "roles", "val": r} for r in roles]
    return base64.b64encode(json.dumps({"claims": claims}).encode()).decode()


def test_auth_modes():
    client = client_for(FakeRepo({"E1001": EMP}))
    settings.pto_auth_mode = "disabled"
    assert client.get("/pto/E1001").status_code == 503
    settings.pto_auth_mode = "easyauth"
    assert client.get("/pto/E1001").status_code == 401
    assert client.get("/pto/E1001", headers={"x-ms-client-principal": _principal(["Other"])}).status_code == 403
    assert client.get("/pto/E1001", headers={"x-ms-client-principal": _principal(["PTO.Read"])}).status_code == 200


def test_auth_runs_before_lookup():
    repo = FakeRepo({})
    settings.pto_auth_mode = "easyauth"
    assert client_for(repo).get("/pto/E9999").status_code == 401
    assert repo.lookups == []
