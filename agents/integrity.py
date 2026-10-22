"""Integrity Guard demo in the terminal (FR-INT-1 to FR-INT-4, FR-SUP-2).

    python -m agents.integrity S1                         take the recommended option
    python -m agents.integrity S1 --choose 100108 --justification "text"
                                                          the buyer overrides #1
    python -m agents.integrity S1 --choose keep --justification "text"
                                                          the buyer keeps the current PO

Steps: Disruption Detection and Supplier Evaluation without model texts (no LLM is
used here), then the override rule (`check_override`). A choice other than #1 with
a justification under 20 characters is rejected and nothing is drafted. Otherwise
the override is logged (`record_override`, DECISION_LOG_PATH) and the Action agent
drafts the chosen option under the same policy rules (POL-1 routes a PO above
Rp150,000,000 to the manager, POL-2 keeps off-panel suppliers out). Last, the
integrity flags table (`get_integrity_flags`): patterns for the manager to review,
never a block on a PO or a buyer (FR-INT-4).
"""
from __future__ import annotations

import argparse
from typing import Any

from adapters import default_adapter
from agents.action import ActionAgent
from agents.detection import DetectionAgent
from agents.supplier_evaluation import SupplierEvaluationAgent
from agents.run import (DEFAULT_MATNR, cases_for, format_action, format_case,
                        notices_on_file)
from integrations.sap.rpt_client import RptClient
from scoring import integrity
from tools import get_integrity_flags
from tools.integrity_tools import (check_override, decision_log_path, find_option, keep_label,
                                   option_label, read_overrides, recommended_label,
                                   record_override)

KEEP_WORDS = {"keep", "keep po", "keep_current_po"}


def chosen_label(choose: str | None, case: dict[str, Any]) -> str | None:
    """The --choose value as an option label; 'keep' means keep the flagged PO."""
    if choose is None:
        return None
    if choose.strip().lower() in KEEP_WORDS:
        return keep_label(case["EBELN"])
    return choose.strip()


def format_override(record: dict[str, Any]) -> list[str]:
    return ["Override record (decision log):"] + [
        f"  {key}: {value:,}" if isinstance(value, int) else f"  {key}: {value}"
        for key, value in record.items()]


def is_override(choose: str | None, case: dict[str, Any]) -> bool:
    """True when --choose names an option other than #1 (FR-INT-1); None means #1."""
    chosen, recommended = chosen_label(choose, case), recommended_label(case)
    return chosen is not None and recommended is not None and \
        chosen.lower() != recommended.lower()


def buyer_choice(case: dict[str, Any], choose: str | None, justification: str,
                 buyer_id: str) -> dict[str, Any]:
    """FR-INT-1, FR-INT-2: check the buyer's choice and log an override; the printed lines too.

    Returns ALLOWED (draft may go ahead), CHOSEN (option label), OVERRIDE_ID (None when
    nothing was logged) and LINES. Nothing is drafted here; the caller passes CHOSEN and
    OVERRIDE_ID to the Action agent. Used by this CLI and by agents.supervisor.
    """
    recommended = recommended_label(case)
    chosen = chosen_label(choose, case) or recommended
    best = find_option(case, recommended)
    picked = find_option(case, chosen)
    lines = ["Buyer's choice:",
             f"  Recommended: {recommended}, expected cost Rp{best['EXPECTED_COST']:,} (simulated)"]
    out = {"ALLOWED": False, "CHOSEN": chosen, "OVERRIDE_ID": None, "LINES": lines}
    if picked is None:
        options = ", ".join(option_label(r) for r in case["SCORING"]["CANDIDATES"])
        lines.append(f"  Rejected: {chosen} is not one of the ranked options ({options}). "
                     "No draft PO was created.")
        return out
    lines.append(f"  Chosen: {option_label(picked)}, expected cost Rp{picked['EXPECTED_COST']:,} "
                 "(simulated)")
    check = check_override(recommended, chosen, justification)
    lines.append(f"  {check['MESSAGE']}")
    if not check["ALLOWED"]:
        return out

    if check["OVERRIDE"]:
        out["OVERRIDE_ID"] = record_override(case["EBELN"], recommended, chosen, justification,
                                             case=case, buyer=buyer_id)
        record = next(o for o in read_overrides() if o["OVERRIDE_ID"] == out["OVERRIDE_ID"])
        lines += [""] + format_override(record)
    else:
        lines.append("  No override, nothing logged.")
    return {**out, "ALLOWED": True}


def format_flags(flags: dict[str, Any]) -> str:
    """The manager view of FR-INT-3 and FR-INT-4 as plain text, numbers from the tool."""
    lines = [f"GESIT Integrity flags ({flags['DATA_NOTE']})",
             f"Rule (FR-INT-3): {flags['RULE']}.", "",
             f"{'BUYER':<9}{'LIFNR':<8}{'k':>4}{'n_b':>5}{'SHARE':>8}{'OTHERS':>8}{'RATIO':>7}"
             f"{'p':>10}{'OVERRIDES':>11}"]
    for f in flags["FLAGS"]:
        ratio = "-" if f["RATIO"] is None else f"{f['RATIO']:.1f}x"
        lines.append(f"{f['BUYER']:<9}{f['SUPPLIER']:<8}{f['K']:>4}{f['N_B']:>5}"
                     f"{f['SHARE_B']:>8.1%}{f['SHARE_OTHERS']:>8.1%}{ratio:>7}"
                     f"{f['P_VALUE']:>10.2g}{f['OVERRIDE_COUNT']:>11}")
    if not flags["FLAGS"]:
        lines.append("  no pair meets the rule")
    lines += [""] + [f["REVIEW_TEXT"] for f in flags["FLAGS"]]
    expected = flags["PAIRS_TESTED"] * integrity.MAX_P_VALUE
    lines += [
        "",
        f"Pairs tested: {flags['PAIRS_TESTED']} (every buyer and supplier in EKKO), flagged: "
        f"{flags['PAIRS_FLAGGED']}. With {flags['PAIRS_TESTED']} tests, about {expected:.1f} "
        f"would pass p < {integrity.MAX_P_VALUE} by chance alone; the k and ratio rules make "
        "that rarer.",
        f"Overrides in the decision log: {flags['OVERRIDES_TOTAL']}; by buyer: "
        + (", ".join(f"{b} {n}" for b, n in flags["OVERRIDES_BY_BUYER"].items()) or "none")
        + "; by chosen option: "
        + (", ".join(f"{c} {n}" for c, n in flags["OVERRIDES_BY_CHOSEN"].items()) or "none"),
        "Flags are for the procurement manager to review. They never block a PO or a buyer.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the GESIT Integrity Guard demo.")
    parser.add_argument("scenario", help="S1, S2, S3 or all")
    parser.add_argument("--matnr", default=DEFAULT_MATNR)
    parser.add_argument("--ebeln", help="flagged PO to act on (default: the most urgent one)")
    parser.add_argument("--buyer", help="buyer acting (default: the buyer of the flagged PO)")
    parser.add_argument("--choose", help="LIFNR of a ranked supplier, or 'keep' for the "
                                         "current PO (default: the recommended option)")
    parser.add_argument("--justification", default="", help="why the buyer overrides #1")
    args = parser.parse_args(argv)

    try:   # local development reads credentials from .env (NFR-4)
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    adapter = default_adapter()
    rpt_client = RptClient()
    # Same path as agents.run: all notices on file, the full flagged list into Supplier Evaluation,
    # so the table here is the one agents.run prints for this case.
    detection = DetectionAgent(adapter=adapter, rpt_client=rpt_client).detect(
        args.matnr, notices_on_file(args.scenario, adapter.scenarios()))
    shown = [args.ebeln] if args.ebeln else cases_for(args.scenario, adapter.scenarios())
    if shown and not set(shown) <= set(detection["FLAGGED_EBELN"]):
        print(f"PO {', '.join(shown)} is not flagged, nothing to choose.")
        return 0
    evaluation = SupplierEvaluationAgent(adapter=adapter, rpt_client=rpt_client).evaluate(
        detection, detection["FLAGGED_POS"], shown)
    cases = evaluation["CASES"]
    if not cases:
        print("No flagged PO to act on, nothing to choose.")
        return 0
    case = cases[0]
    buyer_id = args.buyer or next(line["ERNAM"] for line in adapter.po_lines()
                                  if line["EBELN"] == case["EBELN"])
    recommended = recommended_label(case)
    print(f"GESIT Integrity Guard ({evaluation['DATA_NOTE']})\n"
          f"Scenario {args.scenario}, PO {case['EBELN']}, buyer {buyer_id}, decision log "
          f"{decision_log_path()}\n")
    print("\n".join(format_case(case)) + "\n")
    if recommended is None:
        print("No ranked option, nothing to choose; handed back to the buyer.")
        return 0

    choice = buyer_choice(case, args.choose, args.justification, buyer_id)
    print("\n".join(choice["LINES"]))
    if not choice["ALLOWED"]:
        return 1

    actions = ActionAgent(adapter=adapter).act(
        {**evaluation, "CASES": [case]},
        choices={case["EBELN"]: {"CHOSEN": choice["CHOSEN"],
                                 "OVERRIDE_ID": choice["OVERRIDE_ID"]}},
        buyer_id=buyer_id)
    print("\n" + format_action(actions))
    print("\n" + format_flags(get_integrity_flags(adapter=adapter)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
