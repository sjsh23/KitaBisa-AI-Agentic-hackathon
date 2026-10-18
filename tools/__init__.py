"""MCP tools from PRD section 8, as plain Python functions (Lambda handlers wrap them later).

Implemented so far (Disruption Detection, Supplier Evaluation and Action):
get_open_pos, get_inventory, get_supplier_history, get_quotes, get_market_price,
predict_delay_risk, score_suppliers, draft_po, submit_for_approval, release_po.
"""
from tools.action import draft_po, release_po, submit_for_approval
from tools.predict import predict_delay_risk
from tools.read_tools import (get_inventory, get_market_price, get_open_pos, get_quotes,
                              get_supplier_history)
from tools.score import score_suppliers

__all__ = ["get_open_pos", "get_inventory", "get_supplier_history", "get_quotes",
           "get_market_price", "predict_delay_risk", "score_suppliers", "draft_po",
           "submit_for_approval", "release_po"]
