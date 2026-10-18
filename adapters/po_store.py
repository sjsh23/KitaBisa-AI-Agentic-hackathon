"""Purchase orders created by GESIT (DRAFT, PENDING_APPROVAL, RELEASED), S/4-shaped.

Kept in memory for one run, so every terminal run starts from the scenario
start state (NFR-2). DynamoDB replaces this class when the agents move to
AgentCore (PRD section 5); the methods stay the same. D-2: EKKO, EKPO and EKET
field names. EBELN continues the S/4 number range after the last PO in EKKO.
"""
from __future__ import annotations

import copy
from typing import Any

from adapters.csv_adapter import CsvAdapter

STATUS_DRAFT = "DRAFT"
STATUS_PENDING = "PENDING_APPROVAL"
STATUS_RELEASED = "RELEASED"


class PoStore:
    """GESIT's own POs for one run, keyed by EBELN; each keeps its event history."""

    def __init__(self, adapter: CsvAdapter) -> None:
        self.adapter = adapter
        self._pos: dict[str, dict[str, Any]] = {}

    def next_ebeln(self) -> str:
        """The next PO number after every PO in EKKO and every PO in this store."""
        used = [int(r["EBELN"]) for r in self.adapter.table("EKKO")] + [int(e) for e in self._pos]
        return str(max(used) + 1)

    def add(self, po: dict[str, Any]) -> dict[str, Any]:
        if po["EBELN"] in self._pos:
            raise ValueError(f"PO {po['EBELN']} exists already")
        self._pos[po["EBELN"]] = copy.deepcopy(po)
        return self.get(po["EBELN"])

    def get(self, ebeln: str) -> dict[str, Any]:
        """A copy of one PO; KeyError when GESIT did not create it."""
        if ebeln not in self._pos:
            raise KeyError(f"PO {ebeln} was not created by GESIT in this run")
        return copy.deepcopy(self._pos[ebeln])

    def update(self, ebeln: str, event: dict[str, Any], **fields: Any) -> dict[str, Any]:
        """Change fields of a PO and append `event` to its history."""
        po = self._pos[ebeln]
        po.update(fields)
        po["HISTORY"].append(dict(event))
        return self.get(ebeln)

    def all(self) -> list[dict[str, Any]]:
        return [self.get(e) for e in sorted(self._pos)]
