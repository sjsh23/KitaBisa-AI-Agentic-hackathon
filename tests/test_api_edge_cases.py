"""Edge cases the mentor asked about (C1 to C6), through the API with TestClient (FR-SEL-1,
FR-SEL-2, FR-ACT-1, FR-ACT-2, POL-2, FR-INT-1, D-1, D-3). Each test that needs other data
works on a modified copy of data_gen/out (without _truth/) in a temp folder; data_gen/out
itself is never changed. Offline: SAP-RPT off (posterior fallback), no model texts."""
import csv
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adapters.csv_adapter import DEFAULT_DATA_DIR
from integrations.api.app import create_app

KEY = "test-key-not-a-secret"
H = {"X-API-Key": KEY}
REASON = "the price is too high this quarter"
JUSTIFICATION = "the next option is still on time and costs less"


def copy_data(tmp_path: Path) -> Path:
    out = tmp_path / "data"
    shutil.copytree(DEFAULT_DATA_DIR, out, ignore=shutil.ignore_patterns("_truth"))
    return out


def edit_csv(path: Path, change) -> None:
    """Rewrite a CSV in the copy, row by row; `change(row)` edits the dict in place."""
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
        fields = list(rows[0])
    for row in rows:
        change(row)
    time.sleep(0.01)   # a new modification time for the adapter's file cache
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def make_client(tmp_path, monkeypatch, data_dir: Path | None = None) -> TestClient:
    monkeypatch.setenv("GESIT_API_KEY", KEY)
    monkeypatch.setenv("SAP_RPT_MODE", "none")
    monkeypatch.setenv("DECISION_LOG_PATH", str(tmp_path / "logs" / "decision_log.jsonl"))
    monkeypatch.delenv("PLAN_LOG_PATH", raising=False)
    monkeypatch.delenv("PO_STORE_PATH", raising=False)
    if data_dir is not None:
        monkeypatch.setenv("GESIT_DATA_DIR", str(data_dir))
    else:
        monkeypatch.delenv("GESIT_DATA_DIR", raising=False)
    return TestClient(create_app(load_env=False, warm_up=False))


def post(client, url, body=None, status=200):
    r = client.post(url, json=body, headers=H)
    assert r.status_code == status, r.text
    return r.json()


def test_data_gen_out_is_never_written(tmp_path):
    data = copy_data(tmp_path)
    assert not (data / "_truth").exists() and (data / "quotes_asof.csv").exists()


# ---------------------------------------------------------------------- C1
def test_c1_no_backup_can_cover_the_quantity(tmp_path, monkeypatch):
    data = copy_data(tmp_path)
    edit_csv(data / "quotes_asof.csv",
             lambda r: r.update(AVAILABLE_KG_NEXT_30D="1500.0"))   # nobody near 10,000 kg
    client = make_client(tmp_path, monkeypatch, data)
    out = post(client, "/run", {"scenario": "S1", "llm": False})
    case = out["CASES"][0]
    assert case["RECOMMENDED"] == "keep PO 4500000431" and out["DRAFTS"] == []
    assert out["PLAN"]["OUTCOME"]["CODE"] == "KEEP_EVERYWHERE"
    assert case["CANNOT_COVER"]                                   # ranked by nobody, with reason
    assert "No backup supplier can take the order: " in out["SUMMARY"]
    assert "that cannot cover 10,000 kg, not even as a split" in out["SUMMARY"]
    assert "nothing was drafted, the buyer decides" in out["SUMMARY"].lower()


# ---------------------------------------------------------------------- C2
def test_c2_every_backup_below_its_moq(tmp_path, monkeypatch):
    data = copy_data(tmp_path)
    edit_csv(data / "quotes_asof.csv", lambda r: r.update(MINBM="100000.0"))
    client = make_client(tmp_path, monkeypatch, data)
    out = post(client, "/run", {"scenario": "S1", "llm": False})
    case = out["CASES"][0]
    below = [e for e in case["EXCLUDED"] if e["REASON"] == "BELOW_MOQ"]
    assert below and all(e["TEXT"] == "with spare capacity below their MOQ" for e in below)
    assert f"{len(below)} with spare capacity below their MOQ" in out["SUMMARY"]
    assert out["DRAFTS"] == [] and case["RECOMMENDED"] == "keep PO 4500000431"


# ---------------------------------------------------------------------- C3
def test_c3_partial_delivery_buys_only_the_remainder(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    out = post(client, "/run", {"scenario": "S3", "llm": False})
    case = out["CASES"][0]
    assert case["QTY"] == 7_200.0 and "capacity shortfall" in case["REASONS"]
    assert "reduce from 12,000 to 4,800 kg" in case["OLD_PO"]
    assert sum(d["MENGE"] for d in out["DRAFTS"]) == 7_200.0
    assert {d["REPLACES_EBELN"] for d in out["DRAFTS"]} == {"4500000433"}


# ---------------------------------------------------------------------- C4
def test_c4_supplier_leaves_the_panel_before_release(tmp_path, monkeypatch):
    data = copy_data(tmp_path)
    client = make_client(tmp_path, monkeypatch, data)
    draft = post(client, "/run", {"scenario": "S1", "llm": False})["DRAFTS"][0]
    edit_csv(data / "LFA1.csv",
             lambda r: r.update(ZZPANEL="") if r["LIFNR"] == draft["LIFNR"] else None)
    out = post(client, f"/approvals/{draft['EBELN']}/approve", {"as": "MANAGER01"}, 403)
    assert out["POLICY_DECISION"]["POLICY"] == "POL-2"
    assert "denied by POL-2: the supplier is not on the approved panel" in out["SUMMARY"]
    pending = client.get("/approvals", headers=H).json()["PENDING"]
    assert [p["EBELN"] for p in pending] == [draft["EBELN"]]   # routed, never released


# ---------------------------------------------------------------------- C5
def test_c5_reject_then_override_to_the_next_option(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    post(client, "/run", {"scenario": "S1", "llm": False})
    rejected = post(client, "/approvals/4500000435/reject", {"as": "MANAGER01", "reason": REASON})
    nxt = rejected["NEXT_OPTION"]["LABEL"]
    assert f"Override scenario S1 with supplier {nxt}" in rejected["NEXT_STEPS"]
    out = post(client, "/override", {"scenario": "S1", "choose": nxt,
                                     "justification": JUSTIFICATION})
    assert out["SUPERSEDED"] == [] and "supersedes" not in out["SUMMARY"]
    [draft] = out["DRAFTS"]
    assert draft["LIFNR"] == nxt and draft["NEXT"] == "manager approval"   # policy again
    pending = client.get("/approvals", headers=H).json()["PENDING"]
    assert [p["EBELN"] for p in pending] == [draft["EBELN"]]
    post(client, "/approvals/4500000435/approve", {"as": "MANAGER01"}, 409)   # stays rejected


# ---------------------------------------------------------------------- C6
def test_c6_spare_capacity_goes_in_urgency_order(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    out = post(client, "/run", {"scenario": "all", "llm": False})
    order = out["FLAGGED_EBELN"]
    assert [c["EBELN"] for c in out["CASES"]] == order == ["4500000431", "4500000433",
                                                          "4500000432"]
    first, second, third = out["CASES"]
    assert first["RESERVED_FOR"] == []
    assert {"EBELN": "4500000431", "LIFNR": "100109", "KG": 10_000.0} in second["RESERVED_FOR"]
    for i, case in enumerate(out["CASES"]):   # only more urgent POs ever reserve capacity
        assert {t["EBELN"] for t in case["RESERVED_FOR"]} <= set(order[:i])
    assert {t["EBELN"] for t in third["RESERVED_FOR"]} == {"4500000431", "4500000433"}
