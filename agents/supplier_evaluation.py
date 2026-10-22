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

from concurrent.futures import ThreadPoolExecutor

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
TEXT_WORKERS = 4   # model calls at the same time (NFR-1)
MAX_EXPLANATION_WORDS = 120   # FR-SEL-4: under 120 words
MIN_TRACK_RECORD = 10         # fewer past goods receipts than this is called out to the buyer
FASTEST_NOTE = ("No new order can cover the full quantity from one supplier, so there is no "
                "fastest option.")   # FR-SEL-3

# Not in FR-SEL-1: suppliers with a flagged PO in the same run are not used as backups.
# Proposed to the team in docs/prd_change_proposals.md. The environment variable
# GESIT_EXCLUDE_FLAGGED_SUPPLIERS=false switches it off.
EXCLUDE_FLAGGED_SUPPLIERS = True
# How the report labels the switch. Item 1 is not agreed by the team yet; once it is, drop
# "proposed rule, " here and nowhere else.
EXCLUDE_FLAGGED_STATUS = "proposed rule, see docs/prd_change_proposals.md"

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
- Start with OPENING, copied word for word. It already names the recommendation and its \
expected cost; do not repeat the expected cost after it.
- Then copy, word for word and in this order, the VS_RECOMMENDED SENTENCE of \
KEEP_CURRENT_ORDER, CHEAPEST and FASTEST, for each one that is present. Each sentence already \
gives the cost difference and the reason; do not reword it or add words around its figures.
- If RECOMMENDED_IS_KEEP is true, OPENING already says to keep the current order: write no \
other sentence about keeping it.
- OPENING already says when the recommendation is also the cheapest or the fastest \
option. Never write a sentence that starts with "It is also".
- If TRACK_RECORD_WARNING is present, copy it word for word, with its full stop.
- If PREDICTION_NOTE is present, copy it word for word, with its full stop.
- If FASTEST_NOTE is present, copy it word for word, with its full stop.
- If RECOMMENDED has MINIMUM_ORDER, you may say it in one short sentence, with its figures.
- End with: All figures are simulated.

Rules:
- Copy numbers exactly as they appear in the phrases. Do not calculate, round, convert or \
estimate any number.
- Always put a space between a word and a number: "supplier 100108". Write amounts as in the \
phrases, with no space after Rp: "Rp1,000,000".
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


def exclude_flagged_text(on: bool) -> str:
    """The report line for the switch: "EXCLUDE_FLAGGED_SUPPLIERS: True (proposed rule, ...)"."""
    return f"EXCLUDE_FLAGGED_SUPPLIERS: {on} ({EXCLUDE_FLAGGED_STATUS})"


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
            "P_LATE_SOURCES": po.get("P_LATE_SOURCES"),   # SAP-RPT, posterior, used (blend only)
            "ARRIVAL_VS_STOCKOUT": po["ARRIVAL_VS_STOCKOUT"], "REASONS": po["REASONS"]}


def is_keep(row: dict[str, Any]) -> bool:
    return row.get("OPTION") == supplier_score.OPTION_KEEP


def reserve(reserved: dict[str, float], table: dict[str, Any], ebeln: str,
            reserved_for: list[dict[str, Any]]) -> None:
    """Add the recommended order (both parts of a split) to the capacity reservations,
    and note which flagged PO they are for."""
    if not table["CANDIDATES"] or is_keep(table["CANDIDATES"][0]):
        return   # keeping the current PO takes no backup supplier's spare capacity
    best = table["CANDIDATES"][0]
    for part in (best, best["SPLIT_WITH"]):
        if part:
            reserved[part["LIFNR"]] = reserved.get(part["LIFNR"], 0.0) + part["MENGE"]
            reserved_for.append({"EBELN": ebeln, "LIFNR": part["LIFNR"], "KG": part["MENGE"]})


class FlaggedListMissing(ValueError):
    """Supplier Evaluation was called without Detection's full list of flagged POs."""


def check_flagged(detection: dict[str, Any], flagged_pos: list[dict[str, Any]] | None,
                  cases: list[str] | None) -> list[dict[str, Any]]:
    """The flagged POs in Detection's urgency order, or a clear error.

    Which suppliers are excluded and which spare capacity is already taken must not
    depend on which cases a caller wants to see, so the full flagged list is required.
    """
    if flagged_pos is None:
        raise FlaggedListMissing(
            "Supplier Evaluation needs the full list of flagged POs from Disruption Detection "
            "(detection['FLAGGED_POS']); pass it as flagged_pos, and use cases= to pick the "
            "cases to show.")
    given = {po["EBELN"] for po in flagged_pos}
    missing = [e for e in detection["FLAGGED_EBELN"] if e not in given]
    if missing:
        raise FlaggedListMissing(
            f"flagged_pos is missing POs that Disruption Detection flagged: {', '.join(missing)}. "
            "Pass all of detection['FLAGGED_POS'], not only the cases to show.")
    unknown = [e for e in (cases or []) if e not in given]
    if unknown:
        raise ValueError(f"cases {', '.join(unknown)} are not flagged POs")
    return sorted(flagged_pos, key=disruption.urgency_key)


class SupplierEvaluationAgent:
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
    def evaluate(self, detection: dict[str, Any], flagged_pos: list[dict[str, Any]] | None = None,
                 cases: list[str] | None = None) -> dict[str, Any]:
        """Deterministic ranked tables for the flagged POs, most urgent first.

        `flagged_pos` must be Detection's full list (detection["FLAGGED_POS"]), whatever
        `cases` (EBELNs, default all) the caller shows. Spare capacity is handed out in
        the fixed urgency order, so a case gets the same table alone or with the others:
        for a later case the more urgent ones are scored first, only to reserve theirs.
        """
        flagged = check_flagged(detection, flagged_pos, cases)
        shown = [po["EBELN"] for po in flagged] if cases is None else list(cases)
        matnr, need_by = detection["MATNR"], detection["INVENTORY"]["STOCKOUT_DATE"]
        order = [po["EBELN"] for po in flagged]
        # EXCLUDE_FLAGGED_SUPPLIERS: suppliers with a flagged PO right now are not backups.
        disrupted = sorted({po["LIFNR"] for po in flagged}) if self.exclude_flagged else []
        # Cases are scored most urgent first; each #1 reserves its supplier's spare
        # capacity, so a later case cannot count on the same kilograms.
        reserved: dict[str, float] = {}
        reserved_for: list[dict[str, Any]] = []
        last = max((order.index(e) for e in shown), default=-1)
        steps, out = [], []
        for po in flagged[:last + 1]:   # nothing after the last case shown can change it
            qty = replacement_qty(po)
            current = current_po_terms(po)
            keep_input = {k: current[k] for k in ("EBELN", "LIFNR", "NETPR", "ARRIVAL", "P_LATE")}
            before = list(reserved_for)
            table = score_suppliers(matnr, qty, need_by, exclude_lifnr=disrupted,
                                    today=detection["TODAY"], reserved_kg=reserved,
                                    adapter=self.adapter, client=self.rpt_client,
                                    current_po=keep_input)
            reserve(reserved, table, po["EBELN"], reserved_for)
            best = (f"keep PO {po['EBELN']}" if table["RECOMMENDED_OPTION"] == supplier_score.OPTION_KEEP
                    else table["RECOMMENDED"])
            steps.append({
                "TOOL": "score_suppliers",
                "INPUT": {"matnr": matnr, "qty": qty, "need_by": need_by,
                          "exclude_lifnr": disrupted, "reserved_kg": table["RESERVED_KG"],
                          "current_po": keep_input},
                "RESULT": (f"{len(table['CANDIDATES'])} options, {len(table['EXCLUDED'])} "
                           f"excluded, model {table['PREDICTION']['MODEL_ID']}, "
                           f"fallback {table['PREDICTION']['FALLBACK']}, #1 {best}"
                           + ("" if po["EBELN"] in shown
                              else ", scored only to reserve its capacity")),
            })
            if po["EBELN"] not in shown:
                continue
            out.append({
                "EBELN": po["EBELN"],
                "LIFNR": po["LIFNR"],
                "REASON_CODES": [r["CODE"] for r in po["REASONS"]],
                "QTY": qty,
                "NEED_BY": need_by,
                "CURRENT_PO": current,
                "URGENCY_ORDER": order,          # the fixed order spare capacity is handed out in
                "RESERVED_FOR": before,          # kilograms taken by more urgent flagged POs
                "SCORING": table,
            })
        return {
            "AGENT": AGENT_NAME,
            "DATA_NOTE": "simulated data",
            "MATNR": matnr,
            "TODAY": detection["TODAY"],
            "EXCLUDE_FLAGGED_SUPPLIERS": self.exclude_flagged,
            "FLAGGED_EBELN": order,
            "SHOWN_EBELN": [e for e in order if e in shown],
            "STEPS": steps,
            "CASES": out,
        }

    # ------------------------------------------------------------ step 2
    def explain(self, report: dict[str, Any]) -> dict[str, Any]:
        """Add the explanation and its check to every case. Never raises.

        The cases' model calls are independent, so they run at the same time on up to
        TEXT_WORKERS threads (NFR-1); the cases keep their order and each text gets the
        same check as before. Every text is written live, nothing is cached.
        """
        settings = explanation_settings()
        names = [sup["NAME1"] for sup in self.adapter.suppliers().values()]
        todo = report["CASES"]
        if len(todo) > 1:
            with ThreadPoolExecutor(max_workers=min(TEXT_WORKERS, len(todo)),
                                    thread_name_prefix="gesit-explanation") as pool:
                cases = list(pool.map(
                    lambda case: self._explain_case(case, report, settings, names), todo))
        else:
            cases = [self._explain_case(case, report, settings, names) for case in todo]
        return {**report, "EXPLANATION_SETTINGS": settings, "CASES": cases}

    def _explain_case(self, case: dict[str, Any], report: dict[str, Any],
                      settings: dict[str, Any], names: list[str]) -> dict[str, Any]:
        """One case's explanation and its check; never raises."""
        out = {**case, "EXPLANATION": None, "EXPLANATION_MODEL": settings["MODEL"],
               "EXPLANATION_ERROR": None, "EXPLANATION_CHECK": None, "EXPLANATION_FIXES": []}
        if not case["SCORING"]["CANDIDATES"]:
            out["EXPLANATION_ERROR"] = "no candidate supplier to explain"
            return out
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
        return out

    def run(self, detection: dict[str, Any], flagged_pos: list[dict[str, Any]] | None = None,
            cases: list[str] | None = None) -> dict[str, Any]:
        """Score, then explain (only the cases shown)."""
        return self.explain(self.evaluate(detection, flagged_pos, cases))


def rp(amount: int) -> str:
    """An IDR amount as the model should quote it, written as in the tables: "Rp178,446,429"."""
    return f"Rp{amount:,}"


def is_remainder(case: dict[str, Any]) -> bool:
    """True when the case replaces only the late remainder of a split delivery (S3)."""
    return disruption.REASON_CAPACITY_SHORTFALL in case["REASON_CODES"]


def keep_words(row: dict[str, Any], remainder: bool, ebeln: bool = True) -> str:
    """What keeping means for this case: "the current order 4500000431", or for a split
    delivery "the 7,200 kg remainder on the current order 4500000433" (kg from the row)."""
    order = f"the current order {row['KEEP_EBELN']}" if ebeln else "the current order"
    return f"the {row['MENGE']:,.0f} kg remainder on {order}" if remainder else order


def _row_facts(row: dict[str, Any], qty: float, remainder: bool = False) -> dict[str, Any]:
    """One ranked row as ready-made phrases, a space between every word and number."""
    if row["COVERS_FULL_QTY"]:
        quantity = f"covers the full {qty:,.0f} kg"
    else:
        part = row["SPLIT_WITH"]
        quantity = (f"{supplier_score.CANNOT_COVER_NOTE}: {row['MENGE']:,.0f} kg from "
                    f"supplier {row['LIFNR']} plus {part['MENGE']:,.0f} kg from "
                    f"supplier {part['LIFNR']} at {rp(part['NETPR'])} per kg")
    moq = moq_phrase(row)
    keep = is_keep(row)
    kept = f"the {row['MENGE']:,.0f} kg remainder on order" if remainder else "order"
    return {
        "SUPPLIER": (f"keep {kept} {row['KEEP_EBELN']} with supplier {row['LIFNR']}, no new order"
                     if keep else f"supplier {row['LIFNR']}"),
        "RANK": f"rank {row['RANK']}, score {row['SCORE']}",
        "PRICE": f"{rp(row['NETPR'])} per kg" + (" (current order price)" if keep else ""),
        "ARRIVAL": f"arrives {row['ARRIVAL']}",
        "LATE_RISK": f"{pct_text(row['P_LATE_PCT'])} chance of arriving late",
        "STOCKOUT": f"{row['EXPECTED_STOCKOUT_DAYS']} expected days without stock",
        "PURCHASE_COST": f"purchase cost {rp(row['PURCHASE_COST'])}",
        "RISK_COST": f"risk cost {rp(row['RISK_COST'])}",
        "EXPECTED_COST": f"expected cost {rp(row['EXPECTED_COST'])}",
        "PAST_DELIVERIES": f"{row['N_RECEIPTS']} past deliveries",
        "QUANTITY": quantity,
        **({"MINIMUM_ORDER": moq} if moq else {}),
    }


def moq_phrase(row: dict[str, Any]) -> str | None:
    """The MOQ round-up of a row in words (proposal item 12), numbers from the table, or None."""
    parts = [row] + ([row["SPLIT_WITH"]] if row.get("SPLIT_WITH") else [])
    raised = [p for p in parts if p.get("MOQ_EXTRA_KG")]
    if not raised:
        return None
    return "; ".join(
        f"order to supplier {p['LIFNR']} raised to its minimum order quantity of "
        f"{p['MENGE']:,.0f} kg, {p['MOQ_EXTRA_KG']:,.0f} kg more than needed, the extra counted "
        f"in the purchase cost" for p in raised)


def _phrase(part: dict[str, Any]) -> str:
    """'purchase saving Rp1,000,000', 'extra risk cost Rp5,000', or 'same purchase cost'."""
    return part["DIRECTION"] if part["AMOUNT"] == 0 else f"{part['DIRECTION']} {rp(part['AMOUNT'])}"


def _label(row: dict[str, Any], remainder: bool = False) -> str:
    """How a comparison names a row: 'supplier 100109' or 'the current order 4500000432'."""
    return keep_words(row, remainder) if is_keep(row) else f"supplier {row['LIFNR']}"


def _why_phrase(diff: dict[str, Any], row: dict[str, Any], best: dict[str, Any],
                remainder: bool = False) -> str | None:
    """Why `row` costs more than #1, with the figure behind it (FR-SEL-4), or None."""
    if diff["MAIN_DRIVER"] == "RISK":
        return (f"mainly from {_phrase(diff['RISK'])} ({row['EXPECTED_STOCKOUT_DAYS']} expected "
                f"days without stock against {best['EXPECTED_STOCKOUT_DAYS']} for "
                f"{_label(best, remainder)})")
    if diff["MAIN_DRIVER"] == "PURCHASE":
        return (f"mainly from {_phrase(diff['PURCHASE'])} ({rp(row['NETPR'])} per kg against "
                f"{rp(best['NETPR'])} for {_label(best, remainder)})")
    return None


def _versus_facts(row: dict[str, Any], best: dict[str, Any], qty: float,
                  subject: str, remainder: bool = False) -> dict[str, Any]:
    """A comparison row: its phrases plus how it compares with #1 (never a negative amount).
    `subject` opens the ready-made SENTENCE, for example "The cheapest option, supplier 100105"."""
    diff = supplier_score.compare_to_best(row, best)
    pct = pct_text(diff["EXPECTED_COST_GAP_PCT"])
    gap = f"{rp(diff['EXPECTED_COST_GAP'])} more ({pct} more)"
    versus = {
        "EXPECTED_COST": f"{rp(diff['EXPECTED_COST_GAP'])} more expected cost ({pct} more)",
        "NEAR_TIE": diff["NEAR_TIE"],
        "PURCHASE": _phrase(diff["PURCHASE"]),
        "RISK": _phrase(diff["RISK"]),
    }
    why = _why_phrase(diff, row, best, remainder)
    if why:
        versus["WHY"] = why
    # The whole sentence is built in code: given only phrases, the model wrapped its own words
    # around them ("with expected cost Rp 972,433,035 more expected cost").
    versus["SENTENCE"] = (f"{subject} {'is a near tie: it ' if diff['NEAR_TIE'] else ''}"
                          f"would cost {gap}" + (f", {why}" if why else "") + ".")
    return {**_row_facts(row, qty, remainder), "VS_RECOMMENDED": versus}


def facts_for_llm(case: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """What the model may see for one case, as phrases: LIFNR numbers only, no names, no buyer."""
    table = case["SCORING"]
    rows = table["CANDIDATES"]
    best = rows[0]
    cheapest = next((r for r in rows if r["IS_CHEAPEST"]), None)   # new orders only
    fastest = next((r for r in rows if r["IS_FASTEST"]), None)
    keep = next((r for r in rows if is_keep(r)), None)
    qty = case["QTY"]
    remainder = is_remainder(case)   # S3: keeping means keeping the late remainder
    reasons = ", ".join(c.lower().replace("_", " ") for c in case["REASON_CODES"])
    facts: dict[str, Any] = {
        "DATA_NOTE": "all figures are simulated",
        "REPLACES": f"order {case['EBELN']} from supplier {case['LIFNR']} ({reasons})",
        "QUANTITY": f"{qty:,.0f} kg needed by the stock-out date {case['NEED_BY']}",
        "DOWNTIME_COST": f"{rp(table['DOWNTIME_COST_PER_DAY'])} per day without stock",
        "CANDIDATES": (f"{sum(not is_keep(r) for r in rows)} suppliers ranked"
                       + (f", plus keeping {keep_words(keep, remainder, ebeln=False)}"
                          if keep else "")),
        "OPENING": opening_sentence(best, remainder,
                                    also=[w for w, on in (("cheapest", best is cheapest),
                                                          ("fastest", best is fastest)) if on]),
        "RECOMMENDED": _row_facts(best, qty, remainder),
        "RECOMMENDED_IS_KEEP": best is keep,
        "RECOMMENDED_IS_CHEAPEST": best is cheapest,
        "RECOMMENDED_IS_FASTEST": best is fastest,
    }
    if cheapest is not None and best is not cheapest:
        both = "cheapest and fastest" if cheapest is fastest else "cheapest"
        facts["CHEAPEST"] = _versus_facts(cheapest, best, qty,
                                          f"The {both} option, supplier {cheapest['LIFNR']},",
                                          remainder)
    if fastest is not None and best is not fastest and fastest is not cheapest:
        facts["FASTEST"] = _versus_facts(fastest, best, qty,
                                         f"The fastest option, supplier {fastest['LIFNR']},",
                                         remainder)
    if fastest is None and any(not is_keep(r) for r in rows):   # FR-SEL-3: every new order splits
        facts["FASTEST_NOTE"] = FASTEST_NOTE
    if keep is not None and best is not keep:   # why not simply keep the current order
        facts["KEEP_CURRENT_ORDER"] = _versus_facts(
            keep, best, qty, f"Keeping {keep_words(keep, remainder)}", remainder)
    if best["N_RECEIPTS"] < MIN_TRACK_RECORD:
        facts["TRACK_RECORD_WARNING"] = (f"The recommendation rests on only {best['N_RECEIPTS']} "
                                         f"past deliveries from supplier {best['LIFNR']}.")
    if table["PREDICTION"]["FALLBACK"]:
        facts["PREDICTION_NOTE"] = ("SAP-RPT was unavailable, so the late risk comes from "
                                    "supplier reliability history.")
    return facts


def pct_text(pct: float) -> str:
    """A gap in percent for the explanation, rounded to a whole percent (amounts stay exact):
    564.13 -> "564%", 0.4 -> "less than 1%"."""
    return "less than 1%" if pct < 1 else f"{round(pct):,}%"


def opening_sentence(best: dict[str, Any], remainder: bool = False,
                     also: list[str] | None = None) -> str:
    """The explanation's first sentence, built in code so the model does not word the
    recommendation itself (it wrote "Its expected cost is expected cost Rp ..."). `also`
    ("cheapest", "fastest") is said here, about #1, never in a later sentence that could
    read as being about another supplier."""
    cost = rp(best["EXPECTED_COST"])
    if is_keep(best):
        kept = f"the {best['MENGE']:,.0f} kg remainder on order" if remainder else "order"
        return (f"Recommend keeping {kept} {best['KEEP_EBELN']} with supplier {best['LIFNR']} "
                f"and placing no new order, at an expected cost of {cost}.")
    if also:
        return (f"Recommend supplier {best['LIFNR']}, also the {' and '.join(also)} option, "
                f"at an expected cost of {cost}.")
    return f"Recommend supplier {best['LIFNR']} at an expected cost of {cost}."


def required_mentions(facts: dict[str, Any]) -> list[tuple[str, str]]:
    """What the explanation must say, as (label, regex) for `grounding.check_explanation`."""
    required = [("simulated", r"\bsimulated\b")]                                  # D-3
    if any(facts.get(k, {}).get("VS_RECOMMENDED", {}).get("NEAR_TIE")
           for k in ("CHEAPEST", "FASTEST")):
        required.append(("near tie", r"near[ -]tie"))
    if facts.get("RECOMMENDED_IS_KEEP") or "KEEP_CURRENT_ORDER" in facts:
        required.append(("keeping the current order", r"\bkeep"))
    if "FASTEST_NOTE" in facts:
        required.append(("no fastest option", r"no fastest option"))
    if "TRACK_RECORD_WARNING" in facts:
        n = facts["RECOMMENDED"]["PAST_DELIVERIES"].split()[0]
        required.append((f"{n} past deliveries", rf"\b{n}\b[^.]{{0,60}}(deliver|receipt)"))
    for key in ("CHEAPEST", "FASTEST"):   # FR-SEL-4: the reason, not only the cost gap
        why = facts.get(key, {}).get("VS_RECOMMENDED", {}).get("WHY")
        if why:
            amount = re.search(r"Rp([\d,]+)", why).group(1)
            required.append((f"reason vs {key.lower()}: {why.split(' (')[0]}",
                             rf"(?<![\d,]){re.escape(amount)}(?![\d,])"))
            days = ("days without stock", r"days without stock")
            if "risk cost" in why and days not in required:
                required.append(days)
    return required


def llm_explainer(facts: dict[str, Any]) -> tuple[str, str]:
    """Ask the Gen AI Hub model for the explanation; returns the text and the model that wrote it."""
    return llm.complete(SYSTEM_PROMPT, "Replacement order facts (JSON):\n" + json.dumps(facts, indent=2))
