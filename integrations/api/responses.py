"""Small JSON views of the agents' reports, each with a chat-ready SUMMARY (D-2, D-3).

Nothing here scores, checks policy or approves: every number is copied from the
Supplier Evaluation, Action, approval and Integrity Guard results. SUMMARY is a few
plain-English sentences for Joule; every amount in it says "(simulated)"; text the user
typed (a reason, a justification) is quoted exactly as received. Every SUMMARY ends with
NEXT_STEPS, the exact phrases the user can type next (they match the Joule scenarios in
ui/joule/). TABLE_MD and PLAN_MD are small Markdown blocks for the chat.
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


def _upper_first(text: str) -> str:
    return text[:1].upper() + text[1:]


def quoted(text: str) -> str:
    """User text exactly as received, in double quotes (A1: never rephrased)."""
    return f'"{text}"'


# ------------------------------------------------------------- next steps (B1)
def say(*phrases: str) -> str:
    """The closing NEXT_STEPS sentence: You can say "..." or "...". """
    return "You can say " + " or ".join(f'"{p}"' for p in phrases if p) + "."


def approve_phrase(ebeln: str, as_buyer: bool = False) -> str:
    return f"Approve purchase order {ebeln}" + (" as the buyer" if as_buyer else "")


def reject_phrase(ebeln: str) -> str:
    return f"Reject purchase order {ebeln}. Reason: ..."


def override_phrase(scenario: str, lifnr: str) -> str:
    return f"Override scenario {scenario} with supplier {lifnr}. Justification: ..."


def run_phrase(scenario: str = "S1") -> str:
    if scenario.upper() == "ALL":
        return RUN_ALL_PHRASE
    return f"Run GESIT on scenario {scenario}"


RUN_ALL_PHRASE = "Run GESIT on all scenarios"
STATUS_PHRASE = "Is GESIT ready?"
EVALUATION_PHRASE = "How does GESIT compare with simple rules?"
WHICH_SCENARIO = "Which scenario: S1, S2, S3 or all?"


PENDING_PHRASE = "Show pending approvals"
INTEGRITY_PHRASE = "Show integrity flags"
RESET_PHRASE = "Reset the demo"


def with_next(summary: str, next_steps: str) -> dict[str, str]:
    """SUMMARY ending with NEXT_STEPS, plus NEXT_STEPS on its own for the UI."""
    return {"SUMMARY": f"{summary} {next_steps}".strip(), "NEXT_STEPS": next_steps}


# ------------------------------------------------------------------ ranked tables
EXCLUDED_WORDS = {   # why a quote is not a candidate, as a noun phrase after a count
    supplier_score.EXCLUDED_OFF_PANEL: "off the approved panel",
    supplier_score.EXCLUDED_QUOTE_NOT_VALID: "with a quote not valid today",
    supplier_score.EXCLUDED_NO_SPARE_CAPACITY: "with no spare capacity",
    supplier_score.EXCLUDED_DISRUPTED: "with a PO flagged by Detection",
    supplier_score.EXCLUDED_BELOW_MOQ: "with spare capacity below their MOQ",
}


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


MD_COLUMNS = ("Rank", "Supplier", "Expected cost (Rp juta)", "Notes")   # A3: 4 columns
MD_SIMULATED = "All numbers simulated."
MD_MAX_BYTES = 3_000   # top-level TABLE_MD
PLAN_MD_MAX_BYTES = 1_000


def juta(amount: int | float) -> str:
    """An IDR amount in Rp juta with one decimal: 178921875 -> "178.9"."""
    return f"{amount / 1_000_000:,.1f}"


def notes(row: dict[str, Any]) -> str:
    """The Notes column: price per kg, lead days, then the marks ("Rp17,550/kg, 3 d, fastest")."""
    marks_text = short_marks(row)
    return f"Rp{row['NETPR']:,}/kg, {row['LEAD_DAYS']} d" + ("" if marks_text == "-"
                                                            else f", {marks_text}")


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


def md_rows(case: dict[str, Any], short: bool = False) -> list[dict[str, Any]]:
    """Rows for TABLE_MD, in rank order: the top 3, plus the keep row and the cheapest and
    fastest new orders when they ranked lower, so every supplier SUMMARY names is shown.
    `short` keeps only #1, the keep row, the cheapest and the fastest (B5: room for the
    explanation)."""
    rows = case["SCORING"]["CANDIDATES"]
    shown = list(rows[:1 if short else TOP_ROWS])
    extra = [next((r for r in rows if r["OPTION"] == supplier_score.OPTION_KEEP), None),
             next((r for r in rows if r.get("IS_CHEAPEST")), None),
             next((r for r in rows if r.get("IS_FASTEST")), None)]
    for row in extra:
        if row is not None and all(row is not s for s in shown):
            shown.append(row)
    return sorted(shown, key=lambda r: r["RANK"])


def table_md(case: dict[str, Any], short: bool = False, explanation: bool = True) -> str:
    """`md_rows` as a small Markdown table, then the checked model explanation (B5)."""
    shown = md_rows(case, short)
    lines = ["| " + " | ".join(MD_COLUMNS) + " |", "|" + "---|" * len(MD_COLUMNS)]
    for r in shown:
        supplier = (f"keep PO {r['KEEP_EBELN']} ({r['LIFNR']})"
                    if r["OPTION"] == supplier_score.OPTION_KEEP else r["LIFNR"])
        lines.append(f"| {r['RANK']} | {supplier} | {juta(r['EXPECTED_COST'])} | {notes(r)} |")
    if not shown:
        lines.append("| - | no candidate supplier | - | - |")
    lines += ["", MD_SIMULATED]
    text = case.get("EXPLANATION")
    if explanation and text:
        lines += ["", f"**Why:** {text}"]
    return "\n".join(lines)


def _case_heading(case: dict[str, Any]) -> str:
    reasons = ", ".join(REASON_LABELS.get(c, c) for c in case["REASON_CODES"])
    return f"### PO {case['EBELN']} ({reasons})"


def tables_md(cases: list[dict[str, Any]]) -> str:
    """Every case's TABLE_MD under a heading per PO, kept under MD_MAX_BYTES (B5): full rows
    with the explanations first; then short rows; then explanations left out from the last
    case backwards; then the cases that do not fit are named."""
    def build(short: bool, explained: int) -> str:
        return "\n\n".join(f"{_case_heading(c)}\n\n{table_md(c, short, i < explained)}"
                           for i, c in enumerate(cases))

    n = len(cases)
    for short, explained in [(False, n), (True, n)] + [(True, k) for k in range(n - 1, -1, -1)]:
        text = build(short, explained)
        if len(text.encode()) < MD_MAX_BYTES:
            return text
    parts: list[str] = []
    for i, case in enumerate(cases):
        part = f"{_case_heading(case)}\n\n{table_md(case, True, False)}"
        more = f"\n\n{n - i} more cases in CASES."
        if len("\n\n".join(parts + [part]).encode()) + len(more) > MD_MAX_BYTES:
            parts.append(more.strip())
            break
        parts.append(part)
    return "\n\n".join(parts)


def excluded_view(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"LIFNR": e["LIFNR"], "REASON": e["REASON"],
             "TEXT": EXCLUDED_WORDS.get(e["REASON"], e["REASON"])}
            for e in case["SCORING"]["EXCLUDED"]]


def case_view(case: dict[str, Any], old_po: dict[str, Any] | None = None) -> dict[str, Any]:
    table = case["SCORING"]
    out = {"EBELN": case["EBELN"], "LIFNR": case["LIFNR"], "QTY": case["QTY"],
           "NEED_BY": case["NEED_BY"],
           "REASONS": [REASON_LABELS.get(c, c) for c in case["REASON_CODES"]],
           "RECOMMENDED": recommended_label(case), "CHEAPEST": table["CHEAPEST"],
           "FASTEST": table["FASTEST"], "TABLE": table_view(case), "TABLE_MD": table_md(case),
           "EXCLUDED": excluded_view(case), "CANNOT_COVER": table.get("CANNOT_COVER", []),
           "RESERVED_FOR": case.get("RESERVED_FOR", []),
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


def superseded_views(actions: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [s for c in (actions or {}).get("CASES", []) for s in c.get("SUPERSEDED", [])]


def plan_view(plan: dict[str, Any]) -> dict[str, Any]:
    return {"PLAN_ID": plan["PLAN_ID"], "TRIGGER": plan["TRIGGER"],
            "STEPS": [{**{k: s[k] for k in ("NO", "AGENT", "STATUS", "SECONDS", "RESULT")},
                       "SAP_RPT": s.get("SAP_RPT"),   # "SAP-RPT: called" or "SAP-RPT: cached"
                       "CALLS": [{k: c[k] for k in ("KIND", "WHAT", "SECONDS", "RESULT")}
                                 for c in s.get("CALLS", [])]}
                      for s in plan["STEPS"]],
            "NOT_IN_LIVE_FLOW": plan["NOT_IN_LIVE_FLOW"], "OUTCOME": plan["OUTCOME"],
            "CASE_OUTCOMES": plan["CASES"], "TOTAL_SECONDS": plan.get("TOTAL_SECONDS")}


def plan_md(plan: dict[str, Any]) -> str:
    """B3: the supervisor's steps for the chat: agent, status, seconds, SAP-RPT called or
    cached, model texts; under PLAN_MD_MAX_BYTES."""
    total = plan.get("TOTAL_SECONDS")
    blocks = [f"**Agents at work** (supervisor plan {plan['PLAN_ID'] or '-'}, Strands Graph"
              + (f", {total:.1f} s" if total is not None else "") + ")"]
    items = []
    for s in plan["STEPS"]:
        bits = [s["STATUS"] + (f" in {s['SECONDS']:.1f} s" if s["SECONDS"] is not None else "")]
        if s.get("SAP_RPT"):
            bits.append(s["SAP_RPT"])
        texts = sum(1 for c in s.get("CALLS", []) if c["KIND"] == "GEN_AI_HUB")
        if texts:
            bits.append(f"{texts} model text{'s' if texts != 1 else ''}")
        items.append(f"- Step {s['NO']}, {s['AGENT']}: {', '.join(bits)}")
    blocks.append("\n".join(items))   # a list after a blank line, so the chat keeps the lines
    blocks.append("Evaluation and Learning: offline (Monte Carlo, eval/monte_carlo.py).")
    outcome = plan.get("OUTCOME")
    if outcome:
        blocks.append(f"Stop: {outcome['TEXT']}")
    text = "\n\n".join(blocks)
    while len(text.encode()) >= PLAN_MD_MAX_BYTES and len(blocks) > 2:
        blocks.pop()
        text = "\n\n".join(blocks)
    return text


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


def no_backup_sentence(case: dict[str, Any]) -> str | None:
    """C1, C2: when no new order is possible, why (counts per exclusion reason)."""
    table = case["SCORING"]
    if any(r["OPTION"] == supplier_score.OPTION_NEW_PO for r in table["CANDIDATES"]):
        return None
    counts: dict[str, int] = {}
    for e in table["EXCLUDED"]:
        counts[e["REASON"]] = counts.get(e["REASON"], 0) + 1
    parts = [f"{n} {EXCLUDED_WORDS.get(reason, reason)}" for reason, n in counts.items()]
    cannot = table.get("CANNOT_COVER") or []
    if cannot:
        parts.insert(0, f"{len(cannot)} that cannot cover {case['QTY']:,.0f} kg, not even as "
                        "a split")
    return "No backup supplier can take the order: " + (", ".join(parts) or "none quoted") + "."


def _next_step(case_ebeln: str, actions: dict[str, Any] | None, inline: bool = False) -> str:
    case = next((c for c in (actions or {}).get("CASES", []) if c["EBELN"] == case_ebeln), None)
    if case is None or not case["ORDERS"]:
        return "nothing was drafted, the buyer decides"
    orders = case["ORDERS"]
    names = _and([f"PO {o['EBELN']} at {rp(o['NETWR'])}" for o in orders if o["EBELN"]] or ["-"])
    nexts = {o["NEXT"] for o in orders}
    superseded = ", ".join(s["EBELN"] for s in case.get("SUPERSEDED", []))
    tail = f", superseding draft PO {superseded}" if inline and superseded else ""
    if action.NEXT_MANAGER in nexts:
        ids = _and([o["APPROVAL_REQUEST_ID"] for o in orders if o["APPROVAL_REQUEST_ID"]])
        return (f"draft {names} is above the approval threshold of "
                f"{rp(APPROVAL_THRESHOLD_IDR)} and waits for the manager ({ids}){tail}")
    if action.NEXT_BLOCKED in nexts:
        return f"draft {names} is blocked by policy, the buyer and the manager decide{tail}"
    return f"draft {names} is within policy and waits for the buyer's one-click approval{tail}"


def next_step_sentence(case_ebeln: str, actions: dict[str, Any] | None) -> str:
    """What happens next for one case, as one sentence."""
    return _upper_first(_next_step(case_ebeln, actions)) + "."


def supersede_sentences(actions: dict[str, Any] | None, case_ebeln: str | None = None) -> list[str]:
    """A2: "Draft PO X supersedes draft PO Y." per superseded draft."""
    out = []
    for case in (actions or {}).get("CASES", []):
        if case_ebeln is not None and case["EBELN"] != case_ebeln:
            continue
        for s in case.get("SUPERSEDED", []):
            out.append(f"Draft PO {_and(s['SUPERSEDED_BY'])} supersedes draft PO {s['EBELN']}.")
    return out


def run_next_steps(evaluation: dict[str, Any] | None, actions: dict[str, Any] | None,
                   scenario_of: dict[str, str], scenario: str) -> str:
    """B1: what to type next after a run: approve or reject a draft above the threshold,
    the buyer's approval for a draft within policy, or a run or an override after keep."""
    orders = [(c, o) for c in (actions or {}).get("CASES", []) for o in c["ORDERS"]
              if o["EBELN"]]
    manager = next((o for _, o in orders if o["NEXT"] == action.NEXT_MANAGER), None)
    buyer = next((o for _, o in orders if o["NEXT"] == action.NEXT_BUYER), None)
    phrases = []
    if manager:
        phrases += [approve_phrase(manager["EBELN"]), reject_phrase(manager["EBELN"])]
    if buyer:
        phrases.append(approve_phrase(buyer["EBELN"], as_buyer=True))
    if phrases:
        return say(*phrases)
    cases = (evaluation or {}).get("CASES", [])
    for case in cases:
        new = next((r for r in case["SCORING"]["CANDIDATES"]
                    if r["OPTION"] == supplier_score.OPTION_NEW_PO), None)
        sid = scenario_of.get(case["EBELN"])
        if new is not None and sid:
            return say(run_phrase(sid if scenario.upper() == "ALL" else scenario),
                       override_phrase(sid, new["LIFNR"]))
    return say(run_phrase(scenario if scenario.upper() in ("S1", "S2", "S3") else "S1"),
               PENDING_PHRASE)


def run_summary(report: dict[str, Any] | None, evaluation: dict[str, Any] | None,
                actions: dict[str, Any] | None) -> str:
    """The recommendation, cheapest and fastest, what happens next (without NEXT_STEPS)."""
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
                     _comparison(case) or no_backup_sentence(case),
                     next_step_sentence(case["EBELN"], actions),
                     *supersede_sentences(actions, case["EBELN"])]
        return " ".join(s for s in sentences if s)
    shown = cases[:3] if len(cases) <= 3 else cases[:2]
    sentences = [f"PO {c['EBELN']}: {_recommendation(c)}; "
                 f"{_next_step(c['EBELN'], actions, inline=True)}." for c in shown]
    rest = len(cases) - len(shown)
    if rest:
        sentences.append(f"{rest} more cases are in CASES.")
    return " ".join(sentences)


def run_response(supervisor: Any) -> dict[str, Any]:
    """The /run JSON for one finished supervisor run."""
    old = {c["EBELN"]: c.get("OLD_PO") for c in (supervisor.actions or {}).get("CASES", [])}
    cases = (supervisor.evaluation or {}).get("CASES", [])
    scenario = supervisor.plan["TRIGGER"]["SCENARIO"]
    scenario_of = {n["ebeln"]: n["id"] for n in supervisor.adapter.scenarios()}
    summary = run_summary(supervisor.report, supervisor.evaluation, supervisor.actions)
    next_steps = run_next_steps(supervisor.evaluation, supervisor.actions, scenario_of, scenario)
    return {
        "DATA_NOTE": DATA_NOTE,
        "SCENARIO": scenario,
        **with_next(summary, next_steps),
        "PLAN": plan_view(supervisor.plan),
        "PLAN_MD": plan_md(supervisor.plan),
        "FLAGGED_EBELN": (supervisor.report or {}).get("FLAGGED_EBELN", []),
        "CASES": [case_view(c, old.get(c["EBELN"])) for c in cases],
        "TABLE_MD": tables_md(cases),
        "DRAFTS": draft_views(supervisor.actions),
        "SUPERSEDED": superseded_views(supervisor.actions),
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


def pending_next_steps(pending: list[dict[str, Any]]) -> str:
    if not pending:
        return say(run_phrase("S1"))
    return say(approve_phrase(pending[0]["EBELN"]), reject_phrase(pending[0]["EBELN"]))


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


def deny_next_steps(decision: dict[str, Any], ebeln: str) -> str:
    if decision["POLICY"] in ("POL-1", "POL-4"):
        return say(approve_phrase(ebeln), PENDING_PHRASE)   # the manager decides
    return say(PENDING_PHRASE, run_phrase("S1"))


def approve_summary(po: dict[str, Any], by: str, role: str = "manager") -> str:
    """A5: a manager's and a buyer's approval both end in RELEASED (`release_po`, FR-ACT-2,
    FR-ACT-3); only who approved differs, and SUMMARY says so."""
    who = (f"the buyer {by}, one-click within policy" if role == "buyer"
           else f"the procurement manager {by}")
    return (f"PO {po['EBELN']} (supplier {po['LIFNR']}, NETWR {rp(po['NETWR'])}) is RELEASED, "
            f"approved by {who}. The buyer now changes the old PO {po['REPLACES_EBELN']} in "
            "S/4 as the draft says.")


def reject_summary(po: dict[str, Any], option: dict[str, Any] | None, reason: str) -> str:
    sentences = [f"PO {po['EBELN']} (supplier {po['LIFNR']}, NETWR {rp(po['NETWR'])}) is "
                 f"rejected by {po['REJECTED_BY']} and will never be released.",
                 f"Reason recorded: {quoted(reason)}.",
                 f"The old PO {po['REPLACES_EBELN']} is untouched, so buyer {po['ERNAM']} "
                 "still has it."]
    if option:
        sentences.append(f"Next option for the buyer, not drafted: rank {option['RANK']}, "
                         f"{option_label(option)}, expected cost "
                         f"{rp(option['EXPECTED_COST'])}.")
    else:
        sentences.append("No other ranked option was saved with the draft.")
    return " ".join(sentences)


def reject_next_steps(po: dict[str, Any], option: dict[str, Any] | None,
                      scenario_of: dict[str, str]) -> str:
    sid = scenario_of.get(po["REPLACES_EBELN"], "S1")
    if option and option.get("OPTION") == supplier_score.OPTION_NEW_PO:
        return say(override_phrase(sid, option["LIFNR"]))
    return say(run_phrase(sid), PENDING_PHRASE)


def short_reason_summary(what: str, text: str, chars: int, minimum: int, tail: str) -> str:
    """A1: 'Reason "too high" has 8 characters, at least 20 are needed. ...'"""
    return (f"{what} {quoted(text)} has {chars} character{'s' if chars != 1 else ''}, "
            f"at least {minimum} are needed. {tail}")


# --------------------------------------------------------------------- integrity
def flag_sentence(flag: dict[str, Any]) -> str:
    """A3, B4: one flagged pair by name, neutral wording (FR-INT-4)."""
    rate = (f"{flag['RATIO']:.1f}x the expected rate" if flag.get("RATIO") is not None
            else "while other buyers place none with this supplier")
    return (f"Flagged for review: {flag['BUYER']} with {flag['SUPPLIER']}, {flag['K']} of "
            f"{flag['N_B']} POs, {rate} (simulated).")


NEUTRAL_NOTE = ("This is a statistic for the manager to review, not a finding; it never blocks "
                "a PO or a buyer.")


def flags_view(flags: dict[str, Any]) -> dict[str, Any]:
    return {k: flags[k] for k in ("RULE", "PAIRS_TESTED", "PAIRS_FLAGGED", "FLAGS",
                                  "OVERRIDES_TOTAL", "OVERRIDES_BY_BUYER", "OVERRIDES_BY_CHOSEN")}


def pair_text(flag: dict[str, Any]) -> str:
    rate = (f"{flag['RATIO']:.1f}x the expected rate" if flag.get("RATIO") is not None
            else "while other buyers place none with this supplier")
    return (f"{flag['BUYER']} with {flag['SUPPLIER']}, {flag['K']} of {flag['N_B']} POs, {rate} "
            "(simulated)")


def flags_sentences(flags: dict[str, Any], buyer: str | None = None,
                    supplier: str | None = None) -> list[str]:
    """A4: with an override, first whether that buyer's own pair with the chosen supplier is
    flagged, then the other flagged pairs, so another buyer's flag never reads as theirs."""
    out = [f"{flags['PAIRS_TESTED']} buyer and supplier pairs tested, {flags['PAIRS_FLAGGED']} "
           "flagged for review."]
    flagged = flags["FLAGS"]
    if buyer and supplier:
        own = next((f for f in flagged if f["BUYER"] == buyer and f["SUPPLIER"] == supplier), None)
        out.append(f"{buyer} with {supplier} is flagged for review: {pair_text(own)}."
                   if own else f"{buyer} with {supplier} is not flagged.")
        others = [f for f in flagged if f is not own]
        if others:
            out.append("Other flagged pairs: " + "; ".join(pair_text(f) for f in others) + ".")
    else:
        out += [flag_sentence(f) for f in flagged]
    if flagged:
        out.append(NEUTRAL_NOTE)
    return out


def integrity_md(flags: dict[str, Any], overrides: list[dict[str, Any]]) -> str:
    """B4: the flagged pairs and the latest overrides as small Markdown tables."""
    lines = ["| Buyer and supplier | POs with the supplier | Share against other buyers | "
             "Ratio |", "|---|---|---|---|"]
    for f in flags["FLAGS"]:
        ratio = "-" if f.get("RATIO") is None else f"{f['RATIO']:.1f}x"
        lines.append(f"| {f['BUYER']} with {f['SUPPLIER']} | {f['K']} of {f['N_B']} | "
                     f"{f['SHARE_B']:.1%} against {f['SHARE_OTHERS']:.1%} | {ratio} |")
    if not flags["FLAGS"]:
        lines.append("| - | no pair meets the rule | - | - |")
    lines += ["", f"Pairs tested: {flags['PAIRS_TESTED']}. Flags are for review, never a block."]
    if overrides:
        lines += ["", "| Override | Buyer, PO | Choice and justification | Extra cost (Rp juta) |",
                  "|---|---|---|---|"]
        for o in overrides:
            text = o["JUSTIFICATION"]
            text = text if len(text) <= 60 else text[:57] + "..."
            lines.append(f"| {o['OVERRIDE_ID']} | {o['BUYER']}, {o['EBELN']} | {o['CHOSEN']} "
                         f"instead of {o['RECOMMENDED']}: \"{text.replace('|', '/')}\" | "
                         f"{juta(o['COST_DIFFERENCE'])} |")
    lines += ["", MD_SIMULATED]
    return "\n".join(lines)


def integrity_summary(flags: dict[str, Any], overrides: list[dict[str, Any]]) -> str:
    """A4: the latest override first, with its own pair, then the other flagged pairs."""
    if not overrides:
        return " ".join(flags_sentences(flags) + ["No override is logged."])
    last = overrides[-1]
    head = [f"Overrides logged: {flags['OVERRIDES_TOTAL']}; the latest, {last['OVERRIDE_ID']}, "
            f"{last['BUYER']} chose {last['CHOSEN']} instead of {last['RECOMMENDED']}."]
    return " ".join(head + flags_sentences(flags, last["BUYER"], last["CHOSEN"]))


def override_summary(*, choice: dict[str, Any] | None, record: dict[str, Any] | None,
                     recommended: str | None, chosen: str, justification: str,
                     next_sentence: str, supersede: list[str], flags: dict[str, Any]) -> str:
    """A3: the override, its expected cost gap against #1, the quoted justification, the
    draft, and each flagged pair by name, in neutral words (FR-INT-1, FR-INT-2, FR-INT-4)."""
    if record is not None:
        sentences = [f"Override {record['OVERRIDE_ID']} logged: {record['CHOSEN']} instead of "
                     f"the recommended {record['RECOMMENDED']}.",
                     f"{record['CHOSEN']} has an expected cost Rp{record['COST_DIFFERENCE']:,} "
                     f"higher than the recommended {record['RECOMMENDED']} (simulated).",
                     f"Justification recorded: {quoted(justification)}."]
    else:
        sentences = [f"{chosen} is the recommended option, so no justification was needed and "
                     "nothing was logged."]
    sentences.append(next_sentence)
    sentences += supersede
    sentences += flags_sentences(flags, record["BUYER"] if record else None,
                                 record["CHOSEN"] if record else None)
    return " ".join(sentences)


# ------------------------------------------------------------------------ status
def status_summary(warm: dict[str, Any], open_drafts: int) -> str:
    """B2: is GESIT ready for the demo?"""
    drafts = f"{open_drafts} open draft{'s' if open_drafts != 1 else ''}"
    failed = [s for s in warm.get("STEPS", []) if str(s["RESULT"]).startswith("failed")]
    state = warm.get("STATUS")
    if state in ("starting", "running"):
        done = len(warm.get("STEPS", []))
        return (f"GESIT is warming up ({done} of 3 steps done), {drafts}. Ask again in about "
                "20 seconds.")
    if state == "off":
        return f"GESIT is up: warm-up off, {drafts}. The first run calls SAP-RPT and takes longer."
    if failed:
        return (f"GESIT is up, but the warm-up step {failed[0]['STEP']} failed, {drafts}: "
                f"{failed[0]['RESULT']}.")
    return f"GESIT is ready: warm-up done, {drafts}."


# -------------------------------------------------------------------- evaluation
EVAL_NAMES = {"GESIT_LEARNING": "GESIT with learning", "GESIT_RPT_ONLY": "GESIT with SAP-RPT only",
              "ALWAYS_CHEAPEST": "always cheapest", "ALWAYS_FASTEST": "always fastest",
              "KEEP_CURRENT_PO": "keep the PO"}
ABOUT_EQUAL_SHARE = 0.03   # a mean gap within 3% of the baseline's cost reads "about equal"


def juta_sim(amount_juta: float) -> str:
    return f"Rp{amount_juta:,.1f} juta (simulated)"


def evaluation_summary(data: dict[str, Any]) -> str:
    """B3: the comparison with simple rules, in the words of PRD section 4 ("tested against
    baselines in simulation"), every figure from the eval/ summary file."""
    tracked = data["TRACKED_TOTAL_JUTA"]
    worlds = data["WORLDS"]
    vs = worlds["VERSUS"]["GESIT_LEARNING"]
    n = worlds["COUNT"]

    def against(key: str) -> str:
        v = vs[key]
        higher = v["OF"] - v["CHEAPER_IN"] - v["EQUAL_IN"]
        mean = v["MEAN_DIFF_JUTA"]
        direction = "lower" if mean < 0 else "higher"
        about = abs(mean) <= ABOUT_EQUAL_SHARE * data["WORLDS"]["MEAN_TOTAL_JUTA"][key]
        return (f"{'about equal to' if about else 'against'} {EVAL_NAMES[key]}: cheaper in "
                f"{v['CHEAPER_IN']}, equal in {v['EQUAL_IN']}, higher in {higher} of {n} worlds, "
                f"{juta_sim(abs(mean))} {direction} on average")

    return " ".join([
        f"GESIT was tested against simple rules in simulation (seed {data['SEED']}, "
        f"{data['RUNS']:,} runs per strategy, {n} simulated worlds).",
        f"With learning it costs less than always cheapest in {vs['ALWAYS_CHEAPEST']['CHEAPER_IN']} "
        f"of {n} worlds and less than keeping the PO in {vs['KEEP_CURRENT_PO']['CHEAPER_IN']} "
        f"of {n}; it is {against('ALWAYS_FASTEST')}.",
        f"Tracked data, total over S1 to S3: GESIT with learning "
        f"{juta_sim(tracked['GESIT_LEARNING'])}, always fastest "
        f"{juta_sim(tracked['ALWAYS_FASTEST'])}, always cheapest "
        f"{juta_sim(tracked['ALWAYS_CHEAPEST'])}.",
        "GESIT matches the best simple rule on cost without being told which rule to use, beats "
        "always-cheapest and keeping the PO, and adds what a rule cannot: an explanation, policy "
        "checks, a human approval and an audit trail.",
    ])


def evaluation_md(data: dict[str, Any]) -> str:
    """B3: per strategy the tracked total and the 11-world counts for GESIT with learning."""
    tracked = data["TRACKED_TOTAL_JUTA"]
    vs = data["WORLDS"]["VERSUS"]["GESIT_LEARNING"]
    n = data["WORLDS"]["COUNT"]
    lines = ["| Strategy | Tracked total (Rp juta) | GESIT with learning cheaper in | Mean gap "
             "(Rp juta) |", "|---|---|---|---|"]
    for key, name in EVAL_NAMES.items():
        v = vs.get(key)
        # GESIT with learning is the row the others are compared against, not missing data
        counts = (f"{v['CHEAPER_IN']} of {n} worlds, equal in {v['EQUAL_IN']}" if v
                  else "baseline (compared against)")
        gap = (f"{v['MEAN_DIFF_JUTA']:+,.1f}" if v else "0.0")
        lines.append(f"| {name} | {tracked[key]:,.1f} | {counts} | {gap} |")
    lines += ["", "Mean gap = GESIT with learning minus the strategy, averaged over the worlds; "
                  "negative means GESIT costs less.", MD_SIMULATED]
    return "\n".join(lines)
