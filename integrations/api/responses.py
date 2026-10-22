"""Small JSON views of the agents' reports, each with a chat-ready SUMMARY (D-2, D-3).

Nothing here scores, checks policy or approves: every number is copied from the
Supplier Evaluation, Action, approval and Integrity Guard results. SUMMARY is 2 to 4
plain-English sentences for Joule; every amount in it says "(simulated)".
"""
from __future__ import annotations

from typing import Any

from agents import action
from agents.run import REASON_LABELS
from policy import APPROVAL_THRESHOLD_IDR
from scoring import supplier_score
from tools.integrity_tools import option_label, recommended_label

DATA_NOTE = "simulated data"
TOP_ROWS = 3   # rows of each ranked table in a response, plus the keep row
NEXT_WORDS = {action.NEXT_BUYER: "buyer approval", action.NEXT_MANAGER: "manager approval",
              action.NEXT_BLOCKED: "blocked by policy", action.NEXT_DONE: "released"}


def rp(amount: int | float) -> str:
    """An amount for SUMMARY: "Rp175,500,000 (simulated)" (D-3)."""
    return f"Rp{int(amount):,} (simulated)"


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


# ------------------------------------------------------------------ ranked tables
def marks(row: dict[str, Any]) -> list[str]:
    """The table marks of one row: keep, cheapest, fastest, MOQ round-up, split."""
    out = []
    if row.get("OPTION") == supplier_score.OPTION_KEEP:
        out.append(f"keep PO {row['KEEP_EBELN']} as it is")
    if row.get("IS_CHEAPEST"):
        out.append("cheapest")
    if row.get("IS_FASTEST"):
        out.append("fastest")
    if row.get("MOQ_EXTRA_KG"):
        out.append(supplier_score.moq_text(row["MOQ_EXTRA_KG"]))
    split = row.get("SPLIT_WITH")
    if split:
        moq = (f" ({supplier_score.moq_text(split['MOQ_EXTRA_KG'])})"
               if split.get("MOQ_EXTRA_KG") else "")
        out.append(f"split: {row['MENGE']:,.0f} kg here + {split['MENGE']:,.0f} kg from "
                   f"{split['LIFNR']} at Rp{split['NETPR']:,}{moq}")
    return out


def row_view(row: dict[str, Any]) -> dict[str, Any]:
    return {"RANK": row["RANK"], "OPTION": row["OPTION"], "LIFNR": row["LIFNR"],
            "MENGE": row["MENGE"], "NETPR": row["NETPR"], "LEAD_DAYS": row["LEAD_DAYS"],
            "ARRIVAL": row["ARRIVAL"], "P_LATE": row["P_LATE"],
            "EXPECTED_STOCKOUT_DAYS": row["EXPECTED_STOCKOUT_DAYS"],
            "EXPECTED_COST": row["EXPECTED_COST"], "MARKS": marks(row)}


def table_view(case: dict[str, Any]) -> list[dict[str, Any]]:
    """The top rows of a case's ranked table, plus the keep row when it ranked lower."""
    rows = case["SCORING"]["CANDIDATES"]
    shown = rows[:TOP_ROWS]
    keep = next((r for r in rows if r["OPTION"] == supplier_score.OPTION_KEEP), None)
    if keep is not None and keep not in shown:
        shown = shown + [keep]
    return [row_view(r) for r in shown]


MD_COLUMNS = ("Rank", "Supplier", "Expected cost (Rp juta)", "Price/kg", "Lead days", "Marks")
MD_SIMULATED = "All numbers simulated."
MD_MAX_BYTES = 3_000   # top-level TABLE_MD


def juta(amount: int | float) -> str:
    """An IDR amount in Rp juta with one decimal: 178921875 -> "178.9"."""
    return f"{amount / 1_000_000:,.1f}"


def short_marks(row: dict[str, Any]) -> str:
    """Marks for the Markdown table: cheapest, fastest, MOQ +N kg, split with X."""
    out = [m for m, on in (("cheapest", row.get("IS_CHEAPEST")),
                           ("fastest", row.get("IS_FASTEST"))) if on]
    if row.get("MOQ_EXTRA_KG"):
        out.append(supplier_score.moq_text(row["MOQ_EXTRA_KG"]))
    split = row.get("SPLIT_WITH")
    if split:
        moq = (f", {supplier_score.moq_text(split['MOQ_EXTRA_KG'])}"
               if split.get("MOQ_EXTRA_KG") else "")
        out.append(f"split with {split['LIFNR']} ({split['MENGE']:,.0f} kg{moq})")
    return ", ".join(out) or "-"


def table_md(case: dict[str, Any]) -> str:
    """The rows of `table_view` (top 3 plus the keep row) as a small Markdown table."""
    rows = case["SCORING"]["CANDIDATES"]
    shown = rows[:TOP_ROWS]
    keep = next((r for r in rows if r["OPTION"] == supplier_score.OPTION_KEEP), None)
    if keep is not None and keep not in shown:
        shown = shown + [keep]
    lines = ["| " + " | ".join(MD_COLUMNS) + " |", "|" + "---|" * len(MD_COLUMNS)]
    for r in shown:
        supplier = (f"keep PO {r['KEEP_EBELN']} ({r['LIFNR']})"
                    if r["OPTION"] == supplier_score.OPTION_KEEP else r["LIFNR"])
        lines.append(f"| {r['RANK']} | {supplier} | {juta(r['EXPECTED_COST'])} | "
                     f"Rp{r['NETPR']:,} | {r['LEAD_DAYS']} | {short_marks(r)} |")
    if not shown:
        lines.append("| - | no candidate supplier | - | - | - | - |")
    return "\n".join(lines + ["", MD_SIMULATED])


def tables_md(cases: list[dict[str, Any]]) -> str:
    """Every case's TABLE_MD under a heading per PO, kept under MD_MAX_BYTES."""
    parts: list[str] = []
    for i, case in enumerate(cases):
        reasons = ", ".join(REASON_LABELS.get(c, c) for c in case["REASON_CODES"])
        part = f"### PO {case['EBELN']} ({reasons})\n\n{table_md(case)}"
        more = f"\n\n{len(cases) - i} more cases in CASES."
        if len("\n\n".join(parts + [part]).encode()) + len(more) > MD_MAX_BYTES:
            parts.append(more.strip())
            break
        parts.append(part)
    return "\n\n".join(parts)


def case_view(case: dict[str, Any], old_po: dict[str, Any] | None = None) -> dict[str, Any]:
    table = case["SCORING"]
    out = {"EBELN": case["EBELN"], "LIFNR": case["LIFNR"], "QTY": case["QTY"],
           "NEED_BY": case["NEED_BY"],
           "REASONS": [REASON_LABELS.get(c, c) for c in case["REASON_CODES"]],
           "RECOMMENDED": recommended_label(case), "CHEAPEST": table["CHEAPEST"],
           "FASTEST": table["FASTEST"], "TABLE": table_view(case), "TABLE_MD": table_md(case),
           "OLD_PO": (old_po or {}).get("TEXT")}
    if "EXPLANATION" in case:
        out["EXPLANATION"] = case["EXPLANATION"]
        out["EXPLANATION_MODEL"] = case["EXPLANATION_MODEL"]
    return out


# ------------------------------------------------------------------------ drafts
def draft_views(actions: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not actions:
        return []
    return [{"EBELN": o["EBELN"], "LIFNR": o["LIFNR"], "MENGE": o["MENGE"], "NETPR": o["NETPR"],
             "NETWR": o["NETWR"], "EINDT": o["EINDT"], "STATUS": o["STATUS"],
             "NEXT": NEXT_WORDS.get(o["NEXT"], o["NEXT"]),
             "APPROVAL_REQUEST_ID": o["APPROVAL_REQUEST_ID"], "REPLACES_EBELN": c["EBELN"],
             "BUYER": c["BUYER"]}
            for c in actions["CASES"] for o in c["ORDERS"]]


def plan_view(plan: dict[str, Any]) -> dict[str, Any]:
    return {"PLAN_ID": plan["PLAN_ID"], "TRIGGER": plan["TRIGGER"],
            "STEPS": [{**{k: s[k] for k in ("NO", "AGENT", "STATUS", "SECONDS", "RESULT")},
                       "SAP_RPT": s.get("SAP_RPT"),   # "SAP-RPT: called" or "SAP-RPT: cached"
                       "CALLS": [{k: c[k] for k in ("KIND", "WHAT", "SECONDS", "RESULT")}
                                 for c in s.get("CALLS", [])]}
                      for s in plan["STEPS"]],
            "NOT_IN_LIVE_FLOW": plan["NOT_IN_LIVE_FLOW"], "OUTCOME": plan["OUTCOME"],
            "CASE_OUTCOMES": plan["CASES"], "TOTAL_SECONDS": plan.get("TOTAL_SECONDS")}


# ----------------------------------------------------------------------- summary
def _recommendation(case: dict[str, Any], gerund: bool = False) -> str:
    """"order from supplier 100109, at an expected cost of ..." ("ordering" with gerund)."""
    rows = case["SCORING"]["CANDIDATES"]
    if not rows:
        return f"no backup supplier can take the order for PO {case['EBELN']}"
    best = rows[0]
    cost = f"at an expected cost of {rp(best['EXPECTED_COST'])}"
    if best["OPTION"] == supplier_score.OPTION_KEEP:
        return f"{'keeping' if gerund else 'keep'} PO {case['EBELN']} as it is, {cost}"
    what = f"supplier {best['LIFNR']}"
    if best.get("SPLIT_WITH"):
        what += f" with part of the order from {best['SPLIT_WITH']['LIFNR']}"
    return f"{'ordering' if gerund else 'order'} from {what}, {cost}"


# What makes a row cost more than #1, from scoring.supplier_score.main_driver (FR-SEL-4).
DRIVER_WORDS = {"RISK": "its delay risk", "PURCHASE": "its purchase cost"}
# How SUMMARY names the cheapest and the fastest new order (IS_CHEAPEST, IS_FASTEST).
BASELINE_WORDS = (("IS_CHEAPEST", "lowest-price supplier"), ("IS_FASTEST", "fastest supplier"))


def versus_phrase(row: dict[str, Any], best: dict[str, Any], name: str) -> str:
    """"the lowest-price supplier, 100105, has an expected cost Rp75,829,526 higher
    (simulated), mainly from its delay risk"; the gap and the driver come from
    `supplier_score.compare_to_best`, never recomputed here."""
    diff = supplier_score.compare_to_best(row, best)
    text = (f"the {name}, {row['LIFNR']}, has an expected cost "
            f"Rp{diff['EXPECTED_COST_GAP']:,} higher (simulated)")
    driver = DRIVER_WORDS.get(diff["MAIN_DRIVER"])
    return text + (f", mainly from {driver}" if driver else "")


def _comparison(case: dict[str, Any]) -> str | None:
    """How #1 compares with the cheapest and the fastest new order (FR-SEL-4)."""
    rows = case["SCORING"]["CANDIDATES"]
    if not rows:
        return None
    best = rows[0]
    parts = []
    for flag, name in BASELINE_WORDS:
        row = next((r for r in rows if r[flag]), None)
        if row is None:
            continue
        parts.append(f"{best['LIFNR']} is also the {name}" if row is best
                     else versus_phrase(row, best, name))
    if not parts:
        return None
    return _upper_first("; ".join(parts)) + "."


def _upper_first(text: str) -> str:
    return text[:1].upper() + text[1:]


def _next_step(case_ebeln: str, actions: dict[str, Any] | None) -> str:
    case = next((c for c in (actions or {}).get("CASES", []) if c["EBELN"] == case_ebeln), None)
    if case is None or not case["ORDERS"]:
        return "nothing was drafted, the buyer decides"
    orders = case["ORDERS"]
    names = _and([f"PO {o['EBELN']} at {rp(o['NETWR'])}" for o in orders if o["EBELN"]] or ["-"])
    nexts = {o["NEXT"] for o in orders}
    if action.NEXT_MANAGER in nexts:
        ids = _and([o["APPROVAL_REQUEST_ID"] for o in orders if o["APPROVAL_REQUEST_ID"]])
        return (f"draft {names} is above the approval threshold of "
                f"{rp(APPROVAL_THRESHOLD_IDR)} and waits for the manager ({ids})")
    if action.NEXT_BLOCKED in nexts:
        return f"draft {names} is blocked by policy, the buyer and the manager decide"
    return f"draft {names} is within policy and waits for the buyer's one-click approval"


def next_step_sentence(case_ebeln: str, actions: dict[str, Any] | None) -> str:
    """What happens next for one case, as one sentence."""
    return _upper_first(_next_step(case_ebeln, actions)) + "."


def run_summary(report: dict[str, Any] | None, evaluation: dict[str, Any] | None,
                actions: dict[str, Any] | None) -> str:
    """2 to 4 sentences: the recommendation, cheapest and fastest, what happens next."""
    if report is None:
        return "The run did not finish. See PLAN for the step that failed."
    if not report["FLAGGED_EBELN"]:
        return (f"Detection checked {len(report['OPEN_POS'])} open POs and flagged none. "
                "No action is needed.")
    cases = (evaluation or {}).get("CASES", [])
    if not cases:
        return "Detection flagged a PO, but no case was ranked. See PLAN."
    if len(cases) == 1:
        case = cases[0]
        reasons = ", ".join(REASON_LABELS.get(c, c) for c in case["REASON_CODES"])
        rec = _recommendation(case, gerund=True)
        sentences = [f"For PO {case['EBELN']} ({reasons}), GESIT recommends {rec}."
                     if case["SCORING"]["CANDIDATES"] else f"For PO {case['EBELN']} "
                     f"({reasons}), {rec}.",
                     _comparison(case),
                     next_step_sentence(case["EBELN"], actions)]
        return " ".join(s for s in sentences if s)
    shown = cases[:3] if len(cases) <= 3 else cases[:2]
    sentences = [f"PO {c['EBELN']}: {_recommendation(c)}; {_next_step(c['EBELN'], actions)}."
                 for c in shown]
    rest = len(cases) - len(shown)
    sentences.append(f"{rest} more cases and the lowest-price and fastest comparisons are in "
                     "CASES." if rest else "The lowest-price and fastest comparisons are in CASES.")
    return " ".join(sentences)


def run_response(supervisor: Any) -> dict[str, Any]:
    """The /run JSON for one finished supervisor run."""
    old = {c["EBELN"]: c.get("OLD_PO") for c in (supervisor.actions or {}).get("CASES", [])}
    cases = (supervisor.evaluation or {}).get("CASES", [])
    return {
        "DATA_NOTE": DATA_NOTE,
        "SCENARIO": supervisor.plan["TRIGGER"]["SCENARIO"],
        "SUMMARY": run_summary(supervisor.report, supervisor.evaluation, supervisor.actions),
        "PLAN": plan_view(supervisor.plan),
        "FLAGGED_EBELN": (supervisor.report or {}).get("FLAGGED_EBELN", []),
        "CASES": [case_view(c, old.get(c["EBELN"])) for c in cases],
        "TABLE_MD": tables_md(cases),
        "DRAFTS": draft_views(supervisor.actions),
    }


# --------------------------------------------------------------------- approvals
def pending_summary(pending: list[dict[str, Any]]) -> str:
    if not pending:
        return ("No PO is waiting for approval. A draft above "
                f"{rp(APPROVAL_THRESHOLD_IDR)} appears here after a run.")
    first = pending[:2]
    lines = [f"{len(pending)} PO waits for the manager's approval." if len(pending) == 1 else
             f"{len(pending)} POs wait for the manager's approval."]
    lines += [f"PO {p['EBELN']}: supplier {p['LIFNR']}, {p['MENGE']:,.0f} kg, NETWR "
              f"{rp(p['NETWR'])}, replaces PO {p['REPLACES_EBELN']}." for p in first]
    lines.append("Approve, or reject with a reason of at least 20 characters.")
    return " ".join(lines[:4])


POLICY_WORDS = {   # the rule in words; the full reason stays in POLICY_DECISION
    "POL-1": "only role manager decides a PO above the approval threshold",
    "POL-2": "the supplier is not on the approved panel",
    "POL-3": "that tool is not on the permitted list",
    "POL-4": "an agent never approves or rejects a PO",
}


def deny_summary(verb: str, principal: Any, decision: dict[str, Any], ebeln: str,
                 status: str) -> str:
    rule = POLICY_WORDS.get(decision["POLICY"], "policy")
    return (f"{verb} by {principal.id} ({principal.role}) denied by {decision['POLICY']}: "
            f"{rule}. PO {ebeln} stays {status}.")


def approve_summary(po: dict[str, Any], by: str) -> str:
    return (f"PO {po['EBELN']} (supplier {po['LIFNR']}, NETWR {rp(po['NETWR'])}) is approved "
            f"and released by {by}. The buyer now changes the old PO {po['REPLACES_EBELN']} in "
            "S/4 as the draft says.")


def reject_summary(po: dict[str, Any], option: dict[str, Any] | None) -> str:
    sentences = [f"PO {po['EBELN']} (supplier {po['LIFNR']}, NETWR {rp(po['NETWR'])}) is "
                 f"rejected by {po['REJECTED_BY']} and will never be released.",
                 f"The old PO {po['REPLACES_EBELN']} is untouched, so buyer {po['ERNAM']} "
                 "still has it."]
    if option:
        sentences.append(f"Next option for the buyer, not drafted: rank {option['RANK']}, "
                         f"{option_label(option)}, expected cost "
                         f"{rp(option['EXPECTED_COST'])}.")
    else:
        sentences.append("No other ranked option was saved with the draft.")
    return " ".join(sentences)


def flags_view(flags: dict[str, Any]) -> dict[str, Any]:
    return {k: flags[k] for k in ("RULE", "PAIRS_TESTED", "PAIRS_FLAGGED", "FLAGS",
                                  "OVERRIDES_TOTAL", "OVERRIDES_BY_BUYER", "OVERRIDES_BY_CHOSEN")}
