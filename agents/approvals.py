"""The manager's approval step in the terminal (FR-ACT-2, FR-ACT-3, POL-1, POL-4).

    python -m agents.approvals list
    python -m agents.approvals approve 4500000435 --as MANAGER01
    python -m agents.approvals reject 4500000435 --as MANAGER01 --reason "text of 20+ characters"

The POs come from the saved PO store (PO_STORE_PATH, default po_store.json next to
the decision log) that `python -m agents.supervisor` writes: the Action side owns
the PO state, so a PO submitted for approval waits there, in another process,
until the manager decides. The approver is the principal given with --as (role
from --role, default manager); in the deployed version it comes from the UI login
(Joule or the fallback UI), never from the tools.

approve reuses `release_po` (same policy check, no new release logic). reject
calls `reject_po`: the draft is never released, the old PO is not touched, and the
next option from the ranked table saved with the draft is printed for the buyer,
not drafted (docs/prd_change_proposals.md item 11). Every figure is from simulated
data (D-3).
"""
from __future__ import annotations

import argparse
from typing import Any

from adapters import PoStore, default_adapter
from adapters.po_store import STATUS_PENDING, po_store_path
from policy import Principal
from scoring import supplier_score
from tools import list_pending_approvals, reject_po, release_po

VIEWER = Principal("terminal", "viewer")   # read-only listing; roles matter for approval tools


def option_text(row: dict[str, Any]) -> str:
    """One ranked row in words, numbers copied from the saved table (simulated)."""
    if row.get("OPTION") == supplier_score.OPTION_KEEP:
        what = f"keep PO {row['KEEP_EBELN']} as it is, no new order"
    else:
        what = (f"{row['LIFNR']}, {row['MENGE']:,.0f} kg at Rp{row['NETPR']:,} per kg, arrives "
                f"{row['ARRIVAL']}")
        split = row.get("SPLIT_WITH")
        if split:
            what += (f", split with {split['LIFNR']} {split['MENGE']:,.0f} kg at "
                     f"Rp{split['NETPR']:,} per kg")
    return f"rank {row['RANK']}: {what}, expected cost Rp{row['EXPECTED_COST']:,} (simulated)"


def format_pending(listing: dict[str, Any]) -> str:
    pending = listing["PENDING"]
    if not pending:
        return "No PO is waiting for approval."
    lines = [f"{'EBELN':<12}{'REQUEST':<16}{'LIFNR':<8}{'MENGE kg':>9}{'NETWR Rp':>14}  "
             f"{'REPLACES':<12}{'BUYER':<9}SUBMITTED"]
    for p in pending:
        lines += [f"{p['EBELN']:<12}{p['APPROVAL_REQUEST_ID']:<16}{p['LIFNR']:<8}"
                  f"{p['MENGE']:>9,.0f}{p['NETWR']:>14,}  {p['REPLACES_EBELN'] or '-':<12}"
                  f"{p['ERNAM']:<9}{p['SUBMITTED_AT'] or '-'}",
                  f"  Why: {p['REASON']}"]
    return "\n".join(lines)


def approve(store: PoStore, ebeln: str, principal: Principal) -> tuple[int, list[str]]:
    po = store.get(ebeln)
    if po["STATUS"] != STATUS_PENDING:
        return 1, [f"Error: PO {ebeln} is {po['STATUS']}, only a {STATUS_PENDING} PO can be "
                   "approved; nothing changed."]
    result = release_po(ebeln, store=store, principal=principal)
    decision = result["POLICY_DECISION"]
    if result["RESULT"] != "RELEASED":
        return 1, [f"Approve by {principal.id} denied by {decision['POLICY']}: "
                   f"{decision['REASON']}. PO {ebeln} stays {result['STATUS']}."]
    lines = [f"PO {ebeln} ({po['APPROVAL_REQUEST_ID']}, {po['LIFNR']}, NETWR "
             f"Rp{po['NETWR']:,}) approved and RELEASED by {principal.id} ({principal.role})."]
    if po.get("OLD_PO_TEXT"):
        lines.append(po["OLD_PO_TEXT"])
    return 0, lines


def reject(store: PoStore, ebeln: str, reason: str, principal: Principal) -> tuple[int, list[str]]:
    result = reject_po(ebeln, reason, store=store, principal=principal)
    decision = result["POLICY_DECISION"]
    if result["RESULT"] == "DENIED":
        return 1, [f"Reject by {principal.id} denied by {decision['POLICY']}: "
                   f"{decision['REASON']}. PO {ebeln} stays {result['STATUS']}."]
    if result["RESULT"] == "REFUSED":
        return 1, [f"Refused: {result['MESSAGE']}. PO {ebeln} stays {result['STATUS']}."]
    po = store.get(ebeln)
    lines = [f"PO {ebeln} ({po['APPROVAL_REQUEST_ID']}, {po['LIFNR']}, NETWR Rp{po['NETWR']:,}) "
             f"REJECTED by {principal.id}: {po['REJECT_REASON']}",
             "The rejected draft is never released.",
             f"Old PO {po['REPLACES_EBELN']}: untouched, the buyer ({po['ERNAM']}) still has it."]
    siblings = [p["EBELN"] for p in store.all()   # the other part of the same split draft
                if "Split order" in po["REASON"] and "Split order" in p["REASON"]
                and abs(int(p["EBELN"]) - int(ebeln)) == 1 and p["STATUS"] == STATUS_PENDING
                and p["REPLACES_EBELN"] == po["REPLACES_EBELN"]]
    if siblings:
        lines.append(f"The other part of this split order, PO {', '.join(siblings)}, still waits "
                     "for approval.")
    option = result["NEXT_OPTION"]
    if option:
        lines += [f"Next option for the buyer, from the ranked table saved with the draft: "
                  f"{option_text(option)}.",
                  "Not drafted and not scored again: the buyer decides. Choosing it over #1 "
                  "needs a justification of 20 characters or more (FR-INT-1)."]
    else:
        lines.append("No other ranked option was saved with the draft; handed back to the buyer.")
    lines.append(f"Decision log: {result['REJECT_ID']}.")
    return 0, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The manager's approval step for GESIT POs.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "approve", "reject"):
        cmd = sub.add_parser(name)
        if name != "list":
            cmd.add_argument("ebeln")
        cmd.add_argument("--as", dest="principal", required=name != "list",
                         help="who acts, e.g. MANAGER01 (from the UI login when deployed)")
        cmd.add_argument("--role", default="manager", help="role of --as (default manager)")
        if name == "reject":
            cmd.add_argument("--reason", required=True, help="why, 20 characters or more")
    args = parser.parse_args(argv)

    store = PoStore(default_adapter(), path=po_store_path())
    principal = Principal(args.principal, args.role) if args.principal else VIEWER
    print(f"GESIT Approvals (simulated data)\nPO store {store.path}, acting as {principal.id} "
          f"({principal.role})\n")
    try:
        if args.command == "list":
            listing = list_pending_approvals(store=store, principal=principal)
            print(format_pending(listing))
            return 0
        if args.command == "approve":
            code, lines = approve(store, args.ebeln, principal)
        else:
            code, lines = reject(store, args.ebeln, args.reason, principal)
    except (KeyError, ValueError) as exc:
        message = str(exc.args[0] if exc.args else exc)
        print(f"Error: {message}" + ("" if message.endswith(".") else "."))
        return 1
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
