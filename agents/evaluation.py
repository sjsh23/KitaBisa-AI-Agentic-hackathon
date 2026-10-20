"""Supplier Evaluation agent (FR-SEL-1 to FR-SEL-5).

Runs after Disruption Detection, in a fixed order (FR-SUP-3, deterministic path):

1. `evaluate`: for every flagged PO, calls the tool `score_suppliers`, which ranks
   the approved-panel suppliers with a valid quote by expected cost (price plus
   the cost of stock-out risk). Every number in the report comes from this step.
2. `explain`: GPT-5.4 on SAP Gen AI Hub (Strands agent) writes, in under 120 words, why #1
   beats the cheapest and the fastest option, using only the numbers it receives
   (FR-SEL-4). The facts are sent as ready-made phrases (a space around every
   number, no negative amounts, near ties marked, and a WHY phrase naming the
   cost that makes the cheapest or fastest lose). A masking check runs before
   the call and the text is checked after it (`agents.grounding`): words, dashes,
   numbers not in the facts, negative amounts, glued LIFNRs, required mentions.
   Before the check, `grounding.tidy_text` fixes spacing and misspelled words
   (never a number) and lists its fixes; the check itself flags, it does not rewrite.

The model sees LIFNR numbers only, never supplier or buyer names (PRD section 10).
FR-SEL-4 and FR-SEL-5 name SAP Gen AI Hub (masking, contract grounding) for this
text. The model is served by Gen AI Hub (a foundation-models deployment on SAP AI
Core), but the orchestration service with its masking and grounding modules is not
used yet: masking is done in code, and the text never cites contract terms.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Mapping

from adapters import CsvAdapter, default_adapter
from agents import grounding, llm
from integrations.sap.rpt_client import RptClient
from scoring import disruption, supplier_score
from tools import score_suppliers

AGENT_NAME = "supplier_evaluation"
MAX_EXPLANATION_WORDS = 120   # FR-SEL-4: under 120 words
MIN_TRACK_RECORD = 10         # fewer past goods receipts than this is called out to the buyer

# Not in FR-SEL-1: suppliers with a flagged PO in the same run are not used as backups.
# Proposed to the team in docs/prd_change_proposals.md. The environment variable
# GESIT_EXCLUDE_FLAGGED_SUPPLIERS=false switches it off.
EXCLUDE_FLAGGED_SUPPLIERS = True

MASKING_NOTE = ("on, in code: supplier names replaced by LIFNR, buyer names never sent, "
                "checked before every call")
GEN_AI_HUB_NOTE = ("serves the model; orchestration masking and contract grounding not used yet "
                   "(FR-SEL-4, FR-SEL-5 target)")

SYSTEM_PROMPT = f"""You are the Supplier Evaluation agent of GESIT, a procurement assistant \
for a food and beverage manufacturer. A purchase order is at risk and backup suppliers have \
been ranked by expected cost: purchase cost plus expected days without stock times the \
downtime cost per day. Keeping the current order as it is was ranked the same way. You \
receive the facts for one replacement order as JSON. Every value is a ready-made phrase. All \
data is simulated.

Write an explanation for the buyer in plain English, at most 100 words:
- Recommend the RECOMMENDED option and give its expected cost. If RECOMMENDED_IS_KEEP is \
true, recommend keeping the current order and placing no new order.
- If KEEP_CURRENT_ORDER is present, say in one sentence why the recommendation beats keeping \
the current order: its EXPECTED_COST phrase followed by its WHY phrase.
- Explain why it beats CHEAPEST and FASTEST using their VS_RECOMMENDED phrases: give the \
EXPECTED_COST phrase followed by the WHY phrase, copied with its figures, so the buyer sees \
the reason and not only the cost difference. If RECOMMENDED_IS_CHEAPEST or \
RECOMMENDED_IS_FASTEST is true, say so instead.
- If a VS_RECOMMENDED block has NEAR_TIE true, say that the two are a near tie.
- If TRACK_RECORD_WARNING is present, repeat it.
- If PREDICTION_NOTE is present, repeat it.
- End with: All figures are simulated.

Rules:
- Copy numbers exactly as they appear in the phrases. Do not calculate, round, convert or \
estimate any number.
- Always put a space between a word and a number: "supplier 100108", "Rp 1,000,000".
- Never write a negative amount or a minus sign; use the words given (saving, extra, lower).
- Never write JSON field names.
- Spell every word correctly and keep a space after every punctuation mark.
- Plain text only: no Markdown (no **bold**, headings or bullet marks).
- Do not mention contract terms.
- Do not use em dashes or en dashes. Keep it under {MAX_EXPLANATION_WORDS} words."""

Explainer = Callable[[dict[str, Any]], "str | tuple[str, str]"]


def exclude_flagged_suppliers(env: Mapping[str, str] | None = None) -> bool:
    """EXCLUDE_FLAGGED_SUPPLIERS, unless the environment switches it."""
    env = os.environ if env is None else env
    raw = env.get("GESIT_EXCLUDE_FLAGGED_SUPPLIERS", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return EXCLUDE_FLAGGED_SUPPLIERS


def explanation_settings() -> dict[str, Any]:
    """Which model writes the explanation, at which temperature, with which masking."""
    return {"MODEL": llm.model_id(), "FALLBACK_MODELS": llm.fallback_model_ids(),
            "TEMPERATURE": llm.model_params().get("temperature"),
            "MASKING": MASKING_NOTE, "GEN_AI_HUB": GEN_AI_HUB_NOTE}


def replacement_qty(po: dict[str, Any]) -> float:
    """KG to buy elsewhere: the late remainder for a capacity shortfall, else the whole PO."""
    for reason in po["REASONS"]:
        if reason["CODE"] == disruption.REASON_CAPACITY_SHORTFALL:
            return float(reason.get("REMAINDER_KG", reason["SHORTFALL_KG"]))
    return float(po["MENGE"])


def current_po_terms(po: dict[str, Any]) -> dict[str, Any]:
    """The flagged PO as it stands: what keeping it would cost and when it arrives.

    NETPR is the revised price after a price increase; ARRIVAL is the expected arrival
    from detection (a delay notice's new date, or the remainder's date for a shortfall).
    The Action agent also uses this to say what happens to the old PO.
    """
    netpr = int(po["NETPR"])
    for reason in po["REASONS"]:
        if reason["CODE"] == disruption.REASON_PRICE_INCREASE:
            netpr = reason["NEW_NETPR"]
    return {"EBELN": po["EBELN"], "LIFNR": po["LIFNR"], "MENGE": float(po["MENGE"]),
            "PO_NETPR": int(po["NETPR"]), "NETPR": netpr, "ARRIVAL": po["EXPECTED_ARRIVAL"],
            "PARTIAL_ARRIVAL": po.get("PARTIAL_ARRIVAL"), "P_LATE": po["P_LATE"],
            "ARRIVAL_VS_STOCKOUT": po["ARRIVAL_VS_STOCKOUT"], "REASONS": po["REASONS"]}


def is_keep(row: dict[str, Any]) -> bool:
    return row.get("OPTION") == supplier_score.OPTION_KEEP


def reserve(reserved: dict[str, float], table: dict[str, Any]) -> None:
    """Add the recommended order (both parts of a split) to the capacity reservations."""
    if not table["CANDIDATES"] or is_keep(table["CANDIDATES"][0]):
        return   # keeping the current PO takes no backup supplier's spare capacity
    best = table["CANDIDATES"][0]
    for part in (best, best["SPLIT_WITH"]):
        if part:
            reserved[part["LIFNR"]] = reserved.get(part["LIFNR"], 0.0) + part["MENGE"]


class EvaluationAgent:
    """Ranks backup suppliers for every PO that Disruption Detection flagged."""

    def __init__(self, adapter: CsvAdapter | None = None, rpt_client: RptClient | None = None,
                 explainer: Explainer | None = None,
                 exclude_flagged: bool | None = None) -> None:
        self.adapter = adapter or default_adapter()
        self.rpt_client = rpt_client
        self.explainer = explainer or llm_explainer
        self.exclude_flagged = (exclude_flagged_suppliers() if exclude_flagged is None
                                else exclude_flagged)

    # ------------------------------------------------------------ step 1
    def evaluate(self, detection: dict[str, Any]) -> dict[str, Any]:
        """Deterministic ranked tables, one case per flagged PO, most urgent first."""
        matnr, need_by = detection["MATNR"], detection["INVENTORY"]["STOCKOUT_DATE"]
        # EXCLUDE_FLAGGED_SUPPLIERS: suppliers with a flagged PO right now are not backups.
        disrupted = (sorted({po["LIFNR"] for po in detection["FLAGGED_POS"]})
                     if self.exclude_flagged else [])
        # Cases are scored most urgent first; each #1 reserves its supplier's spare
        # capacity, so a later case cannot count on the same kilograms.
        reserved: dict[str, float] = {}
        steps, cases = [], []
        for po in detection["FLAGGED_POS"]:
            qty = replacement_qty(po)
            current = current_po_terms(po)
            keep_input = {k: current[k] for k in ("EBELN", "LIFNR", "NETPR", "ARRIVAL", "P_LATE")}
            table = score_suppliers(matnr, qty, need_by, exclude_lifnr=disrupted,
                                    today=detection["TODAY"], reserved_kg=reserved,
                                    adapter=self.adapter, client=self.rpt_client,
                                    current_po=keep_input)
            reserve(reserved, table)
            best = (f"keep PO {po['EBELN']}" if table["RECOMMENDED_OPTION"] == supplier_score.OPTION_KEEP
                    else table["RECOMMENDED"])
            steps.append({
                "TOOL": "score_suppliers",
                "INPUT": {"matnr": matnr, "qty": qty, "need_by": need_by,
                          "exclude_lifnr": disrupted, "reserved_kg": table["RESERVED_KG"],
                          "current_po": keep_input},
                "RESULT": (f"{len(table['CANDIDATES'])} options, {len(table['EXCLUDED'])} "
                           f"excluded, model {table['PREDICTION']['MODEL_ID']}, "
                           f"fallback {table['PREDICTION']['FALLBACK']}, #1 {best}"),
            })
            cases.append({
                "EBELN": po["EBELN"],
                "LIFNR": po["LIFNR"],
                "REASON_CODES": [r["CODE"] for r in po["REASONS"]],
                "QTY": qty,
                "NEED_BY": need_by,
                "CURRENT_PO": current,
                "SCORING": table,
            })
        return {
            "AGENT": AGENT_NAME,
            "DATA_NOTE": "simulated data",
            "MATNR": matnr,
            "TODAY": detection["TODAY"],
            "EXCLUDE_FLAGGED_SUPPLIERS": self.exclude_flagged,
            "STEPS": steps,
            "CASES": cases,
        }

    # ------------------------------------------------------------ step 2
    def explain(self, report: dict[str, Any]) -> dict[str, Any]:
        """Add the explanation and its check to every case. Never raises."""
        settings = explanation_settings()
        names = [sup["NAME1"] for sup in self.adapter.suppliers().values()]
        cases = []
        for case in report["CASES"]:
            out = {**case, "EXPLANATION": None, "EXPLANATION_MODEL": settings["MODEL"],
                   "EXPLANATION_ERROR": None, "EXPLANATION_CHECK": None, "EXPLANATION_FIXES": []}
            if not case["SCORING"]["CANDIDATES"]:
                out["EXPLANATION_ERROR"] = "no candidate supplier to explain"
                cases.append(out)
                continue
            facts = facts_for_llm(case, report)
            try:
                grounding.assert_masked(facts, names)          # PRD section 10, FR-SEL-4
                answer = self.explainer(facts)
                if isinstance(answer, tuple):   # (text, model that wrote it)
                    answer, out["EXPLANATION_MODEL"] = answer
                # Spacing and spelling fixed in code before the check (NFR-5); numbers untouched.
                out["EXPLANATION"], out["EXPLANATION_FIXES"] = grounding.tidy_text(
                    answer, SYSTEM_PROMPT, facts)
                out["EXPLANATION_CHECK"] = grounding.check_explanation(
                    out["EXPLANATION"], facts, MAX_EXPLANATION_WORDS,
                    lifnrs=[r["LIFNR"] for r in case["SCORING"]["CANDIDATES"]] + [case["LIFNR"]],
                    must_mention=required_mentions(facts))
            except grounding.MaskingError as exc:
                out["EXPLANATION_ERROR"] = str(exc)
            except Exception as exc:   # the ranked table must survive any model failure
                out["EXPLANATION_ERROR"] = f"explanation model unavailable: {llm.error_detail(exc)}"
            cases.append(out)
        return {**report, "EXPLANATION_SETTINGS": settings, "CASES": cases}

    def run(self, detection: dict[str, Any]) -> dict[str, Any]:
        """Score, then explain."""
        return self.explain(self.evaluate(detection))


def rp(amount: int) -> str:
    """An IDR amount as the model should quote it, with a space after Rp."""
    return f"Rp {amount:,}"


def _row_facts(row: dict[str, Any], qty: float) -> dict[str, Any]:
    """One ranked row as ready-made phrases, a space between every word and number."""
    if row["COVERS_FULL_QTY"]:
        quantity = f"covers the full {qty:,.0f} kg"
    else:
        part = row["SPLIT_WITH"]
        quantity = (f"{supplier_score.CANNOT_COVER_NOTE}: {row['MENGE']:,.0f} kg from "
                    f"supplier {row['LIFNR']} plus {part['MENGE']:,.0f} kg from "
                    f"supplier {part['LIFNR']} at {rp(part['NETPR'])} per kg")
    keep = is_keep(row)
    return {
        "SUPPLIER": (f"keep order {row['KEEP_EBELN']} with supplier {row['LIFNR']}, no new order"
                     if keep else f"supplier {row['LIFNR']}"),
        "RANK": f"rank {row['RANK']}, score {row['SCORE']}",
        "PRICE": f"{rp(row['NETPR'])} per kg" + (" (current order price)" if keep else ""),
        "ARRIVAL": f"arrives {row['ARRIVAL']}",
        "LATE_RISK": f"{row['P_LATE_PCT']}% chance of arriving late",
        "STOCKOUT": f"{row['EXPECTED_STOCKOUT_DAYS']} expected days without stock",
        "PURCHASE_COST": f"purchase cost {rp(row['PURCHASE_COST'])}",
        "RISK_COST": f"risk cost {rp(row['RISK_COST'])}",
        "EXPECTED_COST": f"expected cost {rp(row['EXPECTED_COST'])}",
        "PAST_DELIVERIES": f"{row['N_RECEIPTS']} past deliveries",
        "QUANTITY": quantity,
    }


def _phrase(part: dict[str, Any]) -> str:
    """'purchase saving Rp 1,000,000', 'extra risk cost Rp 5,000', or 'same purchase cost'."""
    return part["DIRECTION"] if part["AMOUNT"] == 0 else f"{part['DIRECTION']} {rp(part['AMOUNT'])}"


def _label(row: dict[str, Any]) -> str:
    """How a comparison names a row: 'supplier 100109' or 'the current order 4500000432'."""
    return f"the current order {row['KEEP_EBELN']}" if is_keep(row) else f"supplier {row['LIFNR']}"


def _why_phrase(diff: dict[str, Any], row: dict[str, Any], best: dict[str, Any]) -> str | None:
    """Why `row` costs more than #1, with the figure behind it (FR-SEL-4), or None."""
    if diff["MAIN_DRIVER"] == "RISK":
        return (f"mainly from {_phrase(diff['RISK'])} ({row['EXPECTED_STOCKOUT_DAYS']} expected "
                f"days without stock against {best['EXPECTED_STOCKOUT_DAYS']} for {_label(best)})")
    if diff["MAIN_DRIVER"] == "PURCHASE":
        return (f"mainly from {_phrase(diff['PURCHASE'])} ({rp(row['NETPR'])} per kg against "
                f"{rp(best['NETPR'])} for {_label(best)})")
    return None


def _versus_facts(row: dict[str, Any], best: dict[str, Any], qty: float) -> dict[str, Any]:
    """A comparison row: its phrases plus how it compares with #1 (never a negative amount)."""
    diff = supplier_score.compare_to_best(row, best)
    versus = {
        "EXPECTED_COST": (f"{rp(diff['EXPECTED_COST_GAP'])} more expected cost "
                          f"({diff['EXPECTED_COST_GAP_PCT']}% more)"),
        "NEAR_TIE": diff["NEAR_TIE"],
        "PURCHASE": _phrase(diff["PURCHASE"]),
        "RISK": _phrase(diff["RISK"]),
    }
    why = _why_phrase(diff, row, best)
    if why:
        versus["WHY"] = why
    return {**_row_facts(row, qty), "VS_RECOMMENDED": versus}


def facts_for_llm(case: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """What the model may see for one case, as phrases: LIFNR numbers only, no names, no buyer."""
    table = case["SCORING"]
    rows = table["CANDIDATES"]
    best = rows[0]
    cheapest = next((r for r in rows if r["IS_CHEAPEST"]), None)   # new orders only
    fastest = next((r for r in rows if r["IS_FASTEST"]), None)
    keep = next((r for r in rows if is_keep(r)), None)
    qty = case["QTY"]
    reasons = ", ".join(c.lower().replace("_", " ") for c in case["REASON_CODES"])
    facts: dict[str, Any] = {
        "DATA_NOTE": "all figures are simulated",
        "REPLACES": f"order {case['EBELN']} from supplier {case['LIFNR']} ({reasons})",
        "QUANTITY": f"{qty:,.0f} kg needed by the stock-out date {case['NEED_BY']}",
        "DOWNTIME_COST": f"{rp(table['DOWNTIME_COST_PER_DAY'])} per day without stock",
        "CANDIDATES": (f"{sum(not is_keep(r) for r in rows)} suppliers ranked"
                       + (", plus keeping the current order" if keep else "")),
        "RECOMMENDED": _row_facts(best, qty),
        "RECOMMENDED_IS_KEEP": best is keep,
        "RECOMMENDED_IS_CHEAPEST": best is cheapest,
        "RECOMMENDED_IS_FASTEST": best is fastest,
    }
    if cheapest is not None and best is not cheapest:
        facts["CHEAPEST"] = _versus_facts(cheapest, best, qty)
    if fastest is not None and best is not fastest:
        facts["FASTEST"] = _versus_facts(fastest, best, qty)
    if keep is not None and best is not keep:   # why not simply keep the current order
        facts["KEEP_CURRENT_ORDER"] = _versus_facts(keep, best, qty)
    if best["N_RECEIPTS"] < MIN_TRACK_RECORD:
        facts["TRACK_RECORD_WARNING"] = (f"the recommendation rests on only {best['N_RECEIPTS']} "
                                         f"past deliveries from supplier {best['LIFNR']}")
    if table["PREDICTION"]["FALLBACK"]:
        facts["PREDICTION_NOTE"] = ("SAP-RPT was unavailable, so the late risk comes from "
                                    "supplier reliability history")
    return facts


def required_mentions(facts: dict[str, Any]) -> list[tuple[str, str]]:
    """What the explanation must say, as (label, regex) for `grounding.check_explanation`."""
    required = [("simulated", r"\bsimulated\b")]                                  # D-3
    if any(facts.get(k, {}).get("VS_RECOMMENDED", {}).get("NEAR_TIE")
           for k in ("CHEAPEST", "FASTEST")):
        required.append(("near tie", r"near[ -]tie"))
    if facts.get("RECOMMENDED_IS_KEEP") or "KEEP_CURRENT_ORDER" in facts:
        required.append(("keeping the current order", r"\bkeep"))
    if "TRACK_RECORD_WARNING" in facts:
        n = facts["RECOMMENDED"]["PAST_DELIVERIES"].split()[0]
        required.append((f"{n} past deliveries", rf"\b{n}\b[^.]{{0,60}}(deliver|receipt)"))
    for key in ("CHEAPEST", "FASTEST"):   # FR-SEL-4: the reason, not only the cost gap
        why = facts.get(key, {}).get("VS_RECOMMENDED", {}).get("WHY")
        if why:
            amount = re.search(r"Rp ([\d,]+)", why).group(1)
            required.append((f"reason vs {key.lower()}: {why.split(' (')[0]}",
                             rf"(?<![\d,]){re.escape(amount)}(?![\d,])"))
            days = ("days without stock", r"days without stock")
            if "risk cost" in why and days not in required:
                required.append(days)
    return required


def llm_explainer(facts: dict[str, Any]) -> tuple[str, str]:
    """Ask the Gen AI Hub model for the explanation; returns the text and the model that wrote it."""
    return llm.complete(SYSTEM_PROMPT, "Replacement order facts (JSON):\n" + json.dumps(facts, indent=2))
