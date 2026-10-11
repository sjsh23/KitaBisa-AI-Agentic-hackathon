"""CSV adapter over the committed simulated dataset (D-1, D-2)."""
import pytest

from adapters import CsvAdapter
from adapters.csv_adapter import DEFAULT_DATA_DIR


@pytest.fixture(scope="module")
def adapter():
    return CsvAdapter()


def test_refuses_truth_folder():
    with pytest.raises(ValueError, match="D-1"):
        CsvAdapter(DEFAULT_DATA_DIR / "_truth")


def test_refuses_truth_table_name(adapter):
    with pytest.raises(ValueError, match="D-1"):
        adapter.table("_truth/suppliers_truth")


def test_open_po_lines_keep_sap_names_and_units(adapter):
    open_pos = adapter.open_po_lines("RM-GULA-001")
    assert [po["EBELN"] for po in open_pos] == [f"45000004{n}" for n in range(30, 35)]
    po = next(p for p in open_pos if p["EBELN"] == "4500000431")
    assert po["LIFNR"] == "100101" and po["EINDT"] == "2026-10-04"
    assert po["MENGE"] == 10000.0
    assert po["NETPR"] == 17150 and isinstance(po["NETPR"], int)
    assert po["NETWR"] == 171_500_000 and isinstance(po["NETWR"], int)


def test_open_po_lines_filter_by_material(adapter):
    assert adapter.open_po_lines("NO-SUCH-MATERIAL") == []


def test_receipts_are_sorted_and_labelled(adapter):
    receipts = adapter.receipts()
    assert len(receipts) == 429
    assert [r["BUDAT"] for r in receipts] == sorted(r["BUDAT"] for r in receipts)
    assert all(r["LATE"] == (1 if r["DELAY_DAYS"] > 0 else 0) for r in receipts)
    assert {r["LIFNR"] for r in adapter.receipts("100108")} == {"100108"}


def test_receipt_labels_match_rpt_po_lines(adapter):
    labels = {r["EBELN"]: r["LATE"] for r in adapter.rpt_rows() if r["LATE"] is not None}
    assert all(labels[r["EBELN"]] == r["LATE"] for r in adapter.receipts())


def test_inventory_and_predict_rows(adapter):
    inventory = adapter.inventory("RM-GULA-001")
    assert inventory["DAYS_OF_COVER"] == 6.0 and inventory["DOWNTIME_COST_PER_DAY"] == 150_000_000
    predict = adapter.rpt_rows(("predict",))
    assert len(predict) == 5 and all(r["LATE"] is None for r in predict)
