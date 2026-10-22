"""Output fixes and demo improvements after the Joule test of 22 Oct evening (A1 to A6, B1 to
B4): the explanation's "also", whole percents, PLAN_MD lines, 4-column tables, the overriding
buyer's own pair first, RELEASED wording, "Which scenario?", GET /status, GET /evaluation,
the override phrase after a reject (FR-SEL-4, FR-ACT-2, FR-ACT-3, FR-INT-4, FR-EVA-4, D-1,
D-3). Offline, fake model texts."""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agents import detection, grounding, supplier_evaluation
from agents.supplier_evaluation import opening_sentence, pct_text
from integrations.api import responses as views
from integrations.api import app as api_app
from integrations.api.app import create_app

KEY = "test-key-not-a-secret"
H = {"X-API-Key": KEY}
REASON = "the price is too high this quarter"
JUSTIFICATION = "100108 has a long-term contract with our plant"
API_DIR = Path(api_app.__file__).resolve().parent


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GESIT_API_KEY", KEY)
    monkeypatch.setenv("SAP_RPT_MODE", "none")
    monkeypatch.setenv("DECISION_LOG_PATH", str(tmp_path / "decision_log.jsonl"))
    monkeypatch.delenv("PLAN_LOG_PATH", raising=False)
    monkeypatch.delenv("PO_STORE_PATH", raising=False)
    monkeypatch.setattr(detection, "llm_reasoner", lambda f: ("ok. All figures are simulated.",
                                                              "fake-model"))
    monkeypatch.setattr(supplier_evaluation, "llm_explainer",
                        lambda f: (f"{f['OPENING']} All figures are simulated.", "fake-model"))
    return TestClient(create_app(load_env=False, warm_up=False))


def call(client, method, url, body=None, status=200):
    r = client.request(method, url, json=body, headers=H)
    assert r.status_code == status, r.text
    data = r.json()
    assert data["SUMMARY"].endswith(data["NEXT_STEPS"])
    assert chr(0x2014) not in r.text and chr(0x2013) not in r.text
    return data


# ------------------------------------------------------------- A1, A6 explanation
def test_opening_says_also_about_number_one():
    best = {"LIFNR": "100109", "EXPECTED_COST": 178_921_875, "OPTION": "NEW_PO"}
    assert opening_sentence(best, also=["fastest"]) == (
        "Recommend supplier 100109, also the fastest option, at an expected cost of "
        "Rp178,921,875.")
    assert opening_sentence(best, also=["cheapest", "fastest"]).startswith(
        "Recommend supplier 100109, also the cheapest and fastest option, at")
    assert opening_sentence(best) == "Recommend supplier 100109 at an expected cost of Rp178,921,875."


def test_facts_put_the_also_in_the_opening(client):
    data = call(client, "POST", "/run", {"scenario": "S1", "llm": True})
    assert data["CASES"][0]["EXPLANATION"].startswith(
        "Recommend supplier 100109, also the fastest option, at an expected cost of")
    assert "It is also" not in supplier_evaluation.SYSTEM_PROMPT.split("Never write")[0]


def test_check_fails_on_it_is_also_after_another_supplier():
    text = ("Recommend supplier 100109 at an expected cost of Rp1. The cheapest option, supplier "
            "100105, would cost Rp2 more. It is also the fastest option. All figures are "
            "simulated.")
    check = grounding.check_explanation(text, {"A": "Rp1 Rp2 100109 100105"}, 120)
    assert check["AMBIGUOUS_ALSO"] == ["It is also the fastest option."]
    assert check["PASSED"] is False


@pytest.mark.parametrize("pct, text", [(564.13, "564%"), (42.38, "42%"), (1.5, "2%"),
                                       (0.4, "less than 1%"), (1234.6, "1,235%")])
def test_whole_percent(pct, text):
    assert pct_text(pct) == text


def test_explanation_facts_have_no_decimal_percent(client):
    from tests.test_moq import evaluation
    from adapters import CsvAdapter
    ev = evaluation(CsvAdapter(), "S1")
    facts = supplier_evaluation.facts_for_llm(ev["CASES"][0], ev)
    assert not re.search(r"\d+\.\d+%", json.dumps(facts))


# ------------------------------------------------------------------- A3 width
def test_tables_have_four_columns(client):
    data = call(client, "POST", "/run", {"scenario": "all", "llm": False})
    for line in data["TABLE_MD"].splitlines():
        if line.startswith("|"):
            assert line.count("|") == 5, line
    s1 = data["CASES"][0]["TABLE_MD"]
    assert re.search(r"\| 1 \| 100109 \| [\d.,]+ \| Rp17,550/kg, 3 d, fastest \|", s1)
    assert "cheapest" in s1 and "keep PO 4500000431 (100101)" in s1


# ---------------------------------------------------------- A4 own pair first
def test_override_names_the_buyers_own_pair_first(client):
    data = call(client, "POST", "/override",
                {"scenario": "S1", "choose": "100108", "justification": JUSTIFICATION})
    text = data["SUMMARY"]
    assert "BUYER01 with 100108 is not flagged. Other flagged pairs: BUYER03 with 100107" in text
    integrity = call(client, "GET", "/integrity")["SUMMARY"]
    assert integrity.startswith("Overrides logged: 1; the latest, OVR-4500000431-001, BUYER01 "
                                "chose 100108 instead of 100109.")
    assert "BUYER01 with 100108 is not flagged. Other flagged pairs: BUYER03" in integrity


def test_flags_sentences_when_the_own_pair_is_flagged():
    flags = {"PAIRS_TESTED": 40, "PAIRS_FLAGGED": 1, "FLAGS": [
        {"BUYER": "BUYER03", "SUPPLIER": "100107", "K": 27, "N_B": 113, "RATIO": 7.7}]}
    out = views.flags_sentences(flags, "BUYER03", "100107")
    assert out[1] == ("BUYER03 with 100107 is flagged for review: BUYER03 with 100107, 27 of 113 "
                      "POs, 7.7x the expected rate (simulated).")
    assert not any(s.startswith("Other flagged pairs") for s in out)


# ------------------------------------------------------------ A5 RELEASED wording
def test_released_for_manager_and_buyer(client):
    call(client, "POST", "/run", {"scenario": "S1", "llm": False})
    m = call(client, "POST", "/approvals/4500000435/approve", {"as": "MANAGER01"})
    s3 = call(client, "POST", "/run", {"scenario": "S3", "llm": False})["DRAFTS"][0]["EBELN"]
    b = call(client, "POST", f"/approvals/{s3}/approve", {"as": "BUYER"})
    assert m["STATUS"] == b["STATUS"] == "RELEASED"
    assert "is RELEASED, approved by the procurement manager MANAGER01" in m["SUMMARY"]
    assert "is RELEASED, approved by the buyer BUYER04, one-click within policy" in b["SUMMARY"]
    assert "APPROVED" not in m["SUMMARY"] + b["SUMMARY"]


# ---------------------------------------------------------- B1 which scenario
@pytest.mark.parametrize("body", [{}, {"scenario": ""}, {"scenario": "S9"}, {"llm": True}])
def test_run_without_a_valid_scenario_asks(client, body):
    data = call(client, "POST", "/run", body, 422)
    assert data["SUMMARY"].startswith("Which scenario: S1, S2, S3 or all?")
    assert data["NEXT_STEPS"] == ('You can say "Run GESIT on scenario S1" or "Run GESIT on all '
                                  'scenarios".')
    call(client, "POST", "/run/start", body, 422)


# ------------------------------------------------------------------ B2 status
def test_status_ready_off_and_warming(client, monkeypatch):
    data = call(client, "GET", "/status")
    assert data["SUMMARY"].startswith("GESIT is up: warm-up off, 0 open drafts.")
    assert {"WARMUP", "RPT_CACHE_SIZE", "OPEN_DRAFTS", "STARTED_AT", "UPTIME_SECONDS"} <= set(data)
    call(client, "POST", "/run", {"scenario": "S1", "llm": False})
    assert call(client, "GET", "/status")["OPEN_DRAFTS"] == 1
    assert views.status_summary({"STATUS": "done", "STEPS": [{"STEP": "x", "RESULT": "ok"}]},
                                0) == "GESIT is ready: warm-up done, 0 open drafts."
    assert views.status_summary({"STATUS": "running", "STEPS": [{"STEP": "x", "RESULT": "ok"}]},
                                2).startswith(
        "GESIT is warming up (1 of 3 steps done), 2 open drafts.")
    failed = views.status_summary({"STATUS": "done", "STEPS": [
        {"STEP": "tiny Gen AI Hub chat", "RESULT": "failed: LlmUnavailable: no key"}]}, 0)
    assert failed.startswith("GESIT is up, but the warm-up step tiny Gen AI Hub chat failed")


# -------------------------------------------------------------- B3 evaluation
def test_evaluation_from_the_summary_file(client):
    data = call(client, "GET", "/evaluation")
    text = data["SUMMARY"]
    ev = data["EVALUATION"]
    vs = ev["WORLDS"]["VERSUS"]["GESIT_LEARNING"]
    n = ev["WORLDS"]["COUNT"]
    assert text.startswith("GESIT was tested against simple rules in simulation")
    assert (f"less than always cheapest in {vs['ALWAYS_CHEAPEST']['CHEAPER_IN']} of {n} worlds"
            in text)
    assert f"keeping the PO in {vs['KEEP_CURRENT_PO']['CHEAPER_IN']} of {n}" in text
    fast = vs["ALWAYS_FASTEST"]
    assert (f"cheaper in {fast['CHEAPER_IN']}, equal in {fast['EQUAL_IN']}, higher in "
            f"{fast['OF'] - fast['CHEAPER_IN'] - fast['EQUAL_IN']} of {n} worlds") in text
    assert "(simulated)" in text and "proven" not in text.lower()
    md = data["TABLE_MD"]
    assert md.count("\n|") >= 6 and md.endswith("All numbers simulated.")
    assert all(line.count("|") == 5 for line in md.splitlines() if line.startswith("|"))


def test_evaluation_missing_file_is_404(client, monkeypatch, tmp_path):
    monkeypatch.setenv(api_app.EVALUATION_ENV, str(tmp_path / "none.json"))
    assert "No evaluation summary" in call(client, "GET", "/evaluation", status=404)["SUMMARY"]


def test_evaluation_summary_file_is_honest_and_complete():
    data = json.loads((API_DIR / "evaluation_summary.json").read_text(encoding="utf-8"))
    assert data["simulated"] is True and data["SEED"] == 42 and data["RUNS"] == 1000
    assert data["WORLDS"]["COUNT"] == 11 and data["COMMAND"].startswith("python -m eval.monte_carlo")
    assert set(data["TRACKED_TOTAL_JUTA"]) == set(views.EVAL_NAMES)
    assert set(data["WORLDS"]["VERSUS"]["GESIT_LEARNING"]) >= {
        "ALWAYS_CHEAPEST", "ALWAYS_FASTEST", "KEEP_CURRENT_PO"}


def test_api_summary_built_from_an_eval_report():
    from eval import monte_carlo as mc
    strategies = {s: {"MEAN_COST": 1e8 * (i + 1), "P_ANY_OUT": 0.1}
                  for i, s in enumerate(mc.STRATEGIES)}
    stats = {"CHEAPER_IN": 3, "EQUAL_IN": 5, "OF": 11, "MEAN_DIFF": 8.4e6, "MIN_DIFF": -9.5e6,
             "MAX_DIFF": 83.5e6}
    out = {"SETTINGS": {"SEED": 42, "RUNS": 1000},
           "RESULTS": {"TOTAL": {"STRATEGIES": strategies}},
           "CROSS_WORLD": {"WORLDS_USED": [f"w{i}" for i in range(11)], "WORLDS_EXCLUDED": [],
                           "MEAN_TOTAL_COST": {s: 4.6e8 for s in mc.STRATEGIES},
                           "VERSUS": {g: {o: stats for o in others}
                                      for g, others in mc.COMPARISONS}}}
    summary = mc.api_summary(out)
    assert summary["TRACKED_TOTAL_JUTA"]["GESIT_RPT_ONLY"] == 100.0
    assert summary["WORLDS"]["VERSUS"]["GESIT_LEARNING"]["ALWAYS_FASTEST"]["MEAN_DIFF_JUTA"] == 8.4
    text = views.evaluation_summary(summary)
    assert "it is about equal to always fastest: cheaper in 3, equal in 5, higher in 3 of 11 " \
           "worlds, Rp8.4 juta (simulated) higher on average" in text
    with pytest.raises(SystemExit):
        mc.api_summary({k: v for k, v in out.items() if k != "CROSS_WORLD"})


def test_the_api_never_imports_eval_or_reads_truth():
    for path in API_DIR.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+eval\b", source, re.MULTILINE), path.name
        assert not re.search(r"""["'][^"'
]*_truth""", source), path.name   # no path to it


# ------------------------------------------------------- B4 next steps after reject
def test_reject_ends_with_the_override_phrase(client):
    call(client, "POST", "/run", {"scenario": "S1", "llm": False})
    data = call(client, "POST", "/approvals/4500000435/reject",
                {"as": "MANAGER01", "reason": REASON})
    label = data["NEXT_OPTION"]["LABEL"]
    assert data["SUMMARY"].endswith(
        f'You can say "Override scenario S1 with supplier {label}. Justification: ...".')
