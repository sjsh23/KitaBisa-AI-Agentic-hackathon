"""Run the agents in the terminal.

    python -m agents.run S1          one scripted supplier notice (S1, S2 or S3)
    python -m agents.run scan        scheduled scan, no notice
    python -m agents.run all         all three notices at once
    python -m agents.run S1 --json   full report as JSON
    python -m agents.run S1 --no-llm skip the Gemini Flash assessment

Only Disruption Detection runs for now (FR-DET); the other agents come later.
"""
from __future__ import annotations

import argparse
import json
from typing import Any

from adapters import default_adapter
from agents.detection import DetectionAgent

DEFAULT_MATNR = "RM-GULA-001"


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
                 f"{reason['REMAINDER_EINDT']} (GAP {reason['REMAINDER_GAP_DAYS']:+d})")
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
        f"Delay risk model: {pred['MODEL_ID']}"
        + (f" (FALLBACK, {pred['FALLBACK_REASON']})" if pred["FALLBACK"] else ""),
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
    lines += ["", "GAP = days from the stock-out date to the expected arrival (positive = too late).",
              "For a split delivery ARRIVAL shows both dates and GAP is for the later part.",
              f"Flagged, most urgent first: {', '.join(report['FLAGGED_EBELN']) or 'none'}"]
    if "REASONING" in report:
        lines += ["", f"Assessment ({report['REASONING_MODEL']}):",
                  report["REASONING"] or f"  not available ({report['REASONING_ERROR']})"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run GESIT Disruption Detection.")
    parser.add_argument("scenario", nargs="?", default="scan", help="S1, S2, S3, scan or all")
    parser.add_argument("--matnr", default=DEFAULT_MATNR)
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument("--no-llm", action="store_true", help="skip the Gemini Flash assessment")
    args = parser.parse_args(argv)

    try:   # local development reads credentials from .env (NFR-4)
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    adapter = default_adapter()
    agent = DetectionAgent(adapter=adapter)
    notices = notices_for(args.scenario, adapter.scenarios())
    report = agent.detect(args.matnr, notices) if args.no_llm else agent.run(args.matnr, notices)
    print(json.dumps(report, indent=2) if args.json else format_report(report))


if __name__ == "__main__":
    main()
