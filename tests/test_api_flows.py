"""One scripted chat, replayed through the API as Joule would send it, with fake model texts
(PRD section 12 demo flow; FR-SUP-1 to FR-SUP-3, FR-ACT-2, FR-ACT-3, FR-INT-1 to FR-INT-4,
D-3). Every response's SUMMARY is checked; offline (SAP-RPT off, posterior fallback)."""
import re

from fastapi.testclient import TestClient

from agents import detection, supplier_evaluation
from integrations.api.app import create_app

KEY = "test-key-not-a-secret"
H = {"X-API-Key": KEY}
# an amount without "(simulated)", or in the old "Rp1,000" format (one chat format: "Rp 1.0 juta")
UNMARKED_AMOUNT = re.compile(r"Rp\d|Rp [\d,]+\.\d juta(?! \(simulated\)| higher \(simulated\))")


def test_scripted_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("GESIT_API_KEY", KEY)
    monkeypatch.setenv("SAP_RPT_MODE", "none")
    monkeypatch.setenv("DECISION_LOG_PATH", str(tmp_path / "decision_log.jsonl"))
    monkeypatch.delenv("PLAN_LOG_PATH", raising=False)
    monkeypatch.delenv("PO_STORE_PATH", raising=False)
    monkeypatch.setattr(detection, "llm_reasoner",
                        lambda facts: ("Assessment. All figures are simulated.", "fake-model"))
    monkeypatch.setattr(supplier_evaluation, "llm_explainer",
                        lambda facts: (f"{facts['OPENING']} All figures are simulated.",
                                       "fake-model"))
    client = TestClient(create_app(load_env=False, warm_up=False))

    def say(method, url, body=None, status=200, *expected):
        r = client.request(method, url, json=body, headers=H)
        assert r.status_code == status, (url, r.text)
        data = r.json()
        summary = data["SUMMARY"]
        assert not UNMARKED_AMOUNT.findall(summary), summary
        assert summary.endswith(data["NEXT_STEPS"]) and data["NEXT_STEPS"].startswith("You can say")
        for text in expected:
            assert text in summary, (url, text, summary)
        return data

    say("POST", "/reset", None, 200, "empty", '"Run GESIT on scenario S1"')

    s1 = say("POST", "/run", {"scenario": "S1"}, 200,
             "For PO 4500000431 (delay notice), GESIT recommends ordering from supplier 100109",
             "waits for the manager (APR-4500000435)",
             '"Approve purchase order 4500000435"')
    assert "**Why:**" in s1["TABLE_MD"] and s1["PLAN_MD"].startswith("**Outcome:**")
    assert s1["PLAN_MD"].index("**Agents at work**") > s1["PLAN_MD"].index("Evaluation and")

    say("GET", "/approvals", None, 200, "1 PO waits for the manager's approval.",
        "PO 4500000435: supplier 100109")

    say("POST", "/approvals/4500000435/reject", {"as": "MANAGER01", "reason": "too high"}, 422,
        'Reason "too high" has 8 characters, at least 20 are needed, so nothing changed')

    rejected = say("POST", "/approvals/4500000435/reject",
                   {"as": "MANAGER01", "reason": "the price is too high this quarter"}, 200,
                   "is rejected by MANAGER01 and will never be released",
                   'Reason recorded: "the price is too high this quarter".',
                   "The old PO 4500000431 is untouched")
    assert rejected["NEXT_OPTION"]["RANK"] == 2

    say("POST", "/override", {"scenario": "S1", "choose": "100108", "justification": "cheaper"},
        422, 'Justification "cheaper" has 7 characters, at least 20 are needed, so nothing '
             'changed')

    justification = "100108 has a long-term contract with our plant"
    first = say("POST", "/override", {"scenario": "S1", "choose": "100108",
                                      "justification": justification}, 200,
                "Override OVR-4500000431-001 logged: 100108 instead of the recommended 100109.",
                f'Justification recorded: "{justification}".',
                "BUYER01 with 100108 is not flagged.",
                "Other flagged pairs: BUYER03 with 100107")
    assert first["SUPERSEDED"] == []
    first_draft = first["DRAFTS"][0]["EBELN"]

    again = say("POST", "/override", {"scenario": "S1", "choose": "100108",
                                      "justification": justification}, 200,
                "Override OVR-4500000431-002 logged")
    second_draft = again["DRAFTS"][0]["EBELN"]
    assert f"Draft PO {second_draft} supersedes draft PO {first_draft}." in again["SUMMARY"]

    s3 = say("POST", "/run", {"scenario": "S3"}, 200,
             "For PO 4500000433 (capacity shortfall)", "within policy and waits for the buyer's",
             "as the buyer\"")
    s3_draft = s3["DRAFTS"][0]["EBELN"]
    say("POST", f"/approvals/{s3_draft}/approve", {"as": "BUYER"}, 200,
        f"PO {s3_draft} (supplier", "is RELEASED, approved by the buyer BUYER04")

    s2 = say("POST", "/run", {"scenario": "S2"}, 200,
             "the current order is already the best option",
             "Nothing was changed and no PO was drafted.",
             '"Run GESIT on scenario S2"')
    assert s2["DRAFTS"] == []

    integrity = say("GET", "/integrity", None, 200,
                    "40 buyer and supplier pairs tested, 1 flagged for review.",
                    "Overrides logged: 2", "not a finding")
    assert "| OVR-4500000431-002 |" in integrity["TABLE_MD"]

    pending = say("GET", "/approvals", None, 200, "1 PO waits")["PENDING"]
    assert [p["EBELN"] for p in pending] == [second_draft]

    say("POST", "/reset", None, 200, "empty")
    assert client.get("/approvals", headers=H).json()["PENDING"] == []
