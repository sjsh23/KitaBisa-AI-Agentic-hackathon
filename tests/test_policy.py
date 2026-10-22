"""POL-1 to POL-4 (PRD section 9): one allow and one deny per policy."""
from policy import AGENT, APPROVAL_THRESHOLD_IDR, Principal, authorize, buyer, manager

ON_PANEL = {"LIFNR": "100109", "ZZPANEL": "X", "NETWR": 126_000_000}
OFF_PANEL = {"LIFNR": "100111", "ZZPANEL": None, "NETWR": 100_000_000}
ABOVE = {**ON_PANEL, "NETWR": APPROVAL_THRESHOLD_IDR + 1}


def test_pol1_threshold_needs_a_manager():
    assert authorize("release_po", buyer("BUYER01"), {**ON_PANEL, "NETWR": APPROVAL_THRESHOLD_IDR}).allowed
    denied = authorize("release_po", buyer("BUYER01"), ABOVE)
    assert (denied.allowed, denied.policy) == (False, "POL-1")
    assert "role manager" in denied.reason
    assert authorize("release_po", manager(), ABOVE).allowed


def test_pol2_approved_panel_only():
    assert authorize("draft_po", AGENT, ON_PANEL).allowed
    for tool in ("draft_po", "release_po"):
        denied = authorize(tool, manager(), OFF_PANEL)   # not even a manager
        assert (denied.allowed, denied.policy) == (False, "POL-2")


def test_pol3_only_listed_tools():
    assert authorize("get_quotes", AGENT).allowed
    denied = authorize("delete_po", manager())
    assert (denied.allowed, denied.policy) == (False, "POL-3")


def test_pol4_agents_never_approve():
    assert authorize("submit_for_approval", AGENT, ON_PANEL).allowed
    denied = authorize("release_po", AGENT, ON_PANEL)   # below threshold and on panel
    assert (denied.allowed, denied.policy) == (False, "POL-4")
    other_agent = Principal("some-agent", "agent")
    assert authorize("release_po", other_agent, ON_PANEL).policy == "POL-4"


def test_decision_as_dict():
    assert authorize("release_po", AGENT, ON_PANEL).as_dict()["POLICY"] == "POL-4"


def test_reject_po_only_by_a_manager():   # proposal item 11: POL-4 for agents, POL-1 for buyers
    assert authorize("reject_po", manager("MGR07"), ABOVE).allowed
    assert authorize("reject_po", AGENT, ABOVE).policy == "POL-4"
    denied = authorize("reject_po", buyer("BUYER01"), ABOVE)
    assert (denied.allowed, denied.policy) == (False, "POL-1")


def test_list_pending_approvals_is_a_permitted_tool():
    assert authorize("list_pending_approvals", AGENT).allowed
