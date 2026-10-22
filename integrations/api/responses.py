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


def case_view(case: dict[str, Any], old_po: dict[str, Any] | None = None) -> dict[str, Any]:
    table = case["SCORING"]
    out = {"EBELN": case["EBELN"], "LIFNR": case["LIFNR"], "QTY": case["QTY"],
           "NEED_BY": case["NEED_BY"],
           "REASONS": [REASON_LABELS.get(c, c) for c in case["REASON_CODES"]],
           "RECOMMENDED": recommended_label(case), "CHEAPEST": table["CHEAPEST"],
           "FASTEST": table["FASTEST"], "TABLE": table_view(case),
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
            "STEPS": [{k: s[k] for k in ("NO", "AGENT", "STATUS", "SECONDS", "RESULT")}
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


def _comparison(case: dict[str, Any]) -> str | None:
    """How #1 compares with the cheapest and the fastest new order (FR-SEL-4)."""
    rows = case["SCORING"]["CANDIDATES"]
    if not rows:
        return None
    best = rows[0]
    parts = []
    for word, row in (("cheapest", next((r for r in rows if r["IS_CHEAPEST"]), None)),
                      ("fastest", next((r for r in rows if r["IS_FASTEST"]), None))):
        if row is None:
            continue
        if row is best:
            parts.append(f"{best['LIFNR']} is also the {word} new order")
        else:
            gap = supplier_score.compare_to_best(row, best)["EXPECTED_COST_GAP"]
            parts.append(f"the {word} new order, {row['LIFNR']}, would cost {rp(gap)} more")
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
    sentences.append(f"{rest} more cases and the cheapest and fastest comparisons are in CASES."
                     if rest else "The cheapest and fastest comparisons are in CASES.")
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
        "DRAFTS": draft_views(supervisor.actions),
    }


# --------------------------------------------------------------------- approvals
def pending_summary(pending: list[dict[str, Any]]) -> str:
    if not pending:
        return ("No PO is waiting for approval. A draft above "
                f"{rp(APPROVAL_THRESHOLD_IDR)} appears here after a run.")
    first = pending[:2]
    lines = [f"{len(pending)} PO{'s' if len(pending) != 1 else ''} wait for the manager's "
             "approval."]
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
