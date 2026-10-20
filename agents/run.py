"""Run the agents in the terminal.

    python -m agents.run S1               one scripted supplier notice (S1, S2 or S3)
    python -m agents.run scan             scheduled scan, no notice
    python -m agents.run all              all three notices at once
    python -m agents.run S1 --json        full reports as JSON
    python -m agents.run S1 --no-llm      skip the model texts (Gen AI Hub)
    python -m agents.run S1 --detect-only Disruption Detection only
    python -m agents.run S1 --approve     also simulate the human approvals (buyer, manager)

Disruption Detection (FR-DET) runs first, then Supplier Evaluation (FR-SEL) for
every flagged PO, then Action (FR-ACT): a draft PO for each #1, routed by policy.
The supervisor comes later.

One world: S1, S2 and S3 all see every notice on file, so Detection flags the same
POs in each; the scenario only picks which case is shown and drafted. A case's
table is then the same alone or in `all`.
"""
from __future__ import annotations

import argparse
import json
from typing import Any

from adapters import default_adapter
from agents import action, llm
from agents.action import ActionAgent, human_approvals
from agents.detection import DetectionAgent
from agents import supplier_evaluation
from agents.supplier_evaluation import SupplierEvaluationAgent
from integrations.sap.rpt_client import RptClient
from scoring import disruption, supplier_score

DEFAULT_MATNR = "RM-GULA-001"
REASON_LABELS = {"PREDICTED_LATE": "predicted late", "DELAY_NOTICE": "delay notice",
                 "PRICE_INCREASE": "price increase", "CAPACITY_SHORTFALL": "capacity shortfall"}
EXCLUDED_LABELS = {"OFF_PANEL": "off the approved panel", "QUOTE_NOT_VALID": "quote not valid today",
                   "NO_SPARE_CAPACITY": "no spare capacity",
                   "DISRUPTED_SUPPLIER": "has a PO flagged by Detection, so no new order from it "
                                         "(EXCLUDE_FLAGGED_SUPPLIERS)"}


def notices_for(scenario: str, scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Supplier notices for a scenario id, `all`, or `scan` (none)."""
    key = scenario.upper()
    if key == "SCAN":
        return []
    if key == "ALL":
        return list(scenarios)
    found = [s for s in scenarios if s["id"].upper() == key]
    if not found:
        raise SystemExit(f"unknown scenario '{scenario}', use S1, S2, S3, scan or all")
    return found


def notices_on_file(scenario: str, scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The notices Detection sees: all of them on file for S1, S2, S3 and `all` (one world,
    every open PO checked against every notice, FR-DET-1, FR-DET-2); none for `scan`."""
    notices_for(scenario, scenarios)   # rejects an unknown scenario id
    return [] if scenario.upper() == "SCAN" else list(scenarios)


def cases_for(scenario: str, scenarios: list[dict[str, Any]]) -> list[str] | None:
    """EBELNs whose case a scenario shows: the scenario's own PO, or None (all) for
    `all` and `scan`. Supplier Evaluation still scores against the full flagged list."""
    if scenario.upper() in ("ALL", "SCAN"):
        return None
    return [n["ebeln"] for n in notices_for(scenario, scenarios)]


def _reason_text(reason: dict[str, Any]) -> str:
    code = reason["CODE"]
    if code == "PREDICTED_LATE":
        return f"P(late) {reason['P_LATE']:.2f} >= {reason['THRESHOLD']}"
    if code == "DELAY_NOTICE":
        return f"delay notice, {reason['DELAY_DAYS']} days, new date {reason['NEW_EINDT']}"
    if code == "PRICE_INCREASE":
        return (f"price revision +{reason['INCREASE_PCT']}% "
                f"(Rp{reason['PO_NETPR']:,} to Rp{reason['NEW_NETPR']:,} per kg)")
    text = (f"capacity shortfall, {reason['DELIVERABLE_KG']:,.0f} of "
            f"{reason['ORDERED_KG']:,.0f} kg deliverable")
    if "REMAINDER_EINDT" in reason:   # FR-DET-3: the gap is the remainder's
        text += (f" on {reason['ON_TIME_EINDT']}, {reason['REMAINDER_KG']:,.0f} kg on "
                 f"{reason['REMAINDER_EINDT']} "
                 f"({disruption.timing_text(reason['REMAINDER_GAP_DAYS']).removeprefix('arrives ')})")
    return text


def _arrival_text(po: dict[str, Any]) -> str:
    """Expected arrival; both dates when the delivery is split (capacity shortfall)."""
    if po.get("PARTIAL_ARRIVAL"):
        return f"{po['PARTIAL_ARRIVAL']} / {po['EXPECTED_ARRIVAL']}"
    return po["EXPECTED_ARRIVAL"]


def format_report(report: dict[str, Any]) -> str:
    """Plain-text view of a detection report."""
    inv, pred = report["INVENTORY"], report["PREDICTION"]
    lines = [
        f"GESIT Disruption Detection ({report['DATA_NOTE']})",
        f"Material {report['MATNR']}, today {report['TODAY']}, trigger {report['TRIGGER']}",
        f"Stock {inv['LABST']:,.0f} kg, {inv['DAYS_OF_COVER']} days of cover, "
        f"stock-out date {inv['STOCKOUT_DATE']}",
        delay_model_text(pred),
        "",
        "Steps:",
    ]
    lines += [f"  {i}. {s['TOOL']}: {s['RESULT']}" for i, s in enumerate(report["STEPS"], 1)]
    arrivals = {po["EBELN"]: _arrival_text(po) for po in report["OPEN_POS"]}
    width = max([11] + [len(a) + 1 for a in arrivals.values()])
    lines += ["", f"{'EBELN':<11}{'LIFNR':<8}{'MENGE kg':>9}  {'EINDT':<11}{'P(late)':>8}  "
                  f"{'ARRIVAL':<{width}}{'GAP':>4}  FLAG"]
    for po in report["OPEN_POS"]:
        p_late = "n/a" if po["P_LATE"] is None else f"{po['P_LATE']:.2f}"
        flag = "; ".join(_reason_text(r) for r in po["REASONS"]) or "-"
        lines.append(f"{po['EBELN']:<11}{po['LIFNR']:<8}{po['MENGE']:>9,.0f}  {po['EINDT']:<11}"
                     f"{p_late:>8}  {arrivals[po['EBELN']]:<{width}}{po['GAP_DAYS']:>+4}  {flag}")
    lines += ["", "GAP = expected arrival against the stock-out date: -3 = arrives 3 days before "
                  "stock runs out, +5 = arrives 5 days after (too late).",
              "For a split delivery ARRIVAL shows both dates and GAP is for the later part."]
    if report.get("P_LATE_RULE"):   # reliability blend on (scoring.reliability.BLEND_RULE_TEXT)
        lines.append(report["P_LATE_RULE"])
    lines.append(f"Flagged, most urgent first: {', '.join(report['FLAGGED_EBELN']) or 'none'}")
    if "REASONING" in report:
        lines += ["", f"Assessment ({report['REASONING_MODEL']}):",
                  report["REASONING"] or f"  not available ({report['REASONING_ERROR']})"]
        lines += fixes_text(report.get("REASONING_FIXES"))
    return "\n".join(lines)


def fixes_text(fixes: list[str] | None) -> list[str]:
    """One line naming the spacing and spelling fixes made in code, if any."""
    return [f"Text tidied in code: {'; '.join(fixes)}"] if fixes else []


def _note(row: dict[str, Any]) -> str:
    if row.get("OPTION") == supplier_score.OPTION_KEEP:
        return f"keep PO {row['KEEP_EBELN']} as it is, no new order"
    marks = [m for m, on in (("cheapest", row["IS_CHEAPEST"]), ("fastest", row["IS_FASTEST"])) if on]
    if row["SPLIT_WITH"]:   # FR-SEL-2: never ranked for more than it can supply
        part = row["SPLIT_WITH"]
        marks.append(f"{row['CAPACITY_NOTE']}: split {row['MENGE']:,.0f} kg here + "
                     f"{part['MENGE']:,.0f} kg from {part['LIFNR']} at Rp{part['NETPR']:,}")
    return ", ".join(marks) or "-"


def _check_text(check: dict[str, Any]) -> str:
    text = f"{check['WORDS']} words"
    if not check["WITHIN_WORD_LIMIT"]:
        text += " (over the limit)"
    if check["UNGROUNDED_NUMBERS"]:
        text += f", numbers not in the facts: {', '.join(check['UNGROUNDED_NUMBERS'])}"
    if check["NEGATIVE_AMOUNTS"]:
        text += f", negative amounts: {', '.join(check['NEGATIVE_AMOUNTS'])}"
    if check["MISSING_SPACES"]:
        text += f", missing space next to a LIFNR: {', '.join(check['MISSING_SPACES'])}"
    if check["MISSING_MENTIONS"]:
        text += f", does not mention: {', '.join(check['MISSING_MENTIONS'])}"
    if check["HAS_DASHES"]:
        text += ", contains em or en dashes"
    if check.get("REPEATED_PHRASES"):
        text += f", repeated wording: {'; '.join(check['REPEATED_PHRASES'])}"
    return text + (", passed" if check["PASSED"] else ", FAILED")


def format_case(case: dict[str, Any]) -> list[str]:
    """Plain-text view of one ranked table (FR-SEL-3)."""
    table, pred = case["SCORING"], case["SCORING"]["PREDICTION"]
    reasons = ", ".join(REASON_LABELS.get(c, c) for c in case["REASON_CODES"])
    lines = [
        f"Replacement for PO {case['EBELN']} (supplier {case['LIFNR']}, {reasons}): "
        f"{case['QTY']:,.0f} kg, needed by {case['NEED_BY']} (stock-out date)",
        delay_model_text(pred),
    ]
    lines += [
        "",
        f"{'RANK':>4}  {'LIFNR':<8}{'SPARE kg':>9}{'NETPR':>7}{'LEAD':>5}  {'ARRIVAL':<11}"
        f"{'P(late)':>8}{'d_late':>7}{'GRs':>5}{'OUT d':>7}{'PURCHASE Rp':>14}{'RISK Rp':>15}"
        f"{'EXPECTED Rp':>15}{'SCORE':>7}  NOTE",
    ]
    for r in table["CANDIDATES"]:
        spare = "-" if r["AVAILABLE_KG"] is None else f"{r['AVAILABLE_KG']:,.0f}"
        lines.append(
            f"{r['RANK']:>4}  {r['LIFNR']:<8}{spare:>9}{r['NETPR']:>7,}"
            f"{r['LEAD_DAYS']:>5}  {r['ARRIVAL']:<11}"
            f"{r['P_LATE']:>8.2f}{r['D_LATE']:>7.1f}{r['N_RECEIPTS']:>5}"
            f"{r['EXPECTED_STOCKOUT_DAYS']:>7.2f}"
            f"{r['PURCHASE_COST']:>14,}{r['RISK_COST']:>15,}{r['EXPECTED_COST']:>15,}"
            f"{r['SCORE']:>7.1f}  {_note(r)}")
    if not table["CANDIDATES"]:
        lines.append("  no candidate supplier")
    lines += [
        "",
        "Excluded: " + ("; ".join(f"{e['LIFNR']} {EXCLUDED_LABELS.get(e['REASON'], e['REASON'])}"
                                  for e in table["EXCLUDED"]) or "none"),
        f"SPARE = AVAILABLE_KG_NEXT_30D left in this run. GRs = past goods receipts. "
        f"OUT d = expected stock-out days.",
        f"EXPECTED = PURCHASE + OUT d x Rp{table['DOWNTIME_COST_PER_DAY']:,} per day of "
        f"downtime (RISK).",
    ]
    lines += capacity_rule_text(case)
    lines.append("Cheapest tie-break: on an equal NETPR, fewer OUT d first, then more SPARE kg, "
                 "then the lower LIFNR.")
    lines.append(supplier_score.fastest_rule_text())
    if table.get("P_LATE_RULE"):   # reliability blend on (scoring.reliability.BLEND_RULE_TEXT)
        lines += [table["P_LATE_RULE"], p_late_sources_text(case)]
    if table.get("KEEP"):
        lines.append(keep_row_text(case, table["KEEP"]))
    best = table["RECOMMENDED"] or "none"
    if table.get("RECOMMENDED_OPTION") == supplier_score.OPTION_KEEP:
        best = f"keep PO {case['EBELN']} ({table['RECOMMENDED']}), no new order"
    lines.append(f"Recommended: {best}, cheapest new order: {table['CHEAPEST'] or 'none'}, "
                 f"fastest new order: {table['FASTEST'] or no_fastest_text(table)}")
    if "EXPLANATION" in case:
        lines += ["", f"Explanation ({case['EXPLANATION_MODEL']}):",
                  case["EXPLANATION"] or f"  not available ({case['EXPLANATION_ERROR']})"]
        lines += fixes_text(case.get("EXPLANATION_FIXES"))
        if case["EXPLANATION_CHECK"]:
            lines.append(f"Explanation check: {_check_text(case['EXPLANATION_CHECK'])}")
    return lines


def p_late_sources_text(case: dict[str, Any]) -> str:
    """The three P(late) numbers for the recommended, cheapest, fastest and keep suppliers."""
    table = case["SCORING"]
    sources = table.get("P_LATE_SOURCES") or {}

    def numbers(src: dict[str, Any] | None) -> str:
        if not src:
            return "n/a"
        rpt = "n/a" if src["P_RPT"] is None else f"{src['P_RPT']:.2f}"
        return f"SAP-RPT {rpt}, posterior {src['P_POST']:.2f}, used {src['P_USED']:.2f}"

    parts, seen = [], set()
    for lifnr in (table["RECOMMENDED"] if table.get("RECOMMENDED_OPTION") == supplier_score.OPTION_NEW_PO
                  else None, table["CHEAPEST"], table["FASTEST"]):
        if lifnr and lifnr not in seen:
            seen.add(lifnr)
            parts.append(f"{lifnr} {numbers(sources.get(lifnr))}")
    if table.get("KEEP"):
        keep = table["KEEP"]
        parts.append(f"keep PO {keep['KEEP_EBELN']} ({keep['LIFNR']}) "
                     f"{numbers((case.get('CURRENT_PO') or {}).get('P_LATE_SOURCES'))}")
    return "P(late) sources: " + "; ".join(parts)


def no_fastest_text(table: dict[str, Any]) -> str:
    """Why a table has no fastest mark: no new order at all, or none without a split."""
    new = [r for r in table["CANDIDATES"] if r["OPTION"] == supplier_score.OPTION_NEW_PO]
    return supplier_score.NO_FASTEST_NOTE if new else "none"


def keep_row_text(case: dict[str, Any], keep: dict[str, Any]) -> str:
    """Footer line for the keep row; the revised price is named only after a price revision."""
    codes = case["REASON_CODES"]
    kept = (f"the {keep['MENGE']:,.0f} kg remainder on PO {case['EBELN']}"
            if disruption.REASON_CAPACITY_SHORTFALL in codes else f"PO {case['EBELN']}")
    price = ("its price (the revised one after a price increase)"
             if disruption.REASON_PRICE_INCREASE in codes else "its price")
    return f"Keep row = {kept} as it stands: {price}, its expected arrival and its own P(late)."


def capacity_rule_text(case: dict[str, Any]) -> list[str]:
    """Footer lines: the fixed order spare capacity is handed out in, and what this case lost."""
    order = case.get("URGENCY_ORDER") or [case["EBELN"]]
    taken = case.get("RESERVED_FOR") or []
    return [
        "Capacity rule: spare capacity goes to the flagged POs in Detection's urgency order ("
        + ", ".join(order) + "), whichever cases a run shows; SPARE is what is left after the "
        "more urgent ones.",
        "Reserved for more urgent POs: " + ("; ".join(
            f"{t['LIFNR']} {t['KG']:,.0f} kg (PO {t['EBELN']})" for t in taken) or "none"),
    ]


def format_evaluation(report: dict[str, Any]) -> str:
    """Plain-text view of a supplier evaluation report."""
    lines = [f"GESIT Supplier Evaluation ({report['DATA_NOTE']})",
             f"Material {report['MATNR']}, today {report['TODAY']}",
             supplier_evaluation.exclude_flagged_text(report["EXCLUDE_FLAGGED_SUPPLIERS"]),
             "Flagged POs, most urgent first: " + (", ".join(report["FLAGGED_EBELN"]) or "none")
             + "; cases shown: " + (", ".join(report["SHOWN_EBELN"]) or "none")]
    settings = report.get("EXPLANATION_SETTINGS")
    if settings:
        fallback = ", ".join(settings["FALLBACK_MODELS"]) or "none"
        lines.append(f"Explanation call: model {settings['MODEL']} (fallback {fallback}), temperature "
                     f"{settings['TEMPERATURE']}, masking {settings['MASKING']}; "
                     f"SAP Gen AI Hub {settings['GEN_AI_HUB']}")
    if not report["CASES"]:
        return "\n".join(lines + ["No flagged PO, nothing to replace."])
    for case in report["CASES"]:
        lines += [""] + format_case(case)
    return "\n".join(lines)


def _next_text(order: dict[str, Any], buyer_id: str) -> str:
    """What happens to one draft next, and which policy decided it (FR-ACT-2, FR-ACT-3)."""
    check = order["RELEASE_CHECK"]
    if order["NEXT"] == action.NEXT_DONE:
        return f"Released by {order['RELEASED_BY']}."
    if order["NEXT"] == action.NEXT_BUYER:
        return f"Within policy for {buyer_id}: waiting for the buyer's one-click approval."
    if order["NEXT"] == action.NEXT_MANAGER:
        return (f"Release by {buyer_id} denied by {check['POLICY']}: {check['REASON']}. "
                f"Submitted for approval {order['APPROVAL_REQUEST_ID']}, waiting for the "
                f"procurement manager.")
    return f"Blocked by {check['POLICY']}: {check['REASON']}."


def format_action(report: dict[str, Any]) -> str:
    """Plain-text view of an action report: the drafts and who decides next."""
    lines = [f"GESIT Action ({report['DATA_NOTE']})",
             f"Drafts by {report['PRINCIPAL']}. Approval threshold "
             f"Rp{report['APPROVAL_THRESHOLD_IDR']:,} (POL-1), approved panel only (POL-2), "
             "agents never release a PO (POL-4)."]
    if not report["CASES"]:
        return "\n".join(lines + ["No recommendation, nothing to draft."])
    for case in report["CASES"]:
        lines += ["", f"Replacement for PO {case['EBELN']} (buyer {case['BUYER']}, "
                      f"{case['QTY']:,.0f} kg)"]
        old_po = case.get("OLD_PO")
        if case["NOTE"]:
            lines.append(f"  {case['NOTE']}")
            if old_po:
                lines.append(old_po["TEXT"])
            continue
        lines.append(f"{'EBELN':<12}{'LIFNR':<8}{'MENGE kg':>9}{'NETPR':>8}{'NETWR Rp':>14}  "
                     f"{'EINDT':<12}STATUS")
        for order in case["ORDERS"]:
            netwr = "-" if order["NETWR"] is None else f"{order['NETWR']:,}"
            lines += [f"{order['EBELN'] or '-':<12}{order['LIFNR']:<8}{order['MENGE']:>9,.0f}"
                      f"{order['NETPR']:>8,}{netwr:>14}  {order['EINDT']:<12}{order['STATUS']}",
                      f"  {_next_text(order, case['BUYER'])}"]
        if old_po:   # what happens to the PO being replaced
            lines.append(old_po["TEXT"])
    lines += ["", "Handed back to a human: " + ("yes" if report["HANDED_BACK"] else "no")]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run GESIT Disruption Detection, Supplier Evaluation and Action.")
    parser.add_argument("scenario", nargs="?", default="scan", help="S1, S2, S3, scan or all")
    parser.add_argument("--matnr", default=DEFAULT_MATNR)
    parser.add_argument("--json", action="store_true", help="print the full reports as JSON")
    parser.add_argument("--no-llm", action="store_true", help="skip the model texts (Gen AI Hub)")
    parser.add_argument("--detect-only", action="store_true",
                        help="run Disruption Detection only")
    parser.add_argument("--approve", action="store_true",
                        help="simulate the human clicks: each buyer approves its drafts within "
                             "policy, the manager approves the ones routed to it")
    args = parser.parse_args(argv)

    try:   # local development reads credentials from .env (NFR-4)
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    adapter = default_adapter()
    rpt_client = RptClient()   # one client, so the AI Core token is fetched once
    detector = DetectionAgent(adapter=adapter, rpt_client=rpt_client)
    notices = notices_on_file(args.scenario, adapter.scenarios())   # one world: all on file
    report = (detector.detect(args.matnr, notices) if args.no_llm
              else detector.run(args.matnr, notices))
    if args.detect_only:
        print(json.dumps(report, indent=2) if args.json
              else format_report(report) + quota_text(llm_notes()))
        return

    # The full flagged list decides exclusions and capacity; the scenario picks the cases shown.
    evaluator = SupplierEvaluationAgent(adapter=adapter, rpt_client=rpt_client)
    shown = cases_for(args.scenario, adapter.scenarios())
    evaluation = (evaluator.evaluate(report, report["FLAGGED_POS"], shown) if args.no_llm
                  else evaluator.run(report, report["FLAGGED_POS"], shown))

    actor = ActionAgent(adapter=adapter)   # in-memory PO store: every run starts clean (NFR-2)
    actions = actor.act(evaluation)
    if args.approve:
        actions = human_approvals(actions, actor.store)
    if args.json:
        print(json.dumps({"DETECTION": report, "SUPPLIER_EVALUATION": evaluation, "ACTION": actions,
                          "POS": actor.store.all(), "LLM_NOTES": llm_notes()}, indent=2))
    else:
        print(format_report(report) + "\n\n" + format_evaluation(evaluation)
              + "\n\n" + format_action(actions) + quota_text(llm_notes()))


def delay_model_text(pred: dict[str, Any]) -> str:
    """'Delay risk model: sap-rpt-1.6', with the fallback reason and any network retries."""
    text = f"Delay risk model: {pred['MODEL_ID']}"
    if pred["FALLBACK"]:
        text += f" (FALLBACK, {pred['FALLBACK_REASON']})"
    retries = pred.get("RETRIES") or []
    if retries:
        text += (f"; {len(retries)} network retr{'y' if len(retries) == 1 else 'ies'} "
                 f"({'; '.join(retries)})")
    return text

def llm_notes() -> list[str]:
    """Models that were not deployed, stayed rate limited or stayed overloaded after
    retries during this run (NFR-1, NFR-6)."""
    return llm.run_notes()


def quota_text(notes: list[str]) -> str:
    if not notes:
        return ""
    return ("\n\nLLM notes: " + "; ".join(notes)
            + ". The next model in the chain was tried (see the model name above each text).")


if __name__ == "__main__":
    main()
