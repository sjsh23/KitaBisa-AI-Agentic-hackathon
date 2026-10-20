"""Disruption Detection agent on the simulated scenarios (FR-DET-1 to FR-DET-4)."""
import json

import pytest

from adapters import CsvAdapter
from agents import detection, llm
from agents.detection import DetectionAgent, facts_for_llm
from agents.run import format_report, notices_for
from integrations.sap.rpt_client import RptClient, RptUnavailable

MATNR = "RM-GULA-001"


class StubRpt(RptClient):
    """SAP-RPT stand-in: fixed P(late) per EBELN, or unavailable."""

    def __init__(self, p_late=None):
        self._p_late = p_late
        self.config = None

    @property
    def model_id(self):
        return "sap-rpt-1.6"

    def predict_late(self, context_rows, query_rows):
        if self._p_late is None:
            raise RptUnavailable("SAP-RPT credentials are not configured")
        return {r["EBELN"]: self._p_late.get(r["EBELN"], 0.1) for r in query_rows}


@pytest.fixture(scope="module")
def adapter():
    return CsvAdapter()


def make_agent(adapter, p_late=None, reasoner=lambda facts: "ok"):
    return DetectionAgent(adapter=adapter, rpt_client=StubRpt(p_late), reasoner=reasoner)


def test_scheduled_scan_flags_only_predicted_late(adapter):
    report = make_agent(adapter, {"4500000434": 0.62}).detect(MATNR)
    assert report["TRIGGER"] == "scheduled_scan"
    assert report["PREDICTION"] == {"MODEL_ID": "sap-rpt-1.6", "FALLBACK": False,
                                    "FALLBACK_REASON": None}
    assert report["FLAGGED_EBELN"] == ["4500000434"]
    assert len(report["OPEN_POS"]) == 5 and all(po["P_LATE"] is not None for po in report["OPEN_POS"])
    assert [s["TOOL"] for s in report["STEPS"]] == ["get_open_pos", "get_inventory",
                                                    "predict_delay_risk"]


def test_s1_delay_notice_arrives_after_stockout(adapter):
    notices = notices_for("S1", adapter.scenarios())
    report = make_agent(adapter, {}).detect(MATNR, notices)
    assert report["INVENTORY"]["STOCKOUT_DATE"] == "2026-10-06"
    assert report["FLAGGED_EBELN"] == ["4500000431"]
    po = report["FLAGGED_POS"][0]
    assert po["EXPECTED_ARRIVAL"] == "2026-10-11" and po["GAP_DAYS"] == 5
    assert po["REASONS"][0]["CODE"] == "DELAY_NOTICE"


def test_s2_and_s3_are_flagged(adapter):
    report = make_agent(adapter, {}).detect(MATNR, notices_for("all", adapter.scenarios()))
    reasons = {po["EBELN"]: po["REASONS"][0]["CODE"] for po in report["FLAGGED_POS"]}
    assert reasons == {"4500000431": "DELAY_NOTICE", "4500000432": "PRICE_INCREASE",
                       "4500000433": "CAPACITY_SHORTFALL"}
    assert report["FLAGGED_EBELN"][0] == "4500000431"   # largest gap after stock-out first


def test_gaps_per_scenario_and_urgency_order(adapter):
    # FR-DET-3: S3's gap is the remainder's, read from scenarios.json (remainder_eindt).
    def flagged(scenario):
        report = make_agent(adapter, {}).detect(MATNR, notices_for(scenario, adapter.scenarios()))
        return report, report["FLAGGED_POS"][0]

    _, s1 = flagged("S1")
    _, s2 = flagged("S2")
    s3_report, s3 = flagged("S3")
    assert (s1["EBELN"], s1["GAP_DAYS"]) == ("4500000431", 5)
    assert (s2["EBELN"], s2["GAP_DAYS"]) == ("4500000432", -3)
    assert (s3["EBELN"], s3["GAP_DAYS"]) == ("4500000433", 5)
    reason = s3["REASONS"][0]
    assert reason["REMAINDER_KG"] == 7200.0 and reason["REMAINDER_GAP_DAYS"] == 5
    assert (s3["PARTIAL_ARRIVAL"], s3["EXPECTED_ARRIVAL"]) == ("2026-10-01", "2026-10-11")
    assert s2["ARRIVAL_VS_STOCKOUT"] == "arrives 3 days before stock runs out"
    assert "7,200 kg on 2026-10-11 (5 days after stock runs out)" in format_report(s3_report)
    assert s1["PARTIAL_ARRIVAL"] is None and s2["PARTIAL_ARRIVAL"] is None
    assert "2026-10-01 / 2026-10-11" in format_report(s3_report)

    report = make_agent(adapter, {}).detect(MATNR, notices_for("all", adapter.scenarios()))
    assert report["FLAGGED_EBELN"] == ["4500000431", "4500000433", "4500000432"]


def test_fallback_is_reported(adapter):
    report = make_agent(adapter, p_late=None).detect(MATNR)
    assert report["PREDICTION"]["FALLBACK"] is True
    assert report["PREDICTION"]["MODEL_ID"] == "beta-posterior-fallback"
    assert all(0 < po["P_LATE"] < 1 for po in report["OPEN_POS"])


def test_detect_is_deterministic(adapter):
    notices = notices_for("S1", adapter.scenarios())
    agent = make_agent(adapter, {"4500000434": 0.62})
    assert json.dumps(agent.detect(MATNR, notices)) == json.dumps(agent.detect(MATNR, notices))


def test_llm_facts_mask_supplier_names_and_drop_buyers(adapter):
    report = make_agent(adapter, {}).detect(MATNR, notices_for("S1", adapter.scenarios()))
    text = json.dumps(facts_for_llm(report, adapter))
    assert "PT Sinar Manis Lampung" not in text and "supplier 100101" in text
    assert "BUYER" not in text and "ERNAM" not in text


def test_run_adds_reasoning(adapter):
    seen = {}

    def reasoner(facts):
        seen.update(facts)
        return "  PO 4500000431 is at risk.  "

    report = make_agent(adapter, {}, reasoner).run(MATNR, notices_for("S1", adapter.scenarios()))
    assert report["REASONING"] == "PO 4500000431 is at risk."
    assert report["REASONING_ERROR"] is None
    # The gap goes to the model in words only, so it cannot write "-3 days after".
    assert seen["FLAGGED_POS"][0]["ARRIVAL_VS_STOCKOUT"] == "arrives 5 days after stock runs out"
    assert "GAP_DAYS" not in seen["FLAGGED_POS"][0]


def test_reasoning_is_tidied(adapter):
    reasoner = lambda facts: "PO 4500000431 has P(late) againts0.23 and arrives late.Stock runs out."
    report = make_agent(adapter, {}, reasoner).run(MATNR, notices_for("S1", adapter.scenarios()))
    assert report["REASONING"] == ("PO 4500000431 has P(late) against 0.23 and arrives late. "
                                   "Stock runs out.")
    assert "spelling againts -> against" in report["REASONING_FIXES"]
    assert "Text tidied in code: " in format_report(report)


def test_run_survives_missing_llm(adapter):
    def reasoner(facts):
        raise llm.LlmUnavailable("GEMINI_API_KEY is not set")

    report = make_agent(adapter, {}, reasoner).run(MATNR)
    assert report["REASONING"] is None
    assert "GEMINI_API_KEY is not set" in report["REASONING_ERROR"]
    assert "not available" in format_report(report)


def test_llm_settings():
    assert llm.model_id({}) == "gemini-3.5-flash"
    assert llm.model_id({"GESIT_LLM_MODEL": "gemini-2.5-flash"}) == "gemini-2.5-flash"
    assert llm.model_params({})["temperature"] == 0.2
    assert llm.model_params({"GESIT_LLM_TEMPERATURE": "0.9"})["temperature"] == 0.2   # NFR-2 cap
    assert "temperature" not in llm.model_params({"GESIT_LLM_TEMPERATURE": ""})
    with pytest.raises(llm.LlmUnavailable):
        llm.build_model({})


def test_llm_model_plan():
    main = (llm.DEFAULT_MODEL_ID, llm.MAX_OUTPUT_TOKENS)
    retry = (llm.DEFAULT_MODEL_ID, llm.RETRY_OUTPUT_TOKENS)
    fallback = [("gemini-2.5-flash", llm.MAX_OUTPUT_TOKENS),
                ("gemini-2.5-flash", llm.RETRY_OUTPUT_TOKENS)]
    assert llm.model_plan({}) == [main, retry] + fallback          # gemini-2.5-flash by default
    assert llm.model_plan({"GESIT_LLM_FALLBACK_MODEL": "none"}) == [main, retry]
    assert llm.model_plan({"GESIT_LLM_FALLBACK_MODEL": llm.DEFAULT_MODEL_ID}) == [main, retry]
    assert llm.model_params({}, 4096)["max_output_tokens"] == 4096


class MaxTokensReachedException(Exception):
    """Same class name as the Strands exception the reasoner looks for."""


def run_reasoner(monkeypatch, failing_steps, fallback="gemini-2.5-flash"):
    """`llm_reasoner` with a fake model call that runs out of tokens on `failing_steps`."""
    calls = []

    def fake_ask(model, system_prompt, prompt):
        calls.append(model)
        if len(calls) in failing_steps:
            raise MaxTokensReachedException()
        return "assessment"

    monkeypatch.setenv("GESIT_LLM_MODEL", "")
    monkeypatch.setenv("GESIT_LLM_FALLBACK_MODEL", fallback)
    monkeypatch.setattr(llm, "build_model",
                        lambda env=None, model=None, max_output_tokens=0: (model, max_output_tokens))
    monkeypatch.setattr(llm, "_ask", fake_ask)
    llm.reset_quota_memory()
    return detection.llm_reasoner({}), calls


def test_reasoner_retries_with_larger_budget_then_fallback_model(monkeypatch):
    main, retry = llm.DEFAULT_MODEL_ID, llm.RETRY_OUTPUT_TOKENS
    assert run_reasoner(monkeypatch, ()) == (("assessment", main), [(main, llm.MAX_OUTPUT_TOKENS)])
    answer, calls = run_reasoner(monkeypatch, (1,))
    assert answer == ("assessment", main) and calls[-1] == (main, retry)
    answer, calls = run_reasoner(monkeypatch, (1, 2))
    assert answer == ("assessment", "gemini-2.5-flash") and len(calls) == 3
    with pytest.raises(MaxTokensReachedException):
        run_reasoner(monkeypatch, (1, 2), fallback="none")


def test_run_reports_the_model_that_answered(adapter):
    report = make_agent(adapter, {}, lambda facts: (" ok ", "gemini-2.5-flash")).run(MATNR)
    assert report["REASONING"] == "ok" and report["REASONING_MODEL"] == "gemini-2.5-flash"


def test_unknown_scenario_exits(adapter):
    with pytest.raises(SystemExit):
        notices_for("S9", adapter.scenarios())
