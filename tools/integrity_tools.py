"""Integrity Guard tools (PRD section 8): record_override, get_integrity_flags (FR-INT-1 to FR-INT-4).

No LLM. The override rule (FR-INT-1): choosing anything other than the #1 option,
including a new order when keeping the current PO ranked #1 and keeping it when a
new order ranked #1, needs a written justification of at least 20 characters
before a draft PO is created. Every override is logged as one JSON line (FR-INT-2)
with the expected costs taken from the Supplier Evaluation result, never recomputed and
never taken from a prompt.

Storage for now: a JSONL file at the environment variable DECISION_LOG_PATH
(default ./decision_log.jsonl, gitignored). DynamoDB replaces it when deployed;
the function signatures stay the same. SAP field names kept (D-2), amounts IDR
integers, every figure from simulated data (D-3).
"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from adapters import CsvAdapter, default_adapter
from scoring import integrity, supplier_score

MIN_JUSTIFICATION_CHARS = 20               # FR-INT-1
DECISION_LOG_ENV = "DECISION_LOG_PATH"
DEFAULT_DECISION_LOG = "decision_log.jsonl"
# Asia/Jakarta (WIB) is UTC+7 all year, without daylight saving; a fixed offset needs no tzdata.
JAKARTA = timezone(timedelta(hours=7), "Asia/Jakarta")
OVERRIDE_PREFIX = "OVR-"
REJECT_PREFIX = "REJ-"
ENTRY_REJECT = "REJECT"   # a manager's reject (FR-ACT-2 reject path, proposal item 11)


# ------------------------------------------------------------------- options
def keep_label(ebeln: str) -> str:
    return f"keep PO {ebeln}"


def option_label(row: Mapping[str, Any]) -> str:
    """How a ranked row is named in the override rule and the log: a LIFNR or 'keep PO <ebeln>'."""
    if row.get("OPTION") == supplier_score.OPTION_KEEP:
        return keep_label(row["KEEP_EBELN"])
    return str(row["LIFNR"])


def recommended_label(case: Mapping[str, Any]) -> str | None:
    """The #1 option of a Supplier Evaluation case, or None when nothing was ranked."""
    rows = case["SCORING"]["CANDIDATES"]
    return option_label(rows[0]) if rows else None


def find_option(case: Mapping[str, Any], label: str) -> dict[str, Any] | None:
    """The ranked row a label names, or None when it is not one of the ranked options."""
    wanted = label.strip().lower()
    return next((row for row in case["SCORING"]["CANDIDATES"]
                 if option_label(row).lower() == wanted), None)


# ------------------------------------------------------------- override rule
def check_override(recommended: str, chosen: str, justification: str | None) -> dict[str, Any]:
    """FR-INT-1: is the buyer's choice allowed now? Writes nothing.

    Same as the recommendation: allowed, no justification needed. Anything else:
    allowed only with at least 20 characters of justification after strip().
    """
    if chosen.strip().lower() == recommended.strip().lower():
        return {"ALLOWED": True, "OVERRIDE": False, "JUSTIFICATION_CHARS": None,
                "MESSAGE": f"{chosen} is the recommended option; no justification needed."}
    chars = len((justification or "").strip())
    if chars < MIN_JUSTIFICATION_CHARS:
        return {"ALLOWED": False, "OVERRIDE": True, "JUSTIFICATION_CHARS": chars,
                "MESSAGE": (f"Rejected: choosing {chosen} instead of the recommended {recommended} "
                            f"needs a written justification of at least "
                            f"{MIN_JUSTIFICATION_CHARS} characters ({chars} given). "
                            "No draft PO was created.")}
    return {"ALLOWED": True, "OVERRIDE": True, "JUSTIFICATION_CHARS": chars,
            "MESSAGE": (f"Override accepted: {chosen} instead of the recommended {recommended}, "
                        "justification recorded.")}


# --------------------------------------------------------------- decision log
def decision_log_path(path: str | os.PathLike[str] | None = None) -> Path:
    return Path(path or os.environ.get(DECISION_LOG_ENV) or DEFAULT_DECISION_LOG)


def _read_log(path: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    log = decision_log_path(path)
    if not log.exists():
        return []
    with open(log, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def read_overrides(path: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    """Every logged override, oldest first; an empty list when the log does not exist.

    Override entries carry no ENTRY_TYPE (unchanged since FR-INT-2); other entry types
    in the same log, such as a manager's reject, are left out.
    """
    return [e for e in _read_log(path) if "ENTRY_TYPE" not in e]


def read_rejections(path: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    """Every logged reject of a PO waiting for approval, oldest first."""
    return [e for e in _read_log(path) if e.get("ENTRY_TYPE") == ENTRY_REJECT]


def clear_decision_log(path: str | os.PathLike[str] | None = None) -> None:
    """Empty the decision log (for scripts/reset_demo, NFR-2)."""
    log = decision_log_path(path)
    if log.exists():
        log.unlink()


def record_override(ebeln: str, recommended: str, chosen: str, justification: str, *,
                    case: Mapping[str, Any], buyer: str,
                    path: str | os.PathLike[str] | None = None,
                    now: datetime | None = None) -> str:
    """FR-INT-2: log one override and return its id.

    `case` is the Supplier Evaluation case for `ebeln`; the expected costs of the recommended
    and the chosen option come from its ranked rows (the keep row included). Raises
    ValueError when the override rule is not met, when `recommended` is not the
    case's #1, or when `chosen` is not a ranked option.
    """
    if case["EBELN"] != ebeln:
        raise ValueError(f"case is for PO {case['EBELN']}, not {ebeln}")
    actual = recommended_label(case)
    if actual is None or actual.lower() != recommended.strip().lower():
        raise ValueError(f"the recommended option for PO {ebeln} is {actual}, not {recommended}")
    check = check_override(recommended, chosen, justification)
    if not check["OVERRIDE"]:
        raise ValueError("chosen equals the recommended option; there is no override to log")
    if not check["ALLOWED"]:
        raise ValueError(check["MESSAGE"])
    best, picked = find_option(case, recommended), find_option(case, chosen)
    if picked is None:
        raise ValueError(f"{chosen} is not one of the ranked options for PO {ebeln}")

    log = decision_log_path(path)
    override_id = f"{OVERRIDE_PREFIX}{ebeln}-{len(read_overrides(log)) + 1:03d}"
    record = {
        "TIMESTAMP": (now or datetime.now(JAKARTA)).astimezone(JAKARTA).isoformat(
            timespec="seconds"),
        "OVERRIDE_ID": override_id,
        "BUYER": buyer,
        "EBELN": ebeln,
        "RECOMMENDED": option_label(best),
        "CHOSEN": option_label(picked),
        "EXPECTED_COST_RECOMMENDED": int(best["EXPECTED_COST"]),
        "EXPECTED_COST_CHOSEN": int(picked["EXPECTED_COST"]),
        # chosen minus recommended: positive when the buyer's choice costs more
        "COST_DIFFERENCE": int(picked["EXPECTED_COST"]) - int(best["EXPECTED_COST"]),
        "JUSTIFICATION": justification.strip(),
        "DATA_NOTE": "simulated data",
    }
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return override_id


def record_rejection(po: Mapping[str, Any], next_option: Mapping[str, Any] | None, *,
                     path: str | os.PathLike[str] | None = None,
                     now: datetime | None = None) -> str:
    """Log a manager's reject as its own entry type and return its id (REJ-<EBELN>-<n>).

    `po` is the rejected PO after the reject (REJECTED_BY, REJECT_REASON filled);
    `next_option` is the row of its saved ranked table offered to the buyer next.
    """
    log = decision_log_path(path)
    reject_id = f"{REJECT_PREFIX}{po['EBELN']}-{len(read_rejections(log)) + 1:03d}"
    entry = {
        "ENTRY_TYPE": ENTRY_REJECT,
        "TIMESTAMP": (now or datetime.now(JAKARTA)).astimezone(JAKARTA).isoformat(
            timespec="seconds"),
        "REJECT_ID": reject_id,
        "EBELN": po["EBELN"],
        "APPROVAL_REQUEST_ID": po["APPROVAL_REQUEST_ID"],
        "REPLACES_EBELN": po["REPLACES_EBELN"],
        "LIFNR": po["LIFNR"],
        "NETWR": int(po["NETWR"]),
        "BUYER": po["ERNAM"],
        "REJECTED_BY": po["REJECTED_BY"],
        "REJECT_REASON": po["REJECT_REASON"],
        "NEXT_OPTION": option_label(next_option) if next_option else None,
        "NEXT_OPTION_EXPECTED_COST": int(next_option["EXPECTED_COST"]) if next_option else None,
        "DATA_NOTE": "simulated data",
    }
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return reject_id


# ---------------------------------------------------------------- flags tool
def get_integrity_flags(*, adapter: CsvAdapter | None = None,
                        path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """FR-INT-3, FR-INT-4: flagged buyer and supplier pairs with evidence, plus override counts.

    Flags are for review only; nothing here blocks a PO or a buyer.
    """
    adapter = adapter or default_adapter()
    overrides = read_overrides(path)
    by_buyer = Counter(o["BUYER"] for o in overrides)
    scan = integrity.scan_pairs(adapter.table("EKKO"), by_buyer)
    return {
        "DATA_NOTE": "simulated data",
        "RULE": integrity.RULE_TEXT,
        "PAIRS_TESTED": scan.pairs_tested,
        "PAIRS_FLAGGED": scan.pairs_flagged,
        "FLAGS": [{**pair.as_dict(), "REVIEW_TEXT": integrity.review_text(pair)}
                  for pair in scan.flagged],
        "OVERRIDES_TOTAL": len(overrides),
        "OVERRIDES_BY_BUYER": dict(sorted(by_buyer.items())),
        "OVERRIDES_BY_CHOSEN": dict(sorted(Counter(o["CHOSEN"] for o in overrides).items())),
    }
