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

The text is tidied in code (`agents.grounding.tidy_text`: spacing and spelling,
never a number) and the fixes are kept in `REASONING_FIXES`.

If the reasoning model is unavailable the report is still returned, with
`REASONING` empty and `REASONING_ERROR` set.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from adapters import CsvAdapter, default_adapter
from agents import grounding, llm
from integrations.sap.rpt_client import RptClient
from scoring import disruption
from tools import get_inventory, get_open_pos, predict_delay_risk

AGENT_NAME = "disruption_detection"
MAX_REASONING_WORDS = 120

SYSTEM_PROMPT = f"""You are the Disruption Detection agent of GESIT, a procurement assistant \
for a food and beverage manufacturer. You receive a JSON detection report about open purchase \
orders for one raw material. All data is simulated.

Write an assessment for the buyer in plain English, at most {MAX_REASONING_WORDS} words:
- Say which purchase orders are flagged and why, most urgent first. FLAGGED_POS is already \
sorted by urgency.
- For each flagged order say how the expected arrival relates to the stock-out date by \
copying ARRIVAL_VS_STOCKOUT as written, for example "arrives 3 days before stock runs out".
- For a capacity shortfall, DELIVERABLE_KG arrives on ON_TIME_EINDT and REMAINDER_KG on \
REMAINDER_EINDT; ARRIVAL_VS_STOCKOUT names both parts.
- If PREDICTION.FALLBACK is true, say that SAP-RPT was unavailable and the delay risk comes \
from the supplier reliability fallback.
- If nothing is flagged, say so in one sentence.

Rules:
- Use only numbers that appear in the JSON. Do not calculate, estimate or round new numbers.
- Never write a day count with a minus or plus sign.
- Refer to suppliers by their LIFNR and to orders by their EBELN.
- Do not recommend a replacement supplier; that is the next agent's job.
- Always put a space between a word and a number ("against 0.23", "supplier 100108").
- Spell every word correctly and keep a space after every punctuation mark.
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
               "REASONING_ERROR": None, "REASONING_FIXES": []}
        try:
            facts = facts_for_llm(report, self.adapter)
            answer = self.reasoner(facts)
            if isinstance(answer, tuple):   # (text, model that wrote it), may be the fallback
                answer, out["REASONING_MODEL"] = answer
            # Spacing and spelling fixed in code (NFR-5); numbers are never changed.
            out["REASONING"], out["REASONING_FIXES"] = grounding.tidy_text(
                answer, SYSTEM_PROMPT, facts)
        except Exception as exc:   # the report must survive any model failure
            out["REASONING_ERROR"] = f"reasoning model unavailable: {llm.error_detail(exc)}"
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

    # The gap goes in words (ARRIVAL_VS_STOCKOUT), not as GAP_DAYS, so the model cannot
    # write "-3 days after stock runs out".
    po_fields = ("EBELN", "LIFNR", "BEDAT", "EINDT", "MENGE", "NETPR", "NETWR", "P_LATE",
                 "REASONS", "PARTIAL_ARRIVAL", "EXPECTED_ARRIVAL", "ARRIVAL_VS_STOCKOUT")
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
    """Ask Gemini Flash for the assessment; returns the text and the model that wrote it."""
    return llm.complete(SYSTEM_PROMPT, "Detection report (JSON):\n" + json.dumps(facts, indent=2))
