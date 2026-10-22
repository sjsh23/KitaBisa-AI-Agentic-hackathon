"""Warm-up on app start, in a background thread (NFR-1); /health answers at once.

1. SAP AI Core token and the deployment lookup of every Gen AI Hub model in the chain.
2. One tiny chat request to the main model (the answer is thrown away, never reused).
3. Disruption Detection and Supplier Evaluation for every notice on file, without model
   texts, Action, PO store or logs: this sends the scenarios' exact SAP-RPT requests (and
   the AI Core token for SAP-RPT), so their answers sit in the prediction cache
   (`integrations.sap.rpt_client`) and the first real run does not wait for SAP-RPT.

Nothing here changes a result: the cache returns exactly what SAP-RPT answered for the
same rows, and every model text of a real run is still written live. Any failure is
recorded in the state and the app keeps serving; GESIT_WARMUP=0 switches it off.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

from adapters import CsvAdapter
from agents import llm
from agents.detection import DetectionAgent
from agents.run import DEFAULT_MATNR, notices_on_file
from agents.supplier_evaluation import SupplierEvaluationAgent
from integrations.sap import aicore
from integrations.sap.rpt_client import RptClient

WARMUP_ENV = "GESIT_WARMUP"
TINY_SYSTEM = "Answer with the single word OK."
TINY_PROMPT = "Are you there?"


def enabled(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return env.get(WARMUP_ENV, "1").strip().lower() not in ("0", "false", "no", "off")


def _step(state: dict[str, Any], name: str, fn: Callable[[], str]) -> None:
    start = time.perf_counter()
    try:
        result = fn()
    except Exception as exc:   # warm-up never stops the app
        result = f"failed: {type(exc).__name__}: {exc}"[:200]
    state["STEPS"].append({"STEP": name, "SECONDS": round(time.perf_counter() - start, 2),
                           "RESULT": result})


def _aicore() -> str:
    session = llm.aicore_session()
    session.token()
    found = []
    for model in llm.model_chain():
        try:
            found.append(f"{model} {session.deployment(model).version}".strip())
        except aicore.AiCoreUnavailable:
            found.append(f"{model} not deployed")
    return "token ok; deployments: " + ", ".join(found)


def _tiny_chat() -> str:
    _, model = llm.complete(TINY_SYSTEM, TINY_PROMPT)
    return f"answered by {model}"


def _prime_rpt(adapter: CsvAdapter) -> str:
    rpt = RptClient()
    detection = DetectionAgent(adapter=adapter, rpt_client=rpt).detect(
        DEFAULT_MATNR, notices_on_file("all", adapter.scenarios()))
    evaluation = SupplierEvaluationAgent(adapter=adapter, rpt_client=rpt).evaluate(
        detection, detection["FLAGGED_POS"])
    model = detection["PREDICTION"]["MODEL_ID"]
    fallback = detection["PREDICTION"]["FALLBACK"]
    return (f"{len(evaluation['CASES'])} cases scored with {model}"
            + (" (fallback, nothing cached)" if fallback else ", SAP-RPT answers cached"))


def run_warmup(adapter: CsvAdapter, state: dict[str, Any]) -> None:
    """The three warm-up steps; fills `state` (STATUS, STEPS, SECONDS)."""
    start = time.perf_counter()
    state.update(STATUS="running", STEPS=[], SECONDS=None)
    _step(state, "SAP AI Core token and deployment lookups", _aicore)
    _step(state, "tiny Gen AI Hub chat", _tiny_chat)
    _step(state, "SAP-RPT requests of the scenarios", lambda: _prime_rpt(adapter))
    state.update(STATUS="done", SECONDS=round(time.perf_counter() - start, 2))


def start(adapter: CsvAdapter, state: dict[str, Any],
          target: Callable[[CsvAdapter, dict[str, Any]], None] | None = None) -> threading.Thread:
    """Run the warm-up (default `run_warmup`) in a daemon thread and return at once."""
    state.update(STATUS="starting", STEPS=[], SECONDS=None)
    thread = threading.Thread(target=target or run_warmup, args=(adapter, state),
                              name="gesit-warmup",
                              daemon=True)
    thread.start()
    return thread
