"""POL-1 to POL-4 (PRD section 9) as plain Python, evaluated on every action tool call.

This is the local stand-in for AgentCore Policy (Cedar) until the Gateway is set
up (milestone 12-18 Oct). The Cedar policies must say the same thing, and the
tests in `tests/test_policy.py` (one allow and one deny per policy) then run
against both.

Rules, checked in this order, deny wins (Cedar is deny-by-default):

- POL-3: a tool that is not in the section 8 list is denied.
- POL-4: an agent principal never calls an approval tool (`release_po`,
  `reject_po`); only a human (buyer or manager) releases a PO.
- POL-2: `draft_po` and `release_po` are denied for a supplier that is not on the
  approved panel (LFA1.ZZPANEL is not `X`).
- POL-1: `release_po` is denied when NETWR is above the threshold, unless the
  principal has role manager. `reject_po` (the manager's no to an approval
  request that POL-1 routed) is denied to every role but manager.

`list_pending_approvals` and `reject_po` are not in PRD section 8 yet: built,
waiting for team agreement (docs/prd_change_proposals.md item 11).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

# FR-ACT-2 and POL-1: Rp150 juta, a config value (open decision in PRD section 17).
APPROVAL_THRESHOLD_IDR = 150_000_000

ROLE_BUYER = "buyer"
ROLE_MANAGER = "manager"
ROLE_AGENT = "agent"

# POL-3: the tools of PRD section 8, nothing else.
PERMITTED_TOOLS = frozenset({
    "get_open_pos", "get_po", "get_supplier_history", "get_quotes", "get_inventory",
    "get_market_price", "predict_delay_risk", "score_suppliers", "draft_po",
    "submit_for_approval", "release_po", "record_override", "record_outcome",
    "get_integrity_flags",
    "list_pending_approvals", "reject_po",   # proposal item 11, waiting for team agreement
})
APPROVAL_TOOLS = frozenset({"release_po", "reject_po"})   # POL-4: humans only
PANEL_TOOLS = frozenset({"draft_po", "release_po"})  # POL-2


@dataclass(frozen=True)
class Principal:
    """Who calls a tool: a buyer, a manager, or an agent."""
    id: str
    role: str


@dataclass(frozen=True)
class Decision:
    """Outcome of one policy check; POLICY names the rule that denied, None when allowed."""
    allowed: bool
    policy: str | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {k.upper(): v for k, v in asdict(self).items()}


AGENT = Principal("gesit-action-agent", ROLE_AGENT)


def buyer(user_id: str) -> Principal:
    return Principal(user_id, ROLE_BUYER)


def manager(user_id: str = "MANAGER01") -> Principal:
    return Principal(user_id, ROLE_MANAGER)


def authorize(tool: str, principal: Principal,
              resource: Mapping[str, Any] | None = None) -> Decision:
    """Allow or deny `principal` calling `tool` on `resource` (a PO with LIFNR, ZZPANEL, NETWR)."""
    resource = resource or {}
    if tool not in PERMITTED_TOOLS:
        return Decision(False, "POL-3", f"tool {tool} is not on the permitted tool list")
    if tool in APPROVAL_TOOLS and principal.role == ROLE_AGENT:
        return Decision(False, "POL-4", f"agent {principal.id} cannot call the approval tool "
                                        f"{tool}; a buyer or manager must")
    if tool == "reject_po" and principal.role != ROLE_MANAGER:
        return Decision(False, "POL-1", f"only role manager decides an approval request; "
                                        f"{principal.id} has role {principal.role}")
    if tool in PANEL_TOOLS and resource.get("ZZPANEL") != "X":
        return Decision(False, "POL-2", f"supplier {resource.get('LIFNR')} is not on the "
                                        "approved panel")
    if tool == "release_po":
        netwr = int(resource.get("NETWR") or 0)
        if netwr > APPROVAL_THRESHOLD_IDR and principal.role != ROLE_MANAGER:
            return Decision(False, "POL-1", f"NETWR Rp{netwr:,} is above the approval "
                                            f"threshold Rp{APPROVAL_THRESHOLD_IDR:,}; needs "
                                            f"role manager, {principal.id} has role {principal.role}")
    return Decision(True, None, f"{principal.id} ({principal.role}) may call {tool}")
