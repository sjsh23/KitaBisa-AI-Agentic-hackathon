"""Action tools (PRD section 8): draft_po, submit_for_approval, release_po (FR-ACT-1, FR-ACT-2).

Every call is checked by `policy.authorize` first (POL-1 to POL-4); a denied call
changes nothing except the PO's history, and returns RESULT "DENIED" with the
policy that denied it. Amounts IDR integers, quantities KG, dates ISO, SAP field
names (D-2).
"""
from __future__ import annotations

from typing import Any

from adapters import PoStore
from adapters.po_store import STATUS_DRAFT, STATUS_PENDING, STATUS_RELEASED
from policy import AGENT, Decision, Principal, authorize

APPROVAL_PREFIX = "APR-"


def _event(tool: str, principal: Principal, decision: Decision, result: str) -> dict[str, Any]:
    return {"TOOL": tool, "BY": principal.id, "ROLE": principal.role, "RESULT": result,
            "POLICY": decision.policy, "REASON": decision.reason}


def _template(store: PoStore, matnr: str, replaces: str | None) -> tuple[dict, dict]:
    """EKKO and EKPO rows to copy organisation data from: the replaced PO, else the latest one."""
    headers = {r["EBELN"]: r for r in store.adapter.table("EKKO")}
    items = [r for r in store.adapter.table("EKPO") if r["MATNR"] == matnr]
    if not items:
        raise KeyError(f"no purchase order history for material {matnr}")
    item = next((r for r in items if r["EBELN"] == replaces),
                max(items, key=lambda r: r["EBELN"]))
    return headers[item["EBELN"]], item


def draft_po(lifnr: str, matnr: str, qty: float, netpr: int, eindt: str, reason: str, *,
             store: PoStore, ernam: str, bedat: str | None = None, replaces: str | None = None,
             principal: Principal = AGENT) -> dict[str, Any]:
    """FR-ACT-1: a PO in status DRAFT with the S/4 fields filled; ERNAM is the buyer it is for."""
    supplier = store.adapter.suppliers().get(lifnr, {})
    netwr = int(round(float(qty) * int(netpr)))
    resource = {"LIFNR": lifnr, "ZZPANEL": supplier.get("ZZPANEL"), "NETWR": netwr}
    decision = authorize("draft_po", principal, resource)                      # POL-2
    if not decision.allowed:
        return {"RESULT": "DENIED", "EBELN": None, "POLICY_DECISION": decision.as_dict(),
                "PO": None}

    header, item = _template(store, matnr, replaces)
    bedat = bedat or store.adapter.inventory(matnr)["DATE"]
    po = {
        "EBELN": store.next_ebeln(), "EBELP": 10,
        "BUKRS": header["BUKRS"], "BSTYP": header["BSTYP"], "BSART": header["BSART"],
        "EKORG": header["EKORG"], "EKGRP": header["EKGRP"], "WAERS": header["WAERS"],
        "LIFNR": lifnr, "ZZPANEL": supplier.get("ZZPANEL"), "BEDAT": bedat, "ERNAM": ernam,
        "MATNR": matnr, "TXZ01": item["TXZ01"], "WERKS": item["WERKS"],
        "MENGE": float(qty), "MEINS": item["MEINS"], "NETPR": int(netpr), "PEINH": 1,
        "NETWR": netwr, "EINDT": eindt,
        "STATUS": STATUS_DRAFT, "REPLACES_EBELN": replaces, "REASON": reason,
        "APPROVAL_REQUEST_ID": None, "RELEASED_BY": None,
        "HISTORY": [_event("draft_po", principal, decision, STATUS_DRAFT)],
    }
    po = store.add(po)
    return {"RESULT": "DRAFTED", "EBELN": po["EBELN"], "POLICY_DECISION": decision.as_dict(),
            "PO": po}


def submit_for_approval(ebeln: str, *, store: PoStore,
                        principal: Principal = AGENT) -> dict[str, Any]:
    """FR-ACT-2: route a draft to the procurement manager; returns the approval request id."""
    po = store.get(ebeln)
    decision = authorize("submit_for_approval", principal, po)
    if not decision.allowed:
        store.update(ebeln, _event("submit_for_approval", principal, decision, "DENIED"))
        return {"RESULT": "DENIED", "EBELN": ebeln, "POLICY_DECISION": decision.as_dict(),
                "APPROVAL_REQUEST_ID": None}
    if po["STATUS"] == STATUS_PENDING:   # submitting twice keeps the first request
        return {"RESULT": "SUBMITTED", "EBELN": ebeln, "POLICY_DECISION": decision.as_dict(),
                "APPROVAL_REQUEST_ID": po["APPROVAL_REQUEST_ID"]}
    if po["STATUS"] != STATUS_DRAFT:
        raise ValueError(f"PO {ebeln} is {po['STATUS']}, only a DRAFT can be submitted")
    request_id = f"{APPROVAL_PREFIX}{ebeln}"
    store.update(ebeln, _event("submit_for_approval", principal, decision, STATUS_PENDING),
                 STATUS=STATUS_PENDING, APPROVAL_REQUEST_ID=request_id)
    return {"RESULT": "SUBMITTED", "EBELN": ebeln, "POLICY_DECISION": decision.as_dict(),
            "APPROVAL_REQUEST_ID": request_id}


def release_po(ebeln: str, *, store: PoStore, principal: Principal) -> dict[str, Any]:
    """FR-ACT-2, FR-ACT-3: release a PO, or DENIED by policy (POL-1, POL-2, POL-4).

    The panel flag is read again from LFA1 at release time, not trusted from the draft.
    """
    po = store.get(ebeln)
    if po["STATUS"] not in (STATUS_DRAFT, STATUS_PENDING):
        raise ValueError(f"PO {ebeln} is {po['STATUS']}, only a DRAFT or PENDING_APPROVAL "
                         "PO can be released")
    panel = store.adapter.suppliers().get(po["LIFNR"], {}).get("ZZPANEL")
    decision = authorize("release_po", principal, {**po, "ZZPANEL": panel})
    if not decision.allowed:
        store.update(ebeln, _event("release_po", principal, decision, "DENIED"))
        return {"RESULT": "DENIED", "EBELN": ebeln, "POLICY_DECISION": decision.as_dict(),
                "STATUS": po["STATUS"]}
    store.update(ebeln, _event("release_po", principal, decision, STATUS_RELEASED),
                 STATUS=STATUS_RELEASED, RELEASED_BY=principal.id)
    return {"RESULT": "RELEASED", "EBELN": ebeln, "POLICY_DECISION": decision.as_dict(),
            "STATUS": STATUS_RELEASED}
