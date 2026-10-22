"""Purchase orders created by GESIT (DRAFT, PENDING_APPROVAL, RELEASED, REJECTED), S/4-shaped.

Two modes. In memory (no `path`) for one run, so every agents.run starts from the
scenario start state (NFR-2), and for the tests. With a `path`, a JSON file that
is loaded on start and saved after every change, so a PO waiting for the manager
survives between processes: the Action side owns the PO state and the wait for
approval (FR-ACT-2). The path comes from PO_STORE_PATH, default po_store.json
next to the decision log (gitignored); `clear_po_store` empties it for the later
scripts/reset_demo. DynamoDB replaces the file when the agents move to AgentCore
(PRD section 5); the methods stay the same. D-2: EKKO, EKPO and EKET field names.
EBELN continues the S/4 number range after the last PO in EKKO and in the store.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from adapters.csv_adapter import CsvAdapter

STATUS_DRAFT = "DRAFT"
STATUS_PENDING = "PENDING_APPROVAL"
STATUS_RELEASED = "RELEASED"
STATUS_REJECTED = "REJECTED"
# Replaced by a newer draft for the same flagged PO before anyone decided; never releasable
# (one open draft per flagged PO, docs/prd_change_proposals.md item 14).
STATUS_SUPERSEDED = "SUPERSEDED"
OPEN_STATUSES = frozenset({STATUS_DRAFT, STATUS_PENDING})

PO_STORE_ENV = "PO_STORE_PATH"
DEFAULT_PO_STORE_NAME = "po_store.json"


def po_store_path(path: str | os.PathLike[str] | None = None) -> Path:
    """PO_STORE_PATH, else po_store.json in the decision log's folder (DECISION_LOG_PATH)."""
    if path or os.environ.get(PO_STORE_ENV):
        return Path(path or os.environ[PO_STORE_ENV])
    decision_log = Path(os.environ.get("DECISION_LOG_PATH") or "decision_log.jsonl")
    return decision_log.with_name(DEFAULT_PO_STORE_NAME)


def clear_po_store(path: str | os.PathLike[str] | None = None) -> None:
    """Delete the saved POs (for scripts/reset_demo, NFR-2)."""
    store = po_store_path(path)
    if store.exists():
        store.unlink()


class PoStore:
    """GESIT's own POs keyed by EBELN; each keeps its event history."""

    def __init__(self, adapter: CsvAdapter, path: str | os.PathLike[str] | None = None) -> None:
        self.adapter = adapter
        self.path = Path(path) if path else None
        self._pos: dict[str, dict[str, Any]] = {}
        if self.path and self.path.exists():
            with open(self.path, encoding="utf-8") as fh:
                self._pos = {po["EBELN"]: po for po in json.load(fh)["POS"]}

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"DATA_NOTE": "simulated data", "POS": self.all()}, fh, indent=2,
                      ensure_ascii=False)
        os.replace(tmp, self.path)

    def next_ebeln(self) -> str:
        """The next PO number after every PO in EKKO and every PO in this store."""
        used = [int(r["EBELN"]) for r in self.adapter.table("EKKO")] + [int(e) for e in self._pos]
        return str(max(used) + 1)

    def add(self, po: dict[str, Any]) -> dict[str, Any]:
        if po["EBELN"] in self._pos:
            raise ValueError(f"PO {po['EBELN']} exists already")
        self._pos[po["EBELN"]] = copy.deepcopy(po)
        self._save()
        return self.get(po["EBELN"])

    def get(self, ebeln: str) -> dict[str, Any]:
        """A copy of one PO; KeyError when GESIT did not create it."""
        if ebeln not in self._pos:
            raise KeyError(f"PO {ebeln} was not created by GESIT"
                           + (f" (store {self.path})" if self.path else " in this run"))
        return copy.deepcopy(self._pos[ebeln])

    def update(self, ebeln: str, event: dict[str, Any] | None = None,
               **fields: Any) -> dict[str, Any]:
        """Change fields of a PO and append `event` to its history (None: fields only)."""
        po = self._pos[ebeln]
        po.update(copy.deepcopy(fields))
        if event is not None:
            po["HISTORY"].append(dict(event))
        self._save()
        return self.get(ebeln)

    def all(self) -> list[dict[str, Any]]:
        return [self.get(e) for e in sorted(self._pos)]
