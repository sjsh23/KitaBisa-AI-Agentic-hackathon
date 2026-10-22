"""Action agent (FR-ACT-1 to FR-ACT-3, POL-1 to POL-4).

Runs after Supplier Evaluation, in a fixed order (FR-SUP-3, deterministic path).
For every case with a recommendation:

1. `draft_po` for the #1 supplier (FR-ACT-1); a split recommendation gets one
   draft per supplier, so the kilograms add up to the replacement quantity.
   ERNAM is the buyer of the PO being replaced. The agent never picks another
   option itself: a buyer's own choice (`choices`) is drafted only with the id of
   an override the Integrity Guard logged after the justification check (FR-INT-1,
   FR-INT-2, `tools.integrity_tools`).
2. Policy check: may that buyer release the draft (`policy.authorize`, the same
   check `release_po` runs)? If yes, the draft waits for the buyer's one-click
   approval (FR-ACT-3). If POL-1 denies it (NETWR above the threshold), the agent
   calls `submit_for_approval` and hands back to the manager (FR-ACT-2, FR-SUP-2).

When keeping the current PO ranks #1 (proposed, docs/prd_change_proposals.md
item 4), nothing is drafted and the case goes back to the buyer.

Every case also says what happens to the old PO (OLD_PO): cancel it, reduce it to
the part that still arrives on time (capacity shortfall), or keep it. GESIT does
not change the old PO itself: PRD section 8 has no tool for it, so the buyer does
it in S/4 (proposal item 5).

Each draft also keeps the case's ranked table (RANKING, DRAFT_RANK) and its old PO
line (OLD_PO_TEXT), so a manager's reject hours later can name the next option for
the buyer from the same table, without scoring again (`tools.action.reject_po`,
proposal item 11).

One open draft per flagged PO (proposal item 14): when a case is drafted again (another
run, or the buyer's override) while an older draft for the same flagged PO is still DRAFT
or PENDING_APPROVAL, the older one becomes SUPERSEDED, with an event naming the new draft,
so a manager can never approve both and buy twice. The parts of one split draft never
supersede each other.

The agent never releases a PO: `release_po` is an approval tool and POL-4 denies
it to agents. Only a human principal releases (`human_approvals` stands in for
the clicks in the terminal). No LLM is used here; every value comes from the
evaluation report and the tools.
"""
from __future__ import annotations

from typing import Any

from adapters import CsvAdapter, PoStore, default_adapter
from adapters.po_store import OPEN_STATUSES, STATUS_RELEASED, STATUS_SUPERSEDED
from policy import AGENT, APPROVAL_THRESHOLD_IDR, Principal, authorize, buyer, manager
from scoring import disruption, supplier_score
from tools import draft_po, release_po, submit_for_approval
from tools.integrity_tools import find_option, recommended_label

AGENT_NAME = "action"

NEXT_BUYER = "BUYER_ONE_CLICK"       # within policy for the buyer (FR-ACT-3)
NEXT_MANAGER = "MANAGER_APPROVAL"    # POL-1 denied the buyer, routed to the manager (FR-ACT-2)
NEXT_BLOCKED = "BLOCKED"             # denied for another reason, nobody can release it
NEXT_DONE = "RELEASED"

OLD_PO_CANCEL = "CANCEL"
OLD_PO_REDUCE = "REDUCE"
OLD_PO_KEEP = "KEEP"
OLD_PO_BY = "Done by the buyer in S/4; GESIT does not change the old PO."


def order_parts(row: dict[str, Any]) -> list[dict[str, Any]]:
    """The orders behind one ranked row: one, or two for a split (FR-SEL-2)."""
    parts = [{"LIFNR": row["LIFNR"], "MENGE": row["MENGE"], "NETPR": row["NETPR"],
              "EINDT": row["ARRIVAL"]}]
    split = row.get("SPLIT_WITH")
    if split:
        parts.append({"LIFNR": split["LIFNR"], "MENGE": split["MENGE"], "NETPR": split["NETPR"],
                      "EINDT": split["ARRIVAL"]})
    return parts


RANKING_FIELDS = ("RANK", "OPTION", "LIFNR", "KEEP_EBELN", "MENGE", "NETPR", "ARRIVAL",
                  "EXPECTED_STOCKOUT_DAYS", "EXPECTED_COST")


def ranking_snapshot(case: dict[str, Any]) -> list[dict[str, Any]]:
    """The case's ranked rows, as saved with each draft (numbers copied, not recomputed)."""
    rows = []
    for row in case["SCORING"]["CANDIDATES"]:
        split = row.get("SPLIT_WITH")
        rows.append({**{k: row.get(k) for k in RANKING_FIELDS},
                     "SPLIT_WITH": {k: split[k] for k in ("LIFNR", "MENGE", "NETPR", "ARRIVAL")}
                     if split else None})
    return rows


def reason_text(case: dict[str, Any], part_no: int, n_parts: int,
                row: dict[str, Any] | None = None, override_id: str | None = None) -> str:
    """Why the draft exists, for the PO text and the audit trail (no names, simulated)."""
    table = case["SCORING"]
    row = row or table["CANDIDATES"][0]
    reasons = ", ".join(c.lower().replace("_", " ") for c in case["REASON_CODES"])
    text = (f"Replaces PO {case['EBELN']} ({reasons}). GESIT rank {row['RANK']} of "
            f"{len(table['CANDIDATES'])}, expected cost Rp{row['EXPECTED_COST']:,} (simulated).")
    if override_id:
        text += f" Buyer's choice, override {override_id}."
    if n_parts > 1:
        text += f" Split order, part {part_no} of {n_parts}."
    return text


def _reason(current: dict[str, Any], code: str) -> dict[str, Any] | None:
    return next((r for r in current["REASONS"] if r["CODE"] == code), None)


def old_po_action(case: dict[str, Any]) -> dict[str, Any] | None:
    """What happens to the flagged PO, in words built from the numbers (simulated).

    Keep it when keeping ranked #1; reduce it to the on-time part after a capacity
    shortfall; otherwise cancel it once the replacement is released.
    """
    current = case.get("CURRENT_PO")
    rows = case["SCORING"]["CANDIDATES"]
    if current is None or not rows:
        return None
    ebeln, menge = current["EBELN"], current["MENGE"]
    price = _reason(current, disruption.REASON_PRICE_INCREASE)
    if rows[0].get("OPTION") == supplier_score.OPTION_KEEP:
        new = next((r for r in rows if r.get("OPTION") != supplier_score.OPTION_KEEP), None)
        text = (f"Old PO {ebeln}: keep it as it is, expected cost "
                f"Rp{rows[0]['EXPECTED_COST']:,}")
        if new:
            text += (f" against Rp{new['EXPECTED_COST']:,} for the best new order, from "
                     f"{new['LIFNR']}")
        text += " (simulated)."
        if price:
            text += (f" The buyer accepts the revised price Rp{price['NEW_NETPR']:,} per kg "
                     f"(was Rp{price['PO_NETPR']:,}) or negotiates it.")
        return {"EBELN": ebeln, "ACTION": OLD_PO_KEEP, "MENGE": menge, "NEW_MENGE": menge,
                "TEXT": f"{text} {OLD_PO_BY}"}

    shortfall = _reason(current, disruption.REASON_CAPACITY_SHORTFALL)
    if shortfall:
        new_menge = menge - case["QTY"]
        text = (f"Old PO {ebeln}: reduce from {menge:,.0f} to {new_menge:,.0f} kg (the part "
                f"arriving {current['PARTIAL_ARRIVAL'] or current['ARRIVAL']}) once the "
                f"replacement is released; the {case['QTY']:,.0f} kg remainder moves to the "
                "new order.")
        return {"EBELN": ebeln, "ACTION": OLD_PO_REDUCE, "MENGE": menge, "NEW_MENGE": new_menge,
                "TEXT": f"{text} {OLD_PO_BY}"}

    delay = _reason(current, disruption.REASON_DELAY_NOTICE)
    if delay:
        timing = current["ARRIVAL_VS_STOCKOUT"].removeprefix("arrives ")
        why = f"it would arrive {delay['NEW_EINDT']}, {timing}"
    elif price:
        why = (f"supplier {current['LIFNR']} now asks Rp{price['NEW_NETPR']:,} per kg instead of "
               f"Rp{price['PO_NETPR']:,}")
    else:
        why = f"P(late) {current['P_LATE']:.2f}"
    text = f"Old PO {ebeln}: cancel its {menge:,.0f} kg once the replacement is released ({why})."
    return {"EBELN": ebeln, "ACTION": OLD_PO_CANCEL, "MENGE": menge, "NEW_MENGE": 0.0,
            "TEXT": f"{text} {OLD_PO_BY}"}


class ActionAgent:
    """Drafts the recommended replacement POs and routes them by policy."""

    def __init__(self, adapter: CsvAdapter | None = None, store: PoStore | None = None,
                 principal: Principal = AGENT) -> None:
        self.adapter = adapter or default_adapter()
        self.store = store or PoStore(self.adapter)
        self.principal = principal

    def act(self, evaluation: dict[str, Any],
            choices: dict[str, dict[str, Any]] | None = None,
            buyer_id: str | None = None) -> dict[str, Any]:
        """Drafts and policy routing for every case of a Supplier Evaluation report.

        `choices` maps EBELN to the buyer's choice {"CHOSEN": option label, "OVERRIDE_ID":
        id or None}; a choice other than #1 without an override id raises ValueError, so
        no draft exists before the justification was checked and logged (FR-INT-1).
        `buyer_id` replaces the buyer of the old PO as the one acting (ERNAM of the drafts).
        """
        choices = choices or {}
        buyers = {line["EBELN"]: line["ERNAM"] for line in self.adapter.po_lines()}
        steps: list[dict[str, Any]] = []
        cases = []
        for case in evaluation["CASES"]:
            owner_id = buyer_id or buyers[case["EBELN"]]
            out = {"EBELN": case["EBELN"], "LIFNR": case["LIFNR"], "BUYER": owner_id,
                   "QTY": case["QTY"], "ORDERS": [], "NOTE": None,
                   "OLD_PO": old_po_action(case), "CHOSEN": None, "OVERRIDE_ID": None,
                   "SUPERSEDED": []}
            if not case["SCORING"]["CANDIDATES"]:
                out["NOTE"] = "no candidate supplier, nothing drafted; handed back to the buyer"
                cases.append(out)
                continue
            choice = choices.get(case["EBELN"])
            row, override_id = self._chosen_row(case, choice)
            out["CHOSEN"] = choice["CHOSEN"] if choice else None
            out["OVERRIDE_ID"] = override_id
            if row.get("OPTION") == supplier_score.OPTION_KEEP:
                out["NOTE"] = (f"the buyer chose to keep the current PO (override {override_id}), "
                               "nothing drafted" if override_id else
                               "keeping the current PO ranks #1, nothing drafted; handed back "
                               "to the buyer")
                if override_id:   # the keep text is about #1, so say it in the buyer's terms
                    out["OLD_PO"] = {**(out["OLD_PO"] or {}), "ACTION": OLD_PO_KEEP,
                                     "TEXT": f"Old PO {case['EBELN']}: kept as it is, by the "
                                             f"buyer's choice. {OLD_PO_BY}"}
                cases.append(out)
                continue
            parts = order_parts(row)
            saved = {"RANKING": ranking_snapshot(case), "DRAFT_RANK": row["RANK"],
                     "OLD_PO_TEXT": (out["OLD_PO"] or {}).get("TEXT")}
            older = self.open_drafts_for(case["EBELN"])   # before this case's new drafts
            for i, part in enumerate(parts, 1):
                out["ORDERS"].append(self._draft_and_route(
                    case, part, reason_text(case, i, len(parts), row, override_id),
                    buyer(owner_id), steps, saved))
            new = [o["EBELN"] for o in out["ORDERS"] if o["EBELN"]]
            if new:
                out["SUPERSEDED"] = self._supersede(older, new, steps)
            cases.append(out)
        return {
            "AGENT": AGENT_NAME,
            "DATA_NOTE": "simulated data",
            "MATNR": evaluation["MATNR"],
            "TODAY": evaluation["TODAY"],
            "PRINCIPAL": self.principal.id,
            "APPROVAL_THRESHOLD_IDR": APPROVAL_THRESHOLD_IDR,
            "STEPS": steps,
            "CASES": cases,
            "HANDED_BACK": handed_back(cases),   # FR-SUP-2: a human decides next
        }

    def open_drafts_for(self, replaces: str) -> list[str]:
        """EBELNs of the drafts still DRAFT or PENDING_APPROVAL that replace `replaces`."""
        return [po["EBELN"] for po in self.store.all()
                if po.get("REPLACES_EBELN") == replaces and po["STATUS"] in OPEN_STATUSES]

    def _supersede(self, older: list[str], new: list[str],
                   steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Mark the older open drafts SUPERSEDED by the new ones (never releasable)."""
        done = []
        for ebeln in older:
            by = ", ".join(new)
            self.store.update(ebeln, {"TOOL": "supersede", "BY": self.principal.id,
                                      "ROLE": self.principal.role, "RESULT": STATUS_SUPERSEDED,
                                      "POLICY": None, "SUPERSEDED_BY": new,
                                      "REASON": f"draft PO {by} replaces the same flagged PO"},
                              STATUS=STATUS_SUPERSEDED, SUPERSEDED_BY=new)
            steps.append({"TOOL": "supersede", "INPUT": {"ebeln": ebeln, "by": new},
                          "RESULT": STATUS_SUPERSEDED})
            done.append({"EBELN": ebeln, "SUPERSEDED_BY": new})
        return done

    @staticmethod
    def _chosen_row(case: dict[str, Any],
                    choice: dict[str, Any] | None) -> tuple[dict[str, Any], str | None]:
        """The row to act on: #1, or the buyer's choice backed by a logged override."""
        best = case["SCORING"]["CANDIDATES"][0]
        if not choice:
            return best, None
        row = find_option(case, choice["CHOSEN"])
        if row is None:
            raise ValueError(f"{choice['CHOSEN']} is not one of the ranked options for PO "
                             f"{case['EBELN']}")
        if row is best:
            return best, None
        if not choice.get("OVERRIDE_ID"):
            raise ValueError(f"choosing {choice['CHOSEN']} instead of {recommended_label(case)} "
                             "needs a logged override with a justification first (FR-INT-1)")
        return row, choice["OVERRIDE_ID"]

    def _draft_and_route(self, case: dict[str, Any], part: dict[str, Any], reason: str,
                         owner: Principal, steps: list[dict[str, Any]],
                         saved: dict[str, Any] | None = None) -> dict[str, Any]:
        """One draft (FR-ACT-1), the buyer's release check, and approval routing (FR-ACT-2)."""
        drafted = draft_po(part["LIFNR"], case["SCORING"]["MATNR"], part["MENGE"], part["NETPR"],
                           part["EINDT"], reason, store=self.store, ernam=owner.id,
                           replaces=case["EBELN"], principal=self.principal)
        steps.append({"TOOL": "draft_po",
                      "INPUT": {"lifnr": part["LIFNR"], "qty": part["MENGE"],
                                "netpr": part["NETPR"], "eindt": part["EINDT"]},
                      "RESULT": f"{drafted['RESULT']} {drafted['EBELN'] or ''}".strip()})
        if drafted["RESULT"] != "DRAFTED":
            return {**part, "EBELN": None, "NETWR": None, "STATUS": "NOT_DRAFTED",
                    "RELEASE_CHECK": drafted["POLICY_DECISION"], "NEXT": NEXT_BLOCKED,
                    "APPROVAL_REQUEST_ID": None}

        po = drafted["PO"]
        if saved:   # the ranked table stays with the PO until the manager decides
            self.store.update(po["EBELN"], None, **saved)
        check = authorize("release_po", owner, po)          # same rule release_po applies
        steps.append({"TOOL": "policy_check",
                      "INPUT": {"tool": "release_po", "principal": owner.id, "ebeln": po["EBELN"]},
                      "RESULT": "allowed" if check.allowed else f"denied by {check.policy}"})
        order = {"EBELN": po["EBELN"], "LIFNR": po["LIFNR"], "MENGE": po["MENGE"],
                 "NETPR": po["NETPR"], "NETWR": po["NETWR"], "EINDT": po["EINDT"],
                 "STATUS": po["STATUS"], "RELEASE_CHECK": check.as_dict(),
                 "APPROVAL_REQUEST_ID": None}
        if check.allowed:
            return {**order, "NEXT": NEXT_BUYER}
        if check.policy != "POL-1":
            return {**order, "NEXT": NEXT_BLOCKED}
        submitted = submit_for_approval(po["EBELN"], store=self.store, principal=self.principal)
        steps.append({"TOOL": "submit_for_approval", "INPUT": {"ebeln": po["EBELN"]},
                      "RESULT": f"{submitted['RESULT']} {submitted['APPROVAL_REQUEST_ID']}"})
        return {**order, "STATUS": self.store.get(po["EBELN"])["STATUS"],
                "APPROVAL_REQUEST_ID": submitted["APPROVAL_REQUEST_ID"], "NEXT": NEXT_MANAGER}


def handed_back(cases: list[dict[str, Any]]) -> bool:
    """True while any case waits for a human: an unreleased order, or nothing to draft."""
    return any(not c["ORDERS"] or any(o["NEXT"] != NEXT_DONE for o in c["ORDERS"])
               for c in cases)


def human_approvals(report: dict[str, Any], store: PoStore,
                    approver: Principal | None = None) -> dict[str, Any]:
    """Terminal stand-in for the human clicks: each buyer approves its one-click drafts,
    the manager approves what was routed to it. Every release goes through policy."""
    approver = approver or manager()
    steps = list(report["STEPS"])
    cases = []
    for case in report["CASES"]:
        orders = []
        for order in case["ORDERS"]:
            principal = {NEXT_BUYER: buyer(case["BUYER"]), NEXT_MANAGER: approver}.get(order["NEXT"])
            if principal is None:
                orders.append(order)
                continue
            result = release_po(order["EBELN"], store=store, principal=principal)
            steps.append({"TOOL": "release_po",
                          "INPUT": {"ebeln": order["EBELN"], "principal": principal.id},
                          "RESULT": result["RESULT"]})
            released = result["RESULT"] == STATUS_RELEASED
            orders.append({**order, "STATUS": result["STATUS"],
                           "NEXT": NEXT_DONE if released else order["NEXT"],
                           "RELEASED_BY": principal.id if released else None,
                           "RELEASE_RESULT": result["POLICY_DECISION"]})
        cases.append({**case, "ORDERS": orders})
    return {**report, "STEPS": steps, "CASES": cases, "HANDED_BACK": handed_back(cases)}
