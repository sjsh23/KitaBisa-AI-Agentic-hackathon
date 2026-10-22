"""HTTP API for Joule (integrations/api): every endpoint offline with FastAPI's TestClient
(FR-SUP-1 to FR-SUP-3, FR-ACT-2, FR-INT-1, POL-1, POL-4, D-3, NFR-4).

SAP-RPT is off (SAP_RPT_MODE=none, posterior fallback) and no model text is asked for
(llm false). The decision log, plan log and PO store live in a temp folder.
"""
import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from agents import supervisor as sup
from integrations.api.app import create_app
from tools.integrity_tools import decision_log_path, read_overrides

KEY = "test-key-not-a-secret"
H = {"X-API-Key": KEY}
REASON = "Price too high for this quarter, ask buyer for option 2"
JUSTIFICATION = "100109 has only 4 receipts, we prefer a long track record"
MAX_BYTES = 20_000
UNMARKED_AMOUNT = re.compile(r"Rp[\d,]*\d(?![\d,]| \(simulated\))")   # an amount without the label


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GESIT_API_KEY", KEY)
    monkeypatch.setenv("SAP_RPT_MODE", "none")
    monkeypatch.setenv("DECISION_LOG_PATH", str(tmp_path / "decision_log.jsonl"))
    monkeypatch.delenv("PLAN_LOG_PATH", raising=False)
    monkeypatch.delenv("PO_STORE_PATH", raising=False)
    return TestClient(create_app(load_env=False))


def call(client, method, url, body=None, status=200, headers=H):
    """One request; checks the status, the size limit and SUMMARY on every response."""
    response = client.request(method, url, json=body, headers=headers)
    assert response.status_code == status, response.text
    assert len(response.content) < MAX_BYTES
    data = response.json()
    if url != "/health":
        assert data["SUMMARY"]
        assert not UNMARKED_AMOUNT.findall(data["SUMMARY"]), data["SUMMARY"]
        assert chr(0x2014) not in response.text and chr(0x2013) not in response.text
    return data


def sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def pending_s1(client):
    call(client, "POST", "/run", {"scenario": "S1", "llm": False})
    [po] = call(client, "GET", "/approvals")["PENDING"]
    return po["EBELN"]


# ------------------------------------------------------------ key and health
def test_app_refuses_to_start_without_a_key(monkeypatch):
    monkeypatch.delenv("GESIT_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GESIT_API_KEY"):
        create_app(load_env=False)


def test_health_needs_no_key(client):
    assert call(client, "GET", "/health", headers={}) == {"status": "ok"}


@pytest.mark.parametrize("method, url, body", [
    ("POST", "/run", {"scenario": "S1", "llm": False}),
    ("POST", "/run/start", {"scenario": "S1", "llm": False}),
    ("GET", "/run/abc", None),
    ("GET", "/approvals", None),
    ("POST", "/approvals/4500000435/approve", {"as": "MANAGER01"}),
    ("POST", "/approvals/4500000435/reject", {"as": "MANAGER01", "reason": REASON}),
    ("POST", "/override", {"scenario": "S1", "choose": "100108", "justification": JUSTIFICATION}),
    ("POST", "/reset", None),
])
def test_401_without_or_with_a_wrong_key(client, method, url, body):
    assert "X-API-Key" in call(client, method, url, body, 401, headers={})["SUMMARY"]
    call(client, method, url, body, 401, headers={"X-API-Key": "wrong"})


# ----------------------------------------------------------------------- run
def test_run_s1(client):
    data = call(client, "POST", "/run", {"scenario": "S1", "llm": False})
    assert data["DATA_NOTE"] == "simulated data" and data["SCENARIO"] == "S1"
    steps = data["PLAN"]["STEPS"]
    assert [s["AGENT"] for s in steps] == ["Disruption Detection", "Supplier Evaluation", "Action"]
    assert all(s["STATUS"] == "DONE" and isinstance(s["SECONDS"], float) for s in steps)
    [case] = data["CASES"]
    assert case["EBELN"] == "4500000431" and case["RECOMMENDED"] == "100109"
    table = case["TABLE"]
    assert len(table) == 4 and table[-1]["OPTION"] == "KEEP_CURRENT_PO"   # top 3 plus keep
    assert {"LIFNR", "EXPECTED_COST", "NETPR", "LEAD_DAYS", "MARKS"} <= set(table[0])
    assert "fastest" in table[0]["MARKS"]
    [draft] = data["DRAFTS"]
    assert (draft["LIFNR"], draft["MENGE"], draft["NETWR"], draft["NEXT"]) == (
        "100109", 10_000.0, 175_500_000, "manager approval")
    text = data["SUMMARY"]
    assert 2 <= len(sentences(text)) <= 4
    assert "recommends ordering from supplier 100109" in text and "cheapest" in text
    assert "waits for the manager" in text and "Rp175,500,000 (simulated)" in text


def test_run_s2_keep_drafts_nothing(client):
    data = call(client, "POST", "/run", {"scenario": "s2", "llm": False})
    assert data["DRAFTS"] == [] and data["PLAN"]["OUTCOME"]["CODE"] == "KEEP_EVERYWHERE"
    assert "keeping PO 4500000432" in data["SUMMARY"] and "Nothing was drafted" in data["SUMMARY"]


def test_run_s3_shows_the_moq_mark(client):
    data = call(client, "POST", "/run", {"scenario": "S3", "llm": False})
    marks = [m for row in data["CASES"][0]["TABLE"] for m in row["MARKS"]]
    assert any("MOQ +300 kg" in m for m in marks)
    assert data["DRAFTS"][0]["NEXT"] == "buyer approval"


def test_run_all_and_scan(client):
    data = call(client, "POST", "/run", {"scenario": "all", "llm": False})
    assert len(data["CASES"]) == 3 and 2 <= len(sentences(data["SUMMARY"])) <= 4
    scan = call(client, "POST", "/run", {"scenario": "scan", "llm": False})
    assert scan["CASES"] == [] and "flagged none" in scan["SUMMARY"]


def test_run_rejects_an_unknown_scenario(client):
    assert "scenario" in call(client, "POST", "/run", {"scenario": "S9"}, 422)["SUMMARY"]


def test_run_start_then_result(client):
    started = call(client, "POST", "/run/start", {"scenario": "S1", "llm": False})
    job = started["job_id"]
    deadline = time.time() + 60
    while True:
        data = call(client, "GET", f"/run/{job}")
        if data["status"] != "running" or time.time() > deadline:
            break
        time.sleep(0.2)
    assert data["status"] == "done" and data["DRAFTS"][0]["NEXT"] == "manager approval"
    call(client, "GET", "/run/nope", status=404)


def test_run_writes_the_plan_log(client):
    call(client, "POST", "/run", {"scenario": "S1", "llm": False})
    [plan] = sup.read_plans()
    assert plan["PLAN_ID"] == "PLAN-001" and all(s["SECONDS"] is not None for s in plan["STEPS"])


# ----------------------------------------------------------------- approvals
def test_approvals_list(client):
    assert call(client, "GET", "/approvals")["PENDING"] == []
    ebeln = pending_s1(client)
    data = call(client, "GET", "/approvals")
    assert data["PENDING"][0]["APPROVAL_REQUEST_ID"] == f"APR-{ebeln}"
    assert "1 PO wait" in data["SUMMARY"]


def test_policy_deny_is_403(client):
    ebeln = pending_s1(client)
    data = call(client, "POST", f"/approvals/{ebeln}/approve", {"as": "BUYER01", "role": "buyer"},
                403)
    assert data["POLICY_DECISION"]["POLICY"] == "POL-1"
    data = call(client, "POST", f"/approvals/{ebeln}/approve",
                {"as": "gesit-action-agent", "role": "agent"}, 403)
    assert data["POLICY_DECISION"]["POLICY"] == "POL-4"
    data = call(client, "POST", f"/approvals/{ebeln}/reject", {"as": "BUYER01", "role": "buyer",
                                                              "reason": REASON}, 403)
    assert data["POLICY_DECISION"]["POLICY"] == "POL-1"
    assert call(client, "GET", "/approvals")["PENDING"][0]["EBELN"] == ebeln   # unchanged


def test_approve_then_double_approve_is_409(client):
    ebeln = pending_s1(client)
    data = call(client, "POST", f"/approvals/{ebeln}/approve", {"as": "MANAGER01"})
    assert data["STATUS"] == "RELEASED" and data["RELEASED_BY"] == "MANAGER01"
    assert "approved and released by MANAGER01" in data["SUMMARY"]
    assert "is RELEASED" in call(client, "POST", f"/approvals/{ebeln}/approve",
                                 {"as": "MANAGER01"}, 409)["SUMMARY"]
    call(client, "POST", f"/approvals/{ebeln}/reject", {"as": "MANAGER01", "reason": REASON}, 409)


def test_reject_returns_the_next_option(client):
    ebeln = pending_s1(client)
    data = call(client, "POST", f"/approvals/{ebeln}/reject", {"as": "MANAGER01", "reason": REASON})
    assert data["STATUS"] == "REJECTED" and data["REPLACES_EBELN"] == "4500000431"
    assert data["NEXT_OPTION"]["RANK"] == 2 and data["NEXT_OPTION"]["LABEL"]
    assert "Next option for the buyer, not drafted: rank 2" in data["SUMMARY"]
    call(client, "POST", f"/approvals/{ebeln}/approve", {"as": "MANAGER01"}, 409)
    call(client, "POST", f"/approvals/{ebeln}/reject", {"as": "MANAGER01", "reason": REASON}, 409)


def test_short_reject_reason_is_422(client):
    ebeln = pending_s1(client)
    data = call(client, "POST", f"/approvals/{ebeln}/reject", {"as": "MANAGER01", "reason": "no"},
                422)
    assert "at least 20 characters" in data["SUMMARY"]


def test_unknown_po_is_404(client):
    call(client, "POST", "/approvals/4500009999/approve", {"as": "MANAGER01"}, 404)


# ------------------------------------------------------------------ override
def test_short_justification_is_422(client):
    data = call(client, "POST", "/override",
                {"scenario": "S1", "choose": "100108", "justification": "cheaper"}, 422)
    assert "at least 20 characters" in data["REASON"] and data["CHOSEN"] == "100108"
    assert read_overrides() == [] and call(client, "GET", "/approvals")["PENDING"] == []


def test_override_logs_drafts_and_returns_flags(client):
    data = call(client, "POST", "/override",
                {"scenario": "S1", "choose": "100108", "justification": JUSTIFICATION})
    assert data["OVERRIDE_ID"] == "OVR-4500000431-001" and data["RECOMMENDED"] == "100109"
    assert data["DRAFTS"][0]["LIFNR"] == "100108"
    flags = data["INTEGRITY_FLAGS"]
    assert flags["PAIRS_TESTED"] == 40 and flags["PAIRS_FLAGGED"] == 1
    assert flags["OVERRIDES_TOTAL"] == 1
    assert "Override OVR-4500000431-001 logged" in data["SUMMARY"]


def test_choosing_number_one_logs_nothing(client):
    data = call(client, "POST", "/override", {"scenario": "S1", "choose": "100109"})
    assert data["OVERRIDE_ID"] is None and "no justification was needed" in data["SUMMARY"]


# --------------------------------------------------------------------- reset
def test_reset_then_approvals_empty(client):
    call(client, "POST", "/override",
         {"scenario": "S1", "choose": "100108", "justification": JUSTIFICATION})
    assert call(client, "GET", "/approvals")["PENDING"]
    data = call(client, "POST", "/reset")
    assert data["status"] == "ok"
    assert call(client, "GET", "/approvals")["PENDING"] == []
    assert sup.read_plans() == [] and not decision_log_path().exists()
    # numbers start again from the scenario start state
    call(client, "POST", "/run", {"scenario": "S1", "llm": False})
    assert call(client, "GET", "/approvals")["PENDING"][0]["EBELN"] == "4500000435"


def test_every_run_response_is_small(client):
    for scenario in ("S1", "S2", "S3", "all", "scan"):
        body = json.dumps(call(client, "POST", "/run", {"scenario": scenario, "llm": False}))
        assert len(body.encode()) < MAX_BYTES
