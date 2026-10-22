"""Speed without changing a result (NFR-1): SAP-RPT prediction cache, parallel model texts,
shared tokens, warm-up, call timing. Offline: a fake SAP-RPT session, fake model texts."""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from adapters import CsvAdapter, PoStore
from agents.action import ActionAgent
from agents.detection import DetectionAgent
from agents.run import (cases_for, format_action, format_evaluation, format_report, llm_notes,
                        notices_on_file, quota_text)
from agents.supervisor import Supervisor
from agents.supplier_evaluation import SupplierEvaluationAgent
from integrations.api import warmup
from integrations.api.app import create_app
from integrations.sap import aicore, calls, rpt_client
from integrations.sap.rpt_client import RptClient, RptConfig

MATNR = "RM-GULA-001"
CONFIG = RptConfig(mode="aicore", auth_url="https://auth.example", client_id="id",
                   client_secret="not-a-secret", base_url="https://aicore.example",
                   deployment_id="dep-1")


class Response:
    def __init__(self, body, status=200):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeRptSession:
    """SAP-RPT stand-in: a fixed P(late) per row from its LIFNR and MENGE; counts requests."""

    def __init__(self):
        self.tokens = 0
        self.predicts = 0
        self.lock = threading.Lock()

    def post(self, url, **kwargs):
        with self.lock:
            if url.endswith("/oauth/token"):
                self.tokens += 1
                return Response({"access_token": f"tok{self.tokens}", "expires_in": 3600})
            self.predicts += 1
        out = []
        for row in kwargs["json"]["rows"]:
            if row["LATE"] != rpt_client.PREDICT_PLACEHOLDER:
                continue
            p = (int(row["LIFNR"]) % 7 + 1) / 20 + (float(row["MENGE"] or 0) % 1000) / 100_000
            label = "LATE" if p >= 0.5 else "ON_TIME"
            out.append({"EBELN": row["EBELN"],
                        "LATE": [{"prediction": label, "confidence": p if p >= 0.5 else 1 - p}]})
        return Response({"predictions": out, "status": {"code": 0}})


@pytest.fixture(scope="module")
def adapter():
    return CsvAdapter()


@pytest.fixture(autouse=True)
def logs(tmp_path, monkeypatch):
    monkeypatch.setenv("DECISION_LOG_PATH", str(tmp_path / "decision_log.jsonl"))
    monkeypatch.delenv("PLAN_LOG_PATH", raising=False)
    monkeypatch.delenv("PO_STORE_PATH", raising=False)
    monkeypatch.delenv("GESIT_RPT_CACHE", raising=False)


def report_text(adapter, client, scenario="all"):
    """Detection and Supplier Evaluation without model texts, as agents.run prints them."""
    detection = DetectionAgent(adapter=adapter, rpt_client=client).detect(
        MATNR, notices_on_file(scenario, adapter.scenarios()))
    evaluation = SupplierEvaluationAgent(adapter=adapter, rpt_client=client).evaluate(
        detection, detection["FLAGGED_POS"], cases_for(scenario, adapter.scenarios()))
    return format_report(detection) + "\n\n" + format_evaluation(evaluation)


# ------------------------------------------------------------- prediction cache
def test_same_output_with_and_without_the_cache(adapter, monkeypatch):
    session = FakeRptSession()
    client = RptClient(CONFIG, session=session)
    first = report_text(adapter, client)
    called = session.predicts
    assert "sap-rpt-1.6" in first and "FALLBACK" not in first and called == 4
    assert report_text(adapter, client) == first          # every request served from the cache
    assert session.predicts == called
    assert report_text(adapter, RptClient(CONFIG, session=session)) == first   # shared by clients
    assert session.predicts == called
    monkeypatch.setenv("GESIT_RPT_CACHE", "0")
    assert report_text(adapter, client) == first          # cache off: SAP-RPT called again
    assert session.predicts == 2 * called


def test_cache_hit_and_miss_are_recorded():
    session = FakeRptSession()
    client = RptClient(CONFIG, session=session)
    context = [{"EBELN": "1", "LIFNR": "100101", "MENGE": 5000.0, "LATE": 1}]
    query = [{"EBELN": "Q", "LIFNR": "100102", "MENGE": 7200.0}]
    position = calls.mark()
    first = client.predict_late(context, query)
    assert client.predict_late(context, query) == first and session.predicts == 1
    assert calls.rpt_status(calls.since(position)) == "SAP-RPT: called and cached"
    client.predict_late(context, [{**query[0], "MENGE": 7201.0}])        # one cell differs
    assert session.predicts == 2
    other_model = RptClient(RptConfig(**{**CONFIG.__dict__, "model_id": "sap-rpt-2"}),
                            session=session)
    other_model.predict_late(context, query)                              # other model id
    assert session.predicts == 3
    position = calls.mark()
    client.predict_late(context, query)
    assert calls.rpt_status(calls.since(position)) == "SAP-RPT: cached"


def test_cache_key_has_no_credential():
    key_a = rpt_client.cache_key(CONFIG, [{"EBELN": "Q"}])
    key_b = rpt_client.cache_key(RptConfig(**{**CONFIG.__dict__, "client_secret": "other"}),
                                 [{"EBELN": "Q"}])
    assert key_a == key_b and len(key_a) == 64


def test_cache_returns_copies():
    session = FakeRptSession()
    client = RptClient(CONFIG, session=session)
    query = [{"EBELN": "Q", "LIFNR": "100102", "MENGE": 7200.0}]
    client.predict_late([], query)["Q"] = 99.0
    assert client.predict_late([], query)["Q"] != 99.0


def test_reset_does_not_clear_the_cache(monkeypatch):
    monkeypatch.setenv("GESIT_API_KEY", "k")
    rpt_client.store_prediction("key", {"Q": 0.2})
    api = TestClient(create_app(load_env=False, warm_up=False))
    assert api.post("/reset", headers={"X-API-Key": "k"}).status_code == 200
    assert rpt_client.cached_prediction("key") == {"Q": 0.2}


# ------------------------------------------------------------------ shared tokens
def test_rpt_token_is_shared_and_refreshed(monkeypatch):
    session = FakeRptSession()
    a, b = RptClient(CONFIG, session=session), RptClient(CONFIG, session=session)
    a.predict_late([], [{"EBELN": "Q1", "LIFNR": "100102", "MENGE": 1.0}])
    b.predict_late([], [{"EBELN": "Q2", "LIFNR": "100102", "MENGE": 2.0}])
    assert session.tokens == 1
    later = time.time() + 3600   # past expires_in minus the margin
    monkeypatch.setattr(rpt_client.time, "time", lambda: later)
    b.predict_late([], [{"EBELN": "Q3", "LIFNR": "100102", "MENGE": 3.0}])
    assert session.tokens == 2


class FakeAiCoreSession:
    def __init__(self):
        self.tokens = 0
        self.lock = threading.Lock()

    def post(self, url, **kwargs):
        time.sleep(0.02)
        with self.lock:
            self.tokens += 1
        return Response({"access_token": f"tok{self.tokens}", "expires_in": 3600})


def test_aicore_token_is_reused_thread_safe_and_refreshed(monkeypatch):
    config = aicore.AiCoreConfig("https://auth.example", "id", "s", "https://aicore.example")
    fake = FakeAiCoreSession()
    session = aicore.AiCoreSession(config, session=fake)
    threads = [threading.Thread(target=session.token) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fake.tokens == 1 and session.token() == "tok1"
    later = time.time() + 3600
    monkeypatch.setattr(aicore.time, "time", lambda: later)
    assert session.token() == "tok2" and fake.tokens == 2


# ------------------------------------------------------------- parallel model texts
def slow_text(seconds):
    def text(facts):
        time.sleep(seconds)
        return f"Recommend supplier {facts.get('RECOMMENDED', {}).get('SUPPLIER', 'x')}. " \
               "All figures are simulated."
    return text


def test_explanations_in_parallel_keep_order_and_checks(adapter):
    client = RptClient(CONFIG, session=FakeRptSession())
    detection = DetectionAgent(adapter=adapter, rpt_client=client).detect(
        MATNR, notices_on_file("all", adapter.scenarios()))
    agent = SupplierEvaluationAgent(adapter=adapter, rpt_client=client, explainer=slow_text(0.4))
    evaluation = agent.evaluate(detection, detection["FLAGGED_POS"])
    start = time.perf_counter()
    parallel = agent.explain(evaluation)
    took = time.perf_counter() - start
    from agents.supplier_evaluation import explanation_settings
    names = [s["NAME1"] for s in adapter.suppliers().values()]
    sequential = [agent._explain_case(c, evaluation, explanation_settings(), names)
                  for c in evaluation["CASES"]]
    assert len(sequential) == 3 and parallel["CASES"] == sequential
    assert [c["EBELN"] for c in parallel["CASES"]] == evaluation["FLAGGED_EBELN"]
    assert took < 1.0   # three 0.4 s texts at the same time, not 1.2 s one after the other


def test_supervisor_writes_the_detection_text_in_parallel_with_the_same_report(adapter):
    def supervisor_run():
        session = FakeRptSession()
        client = RptClient(CONFIG, session=session)
        rpt_client.clear_prediction_cache()
        s = Supervisor("S1", adapter=adapter, rpt_client=client, store=PoStore(adapter),
                       use_llm=True,
                       detector=DetectionAgent(adapter=adapter, rpt_client=client,
                                               reasoner=slow_text(0.5)),
                       evaluator=SupplierEvaluationAgent(adapter=adapter, rpt_client=client,
                                                         explainer=slow_text(0.5)))
        start = time.perf_counter()
        s.run()
        return s, time.perf_counter() - start

    s, took = supervisor_run()
    assert took < 0.95   # Detection text and explanation together, not 1.0 s in a row
    assert s.report["REASONING"].startswith("Recommend supplier")
    # The same run done step by step, as agents.run does it, prints the same report.
    rpt_client.clear_prediction_cache()
    client = RptClient(CONFIG, session=FakeRptSession())
    report = DetectionAgent(adapter=adapter, rpt_client=client, reasoner=slow_text(0)).run(
        MATNR, notices_on_file("S1", adapter.scenarios()))
    evaluation = SupplierEvaluationAgent(adapter=adapter, rpt_client=client,
                                         explainer=slow_text(0)).run(
        report, report["FLAGGED_POS"], cases_for("S1", adapter.scenarios()))
    actions = ActionAgent(adapter=adapter, store=PoStore(adapter)).act(evaluation)
    expected = (format_report(report) + "\n\n" + format_evaluation(evaluation) + "\n\n"
                + format_action(actions) + quota_text(llm_notes()))
    assert s.report_text() == expected


def test_plan_log_says_called_then_cached(adapter):
    session = FakeRptSession()
    client = RptClient(CONFIG, session=session)

    def run():
        s = Supervisor("S1", adapter=adapter, rpt_client=client, store=PoStore(adapter),
                       use_llm=False)
        s.run()
        return {st["STEP"]: st for st in s.plan["STEPS"]}

    first, second = run(), run()
    assert first["detection"]["SAP_RPT"] == "SAP-RPT: called"
    assert second["detection"]["SAP_RPT"] == "SAP-RPT: cached"
    assert second["supplier_evaluation"]["SAP_RPT"] == "SAP-RPT: cached"
    kinds = {c["KIND"] for c in first["detection"]["CALLS"]}
    assert kinds == {calls.SAP_RPT, calls.AICORE_TOKEN}
    assert all(isinstance(c["SECONDS"], float) for c in first["detection"]["CALLS"])


# ------------------------------------------------------------------------ warm-up
def test_warmup_does_not_block_health(monkeypatch):
    monkeypatch.setenv("GESIT_API_KEY", "k")
    release = threading.Event()

    def slow_warmup(adapter, state):
        state.update(STATUS="running")
        release.wait(10)
        state.update(STATUS="done", SECONDS=0.0)

    monkeypatch.setattr(warmup, "run_warmup", slow_warmup)
    api = TestClient(create_app(load_env=False, warm_up=True))
    start = time.perf_counter()
    assert api.get("/health").json() == {"status": "ok"}
    assert time.perf_counter() - start < 0.5
    assert api.get("/warmup", headers={"X-API-Key": "k"}).json()["STATUS"] == "running"
    release.set()
    deadline = time.time() + 5
    while api.get("/warmup", headers={"X-API-Key": "k"}).json()["STATUS"] != "done":
        assert time.time() < deadline
        time.sleep(0.05)


def test_warmup_offline_records_failures_and_finishes(adapter, monkeypatch):
    for key in ("AICORE_AUTH_URL", "AICORE_CLIENT_ID", "AICORE_CLIENT_SECRET", "AICORE_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SAP_RPT_MODE", "none")
    state = {}
    warmup.run_warmup(adapter, state)
    assert state["STATUS"] == "done" and len(state["STEPS"]) == 3
    assert state["STEPS"][0]["RESULT"].startswith("failed: LlmUnavailable")
    assert "nothing cached" in state["STEPS"][2]["RESULT"]


def test_warmup_fills_the_cache_for_the_scenarios(adapter, monkeypatch):
    session = FakeRptSession()
    monkeypatch.setattr(warmup, "RptClient", lambda: RptClient(CONFIG, session=session))
    warmup._prime_rpt(adapter)
    primed = session.predicts
    client = RptClient(CONFIG, session=session)
    for scenario in ("S1", "S2", "S3", "all"):
        report_text(adapter, client, scenario)
    assert primed == 4 and session.predicts == primed   # every scenario served from the cache


def test_warmup_switch(monkeypatch):
    monkeypatch.setenv("GESIT_WARMUP", "0")
    assert warmup.enabled() is False
    monkeypatch.setenv("GESIT_WARMUP", "1")
    assert warmup.enabled() is True
