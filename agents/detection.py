"""Disruption Detection agent (FR-DET-1 to FR-DET-4).

Two steps, in a fixed order (FR-SUP-3, deterministic path):

1. `detect`: calls the tools `get_open_pos`, `get_inventory` and
   `predict_delay_risk` (SAP-RPT-1.6, with the posterior fallback), then applies
   the flag rules and stock-out arithmetic from `scoring.disruption`. Every
   number in the report comes from this step.
2. `reason`: Gemini Flash (Strands agent) writes a short assessment of the report.
   It receives the numbers and may not compute or invent new ones. Supplier
   names are masked to their LIFNR before the text leaves the system (PRD
   section 10); buyer names are not sent.

If the reasoning model is unavailable the report is still returned, with
`REASONING` empty and `REASONING_ERROR` set.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from adapters import CsvAdapter, default_adapter
from agents import llm
from integrations.sap.rpt_client import RptClient
from scoring import disruption
from tools import get_inventory, get_open_pos, predict_delay_risk

AGENT_NAME = "disruption_detection"
MAX_REASONING_WORDS = 120
LLM_ATTEMPTS = 3                # tries for a transient model error
LLM_RETRY_WAIT_SECONDS = 2.0    # wait before try n+1 is n times this

SYSTEM_PROMPT = f"""You are the Disruption Detection agent of GESIT, a procurement assistant \
for a food and beverage manufacturer. You receive a JSON detection report about open purchase \
orders for one raw material. All data is simulated.

Write an assessment for the buyer in plain English, at most {MAX_REASONING_WORDS} words:
- Say which purchase orders are flagged and why, most urgent first. FLAGGED_POS is already \
sorted by urgency.
- For each flagged order relate the expected arrival to the stock-out date using GAP_DAYS \
(positive means the goods arrive that many days after stock runs out).
- For a capacity shortfall, DELIVERABLE_KG arrives on ON_TIME_EINDT and REMAINDER_KG on \
REMAINDER_EINDT; GAP_DAYS is for the remainder.
- If PREDICTION.FALLBACK is true, say that SAP-RPT was unavailable and the delay risk comes \
from the supplier reliability fallback.
- If nothing is flagged, say so in one sentence.

Rules:
- Use only numbers that appear in the JSON. Do not calculate, estimate or round new numbers.
- Refer to suppliers by their LIFNR and to orders by their EBELN.
- Do not recommend a replacement supplier; that is the next agent's job.
- Do not use em dashes or en dashes."""

Reasoner = Callable[[dict[str, Any]], "str | tuple[str, str]"]


class DetectionAgent:
    """Finds open POs at risk for one material."""

    def __init__(self, adapter: CsvAdapter | None = None, rpt_client: RptClient | None = None,
                 reasoner: Reasoner | None = None) -> None:
        self.adapter = adapter or default_adapter()
        self.rpt_client = rpt_client
        self.reasoner = reasoner or llm_reasoner

    # ------------------------------------------------------------ step 1
    def detect(self, matnr: str, notices: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Deterministic detection report for `matnr` given supplier notices (may be empty)."""
        notices = [n for n in (notices or []) if n.get("matnr", matnr) == matnr]
        steps: list[dict[str, Any]] = []

        open_pos = get_open_pos(matnr, adapter=self.adapter)
        steps.append({"TOOL": "get_open_pos", "INPUT": {"matnr": matnr},
                      "RESULT": f"{len(open_pos)} open PO lines"})

        inventory = get_inventory(matnr, adapter=self.adapter)
        today = inventory["DATE"]
        stockout = disruption.stockout_date(today, inventory["DAYS_OF_COVER"])   # FR-DET-3
        steps.append({"TOOL": "get_inventory", "INPUT": {"matnr": matnr},
                      "RESULT": f"{inventory['DAYS_OF_COVER']} days of cover, stock-out {stockout}"})

        # FR-DET-1: one feature row per open PO, scored by SAP-RPT.
        features = {r["EBELN"]: r for r in self.adapter.rpt_rows(("predict",))}
        rows = [features[po["EBELN"]] for po in open_pos if po["EBELN"] in features]
        prediction = predict_delay_risk(rows, adapter=self.adapter, client=self.rpt_client)
        p_late = {p["EBELN"]: p["P_LATE"] for p in prediction["predictions"]}
        steps.append({"TOOL": "predict_delay_risk", "INPUT": {"rows": len(rows)},
                      "RESULT": f"model {prediction['model_id']}, fallback {prediction['fallback']}"})

        evaluated = [
            disruption.evaluate_po(                                              # FR-DET-2
                po, p_late.get(po["EBELN"]),
                [n for n in notices if n.get("ebeln") == po["EBELN"]], stockout)
            for po in open_pos
        ]
        # Most urgent first: largest gap after stock-out, then highest delay risk.
        flagged = sorted((po for po in evaluated if po["FLAGGED"]),
                         key=lambda po: (-po["GAP_DAYS"], -(po["P_LATE"] or 0.0), po["EBELN"]))

        return {
            "AGENT": AGENT_NAME,
            "DATA_NOTE": "simulated data",
            "MATNR": matnr,
            "TODAY": today,
            "TRIGGER": "supplier_notice" if notices else "scheduled_scan",
            "NOTICES": notices,
            "INVENTORY": {**inventory, "STOCKOUT_DATE": stockout},
            "PREDICTION": {"MODEL_ID": prediction["model_id"], "FALLBACK": prediction["fallback"],
                           "FALLBACK_REASON": prediction["fallback_reason"]},
            "STEPS": steps,
            "OPEN_POS": evaluated,
            "FLAGGED_POS": flagged,
            "FLAGGED_EBELN": [po["EBELN"] for po in flagged],
        }

    # ------------------------------------------------------------ step 2
    def reason(self, report: dict[str, Any]) -> dict[str, Any]:
        """Add the Gemini Flash assessment to a report. Never raises."""
        out = {**report, "REASONING": None, "REASONING_MODEL": llm.model_id(),
               "REASONING_ERROR": None}
        try:
            answer = self.reasoner(facts_for_llm(report, self.adapter))
            if isinstance(answer, tuple):   # (text, model that wrote it), may be the fallback
                answer, out["REASONING_MODEL"] = answer
            out["REASONING"] = answer.strip()
        except Exception as exc:   # the report must survive any model failure
            out["REASONING_ERROR"] = f"reasoning model unavailable: {_error_detail(exc)}"
        return out

    def run(self, matnr: str, notices: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Detect, then explain."""
        return self.reason(self.detect(matnr, notices))


def facts_for_llm(report: dict[str, Any], adapter: CsvAdapter) -> dict[str, Any]:
    """The part of the report the model may see: no buyer, supplier names masked to LIFNR."""
    names = {s["NAME1"]: lifnr for lifnr, s in adapter.suppliers().items()}

    def mask(text: str) -> str:
        for name, lifnr in names.items():
            text = text.replace(name, f"supplier {lifnr}")
        return text

    po_fields = ("EBELN", "LIFNR", "BEDAT", "EINDT", "MENGE", "NETPR", "NETWR", "P_LATE",
                 "REASONS", "PARTIAL_ARRIVAL", "EXPECTED_ARRIVAL", "GAP_DAYS")
    return {
        "DATA_NOTE": report["DATA_NOTE"],
        "MATNR": report["MATNR"],
        "TODAY": report["TODAY"],
        "TRIGGER": report["TRIGGER"],
        "NOTICES": [{"EBELN": n.get("ebeln"), "LIFNR": n.get("lifnr"), "TYPE": n.get("type"),
                     "MESSAGE": mask(n.get("message", ""))} for n in report["NOTICES"]],
        "INVENTORY": {k: report["INVENTORY"][k] for k in
                      ("LABST", "DAILY_USAGE_KG", "DAYS_OF_COVER", "STOCKOUT_DATE",
                       "DOWNTIME_COST_PER_DAY")},
        "PREDICTION": report["PREDICTION"],
        "OPEN_PO_COUNT": len(report["OPEN_POS"]),
        "FLAGGED_POS": [{k: po[k] for k in po_fields} for po in report["FLAGGED_POS"]],
    }


def llm_reasoner(facts: dict[str, Any]) -> tuple[str, str]:
    """Ask Gemini Flash for the assessment; returns the text and the model that wrote it.

    When the model runs out of output tokens the next entry of `llm.model_plan()` is tried:
    the same model with a larger budget, then the optional fallback model.
    """
    prompt = "Detection report (JSON):\n" + json.dumps(facts, indent=2)
    plan = llm.model_plan()
    for step, (model_id, max_output_tokens) in enumerate(plan, 1):
        model = llm.build_model(model=model_id, max_output_tokens=max_output_tokens)
        try:
            return _ask(model, prompt), model_id
        except Exception as exc:
            if step == len(plan) or not _is_out_of_tokens(exc):
                raise
    raise AssertionError("unreachable")


def _ask(model: Any, prompt: str) -> str:
    """One assessment from a Strands agent without tools, retrying transient errors."""
    from strands import Agent

    for attempt in range(1, LLM_ATTEMPTS + 1):
        agent = Agent(model=model, system_prompt=SYSTEM_PROMPT, tools=[], callback_handler=None)
        try:
            return str(agent(prompt))
        except Exception as exc:
            if attempt == LLM_ATTEMPTS or not _is_transient(exc):
                raise
            time.sleep(LLM_RETRY_WAIT_SECONDS * attempt)
    raise AssertionError("unreachable")


def _is_transient(exc: Exception) -> bool:
    """Overload (HTTP 5xx) and rate-limit errors are worth one more try."""
    return type(exc).__name__ in ("ServerError", "ModelThrottledException")


def _is_out_of_tokens(exc: Exception) -> bool:
    """The answer was cut off at the output token budget (Gemini finish reason MAX_TOKENS)."""
    return type(exc).__name__ == "MaxTokensReachedException"


def _error_detail(exc: Exception) -> str:
    """Short, key-free description of a model failure, with the HTTP code when there is one."""
    if isinstance(exc, llm.LlmUnavailable):
        return str(exc)
    parts = [type(exc).__name__]
    for attribute in ("code", "status"):
        value = getattr(exc, attribute, None)
        if isinstance(value, (int, str)) and value != "":
            parts.append(str(value))
    return " ".join(parts)
