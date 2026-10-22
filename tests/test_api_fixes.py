"""Fixes and chat features after the Joule end-to-end test of 22 Oct (A1 to A4, B1 to B5):
quoted user text, one open draft per flagged PO, the override summary, NEXT_STEPS, the
buyer's approval, PLAN_MD, GET /integrity, the explanation in TABLE_MD. Offline, fake
model texts (FR-ACT-2, FR-ACT-3, FR-INT-1 to FR-INT-4, FR-SEL-4, D-3; proposal item 14)."""
import copy
import re

import pytest
from fastapi.testclient import TestClient

from adapters import CsvAdapter, PoStore
from agents import detection, supplier_evaluation
from agents.action import ActionAgent
from integrations.api import responses as views
from integrations.api.app import create_app

KEY = "test-key-not-a-secret"
H = {"X-API-Key": KEY}
REASON = "the price is too high this quarter"
JUSTIFICATION = "100108 has a long-term contract with our plant"
UNMARKED_AMOUNT = re.compile(r"Rp[\d,]*\d(?![\d,]| \(simulated\)| higher \(simulated\)"
                             r"| higher than the recommended \d{6} \(simulated\))")
NEXT = re.compile(r'You can say "[^"]+"( or "[^"]+")*\.$')


def fake_reasoner(facts):
    return f"{facts['OPEN_PO_COUNT']} open POs checked. All figures are simulated.", "fake-model"


def fake_explainer(facts):
    return f"{facts['OPENING']} All figures are simulated.", "fake-model"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GESIT_API_KEY", KEY)
    monkeypatch.setenv("SAP_RPT_MODE", "none")
    monkeypatch.setenv("DECISION_LOG_PATH", str(tmp_path / "decision_log.jsonl"))
    monkeypatch.delenv("PLAN_LOG_PATH", raising=False)
    monkeypatch.delenv("PO_STORE_PATH", raising=False)
    monkeypatch.setattr(detection, "llm_reasoner", fake_reasoner)
    monkeypatch.setattr(supplier_evaluation, "llm_explainer", fake_explainer)
    return TestClient(create_app(load_env=False, warm_up=False))


def call(client, method, url, body=None, status=200):
    response = client.request(method, url, json=body, headers=H)
    assert response.status_code == status, response.text
    data = response.json()
    summary = data["SUMMARY"]
    assert not UNMARKED_AMOUNT.findall(summary), summary
    assert NEXT.search(summary), summary                       # B1: every SUMMARY ends so
    assert summary.endswith(data["NEXT_STEPS"])
    assert chr(0x2014) not in response.text and chr(0x2013) not in response.text
    return data


def run(client, scenario, llm=False):
    return call(client, "POST", "/run", {"scenario": scenario, "llm": llm})


def override(client, choose="100108", justification=JUSTIFICATION, status=200):
    return call(client, "POST", "/override",
                {"scenario": "S1", "choose": choose, "justification": justification}, status)


# ------------------------------------------------------------------- A1 quoting
def test_short_reject_reason_is_quoted_exactly(client):
    run(client, "S1")
    data = call(client, "POST", "/approvals/4500000435/reject",
                {"as": "MANAGER01", "reason": "too high"}, 422)
    assert data["SUMMARY"].startswith('Reason "too high" has 8 characters, at least 20 are '
                                      "needed.")
    assert data["REASON_CHARS"] == 8
    assert 'Reject purchase order 4500000435. Reason: ...' in data["NEXT_STEPS"]


def test_reject_quotes_the_reason_as_received(client):
    run(client, "S1")
    received = "  The price is TOO high, ask for option 2  "
    data = call(client, "POST", "/approvals/4500000435/reject",
                {"as": "MANAGER01", "reason": received})
    assert f'Reason recorded: "{received}".' in data["SUMMARY"]


def test_short_justification_is_quoted_exactly(client):
    data = override(client, justification="cheaper", status=422)
    assert data["SUMMARY"].startswith('Justification "cheaper" has 7 characters, at least 20 '
                                      "are needed. No draft PO was created.")


def test_override_quotes_the_justification(client):
    data = override(client)
    assert f'Justification recorded: "{JUSTIFICATION}".' in data["SUMMARY"]
    assert data["JUSTIFICATION"] == JUSTIFICATION


# ------------------------------------------------------- A2 one open draft per PO
def test_second_override_supersedes_the_first_draft(client):
    first = override(client)["DRAFTS"][0]["EBELN"]
    data = override(client)
    second = data["DRAFTS"][0]["EBELN"]
    assert f"Draft PO {second} supersedes draft PO {first}." in data["SUMMARY"]
    assert data["SUPERSEDED"] == [{"EBELN": first, "SUPERSEDED_BY": [second]}]
    pending = call(client, "GET", "/approvals")["PENDING"]
    assert [p["EBELN"] for p in pending] == [second]
    for verb, body in (("approve", {"as": "MANAGER01"}),
                       ("reject", {"as": "MANAGER01", "reason": REASON})):
        msg = call(client, "POST", f"/approvals/{first}/{verb}", body, 409)["SUMMARY"]
        assert f"PO {first} is SUPERSEDED by draft PO {second} and can never be released" in msg


def test_run_again_supersedes_and_rejected_drafts_are_left_alone(client):
    run(client, "S1")                                        # 4500000435 pending
    call(client, "POST", "/approvals/4500000435/reject", {"as": "MANAGER01", "reason": REASON})
    data = run(client, "S1")                                 # 4500000436, 435 was rejected
    assert data["SUPERSEDED"] == [] and "supersedes" not in data["SUMMARY"]
    data = run(client, "S1")                                 # 4500000437 supersedes 436
    assert "Draft PO 4500000437 supersedes draft PO 4500000436." in data["SUMMARY"]


def test_split_parts_never_supersede_each_other(tmp_path):
    adapter = CsvAdapter()
    store = PoStore(adapter, path=tmp_path / "po_store.json")

    def split_case():
        from tests.test_moq import evaluation, s3_split_row
        ev = evaluation(adapter, "S3")
        case, row = s3_split_row(ev)
        case = copy.deepcopy(case)
        case["SCORING"]["CANDIDATES"] = [row]
        return {**ev, "CASES": [case]}

    first = ActionAgent(adapter=adapter, store=store).act(split_case())
    assert [o["STATUS"] for o in first["CASES"][0]["ORDERS"]] == ["DRAFT", "DRAFT"]
    assert first["CASES"][0]["SUPERSEDED"] == []
    second = ActionAgent(adapter=adapter, store=store).act(split_case())
    new = [o["EBELN"] for o in second["CASES"][0]["ORDERS"]]
    old = [o["EBELN"] for o in first["CASES"][0]["ORDERS"]]
    assert second["CASES"][0]["SUPERSEDED"] == [{"EBELN": e, "SUPERSEDED_BY": new} for e in old]
    statuses = {po["EBELN"]: po["STATUS"] for po in store.all()}
    assert [statuses[e] for e in old] == ["SUPERSEDED", "SUPERSEDED"]
    assert [statuses[e] for e in new] == ["DRAFT", "DRAFT"]
    event = store.get(old[0])["HISTORY"][-1]
    assert event["TOOL"] == "supersede" and event["SUPERSEDED_BY"] == new


# ------------------------------------------------------------ A3 override summary
def test_override_summary_names_the_gap_the_flags_and_stays_neutral(client):
    data = override(client)
    text = data["SUMMARY"]
    assert re.search(r"100108 has an expected cost Rp[\d,]+ higher than the recommended 100109 "
                     r"\(simulated\)\.", text)
    assert ("Flagged for review: BUYER03 with 100107, 27 of 113 POs, 7.7x the expected rate "
            "(simulated).") in text
    assert "not a finding" in text
    for word in ("fraud", "suspicious", "corrupt", "guilty", "violation"):
        assert word not in text.lower()


# ------------------------------------------------------ A4 no repeats in `all`
def test_run_all_repeats_no_sentence_and_no_table(client):
    data = run(client, "all", llm=True)
    summary = re.sub(r'"[^"]*"', '"Q"', data["SUMMARY"])
    parts = [s for s in re.split(r"(?<=[.!?])\s+", summary) if s]
    assert len(parts) == len(set(parts))
    md = data["TABLE_MD"]
    headings = [l for l in md.splitlines() if l.startswith("### PO ")]
    assert len(headings) == len(set(headings)) == 3
    tables = md.split("### PO ")[1:]
    bodies = [t.split("\n", 1)[1] for t in tables]
    assert len(bodies) == len(set(bodies))
    assert len({c["TABLE_MD"] for c in data["CASES"]}) == 3


# ------------------------------------------------------------------ B1 next steps
def test_next_steps_for_manager_buyer_and_keep(client):
    assert run(client, "S1")["NEXT_STEPS"] == (
        'You can say "Approve purchase order 4500000435" or "Reject purchase order 4500000435. '
        'Reason: ...".')
    assert run(client, "S3")["NEXT_STEPS"] == (
        'You can say "Approve purchase order 4500000436 as the buyer".')
    s2 = run(client, "S2")["NEXT_STEPS"]
    assert s2.startswith('You can say "Run GESIT on scenario S2" or "Override scenario S2 with '
                         'supplier ')


def test_every_endpoint_has_next_steps(client):
    call(client, "POST", "/reset")
    call(client, "GET", "/approvals")
    run(client, "S1")
    call(client, "GET", "/approvals")
    call(client, "GET", "/integrity")
    call(client, "POST", "/approvals/4500009999/approve", {"as": "MANAGER01"}, 404)
    call(client, "POST", "/approvals/4500000435/approve", {"as": "MANAGER01"})
    call(client, "POST", "/approvals/4500000435/approve", {"as": "MANAGER01"}, 409)


# ------------------------------------------------------------- B2 buyer approval
def test_buyer_approves_a_draft_within_policy(client):
    draft = run(client, "S3")["DRAFTS"][0]
    data = call(client, "POST", f"/approvals/{draft['EBELN']}/approve", {"as": "BUYER"})
    assert data["RELEASED_BY"] == draft["BUYER"] == "BUYER04"
    assert "the buyer's one-click, within policy" in data["SUMMARY"]


def test_buyer_above_the_threshold_gets_a_clear_403(client):
    run(client, "S1")
    data = call(client, "POST", "/approvals/4500000435/approve", {"as": "buyer"}, 403)
    assert data["SUMMARY"].startswith("Approve by BUYER01 (buyer) denied by POL-1: only role "
                                      "manager decides a PO above the approval threshold.")
    assert 'Approve purchase order 4500000435"' in data["NEXT_STEPS"]
    assert call(client, "GET", "/approvals")["PENDING"][0]["EBELN"] == "4500000435"


# --------------------------------------------------------------------- B3 PLAN_MD
def test_plan_md_shows_the_agents_at_work(client):
    data = run(client, "S1", llm=True)
    md = data["PLAN_MD"]
    assert len(md.encode()) < 1_000
    assert md.startswith("**Agents at work** (supervisor plan PLAN-001, Strands Graph")
    for line in ("1. Disruption Detection: DONE in", "2. Supplier Evaluation: DONE in",
                 "3. Action: DONE in", "Evaluation and Learning: offline"):
        assert line in md
    assert len(run(client, "all", llm=True)["PLAN_MD"].encode()) < 1_000


def test_plan_md_names_sap_rpt_called_or_cached():
    plan = {"PLAN_ID": "PLAN-009", "TOTAL_SECONDS": 4.3, "OUTCOME": None, "STEPS": [
        {"NO": 1, "AGENT": "Disruption Detection", "STATUS": "DONE", "SECONDS": 0.01,
         "SAP_RPT": "SAP-RPT: cached", "CALLS": []},
        {"NO": 2, "AGENT": "Supplier Evaluation", "STATUS": "DONE", "SECONDS": 4.2,
         "SAP_RPT": "SAP-RPT: called", "CALLS": [{"KIND": "GEN_AI_HUB"}, {"KIND": "GEN_AI_HUB"}]}]}
    md = views.plan_md(plan)
    assert "1. Disruption Detection: DONE in 0.0 s, SAP-RPT: cached" in md
    assert "2. Supplier Evaluation: DONE in 4.2 s, SAP-RPT: called, 2 model texts" in md


# ------------------------------------------------------------------ B4 integrity
def test_integrity_endpoint(client):
    override(client)
    data = call(client, "GET", "/integrity")
    assert "40 buyer and supplier pairs tested, 1 flagged for review." in data["SUMMARY"]
    assert "BUYER03 with 100107" in data["SUMMARY"] and "not a finding" in data["SUMMARY"]
    assert "OVR-4500000431-001" in data["SUMMARY"]
    md = data["TABLE_MD"]
    assert "| BUYER03 | 100107 | 27 of 113 |" in md and "| OVR-4500000431-001 | BUYER01 |" in md
    assert md.endswith("All numbers simulated.") and len(md.encode()) < 3_000
    assert data["LATEST_OVERRIDES"][0]["JUSTIFICATION"] == JUSTIFICATION


# --------------------------------------------------------- B5 explanation in TABLE_MD
def test_explanation_under_each_table(client):
    data = run(client, "S1", llm=True)
    case = data["CASES"][0]
    assert f"**Why:** {case['EXPLANATION']}" in case["TABLE_MD"]
    assert f"**Why:** {case['EXPLANATION']}" in data["TABLE_MD"]


def test_rows_are_shortened_before_the_explanation_is_dropped(client, monkeypatch):
    long = "Recommend the top option. " + "This option is the best trade-off. " * 18 + \
        "All figures are simulated."
    monkeypatch.setattr(supplier_evaluation, "llm_explainer", lambda facts: (long, "fake-model"))
    data = run(client, "all", llm=True)
    md = data["TABLE_MD"]
    assert len(md.encode()) < views.MD_MAX_BYTES
    assert md.count("**Why:**") >= 1
    full = [c["TABLE_MD"].split("\n\n")[0].count("\n| ") for c in data["CASES"]]
    shown = [t.split("\n\n")[1].count("\n| ") for t in md.split("### PO ")[1:]]
    assert sum(shown) < sum(full)   # rows were cut first
