"""Feature rows for hypothetical POs match the definitions of rpt_po_lines (FR-SEL-2)."""
import importlib.util
import sys
from pathlib import Path

import pytest

from adapters import CsvAdapter
from scoring import rpt_features

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def adapter():
    return CsvAdapter()


@pytest.mark.parametrize("ebeln", ["4500000430", "4500000431", "4500000433", "4500000200"])
def test_history_features_match_the_dataset(adapter, ebeln):
    row = next(r for r in adapter.rpt_rows() if r["EBELN"] == ebeln)
    features = rpt_features.history_features(adapter.receipts(row["LIFNR"]), row["BEDAT"])
    for column, value in features.items():
        assert value == (pytest.approx(row[column]) if row[column] is not None else None), column


def test_calendar_features():
    assert rpt_features.is_rainy("2026-11-02") and not rpt_features.is_rainy("2026-10-03")
    assert rpt_features.in_lebaran_window("2026-03-12")       # 8 days before Idul Fitri
    assert not rpt_features.in_lebaran_window("2026-03-29")   # 9 days after


def test_calendar_constants_match_the_generator():
    # D-4: the assumptions live in data_gen/config.py; the agent keeps a copy.
    spec = importlib.util.spec_from_file_location("gen_config", REPO / "data_gen" / "config.py")
    config = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = config   # its dataclasses look the module up while loading
    try:
        spec.loader.exec_module(config)
    finally:
        del sys.modules[spec.name]
    assert tuple(config.IDUL_FITRI) == rpt_features.IDUL_FITRI
    assert set(config.RAINY_MONTHS) == rpt_features.RAINY_MONTHS
    assert tuple(config.LEBARAN_WINDOW) == rpt_features.LEBARAN_WINDOW


def test_utilization_from_spare_capacity():
    # 30,000 kg spare + 10,000 kg ordered recently = 40,000 kg capacity.
    assert rpt_features.utilization(10_000, 30_000, 8_000) == 0.45
    assert rpt_features.utilization(10_000, 0.0, 8_000) is None


def test_hypothetical_row(adapter):
    quote = next(q for q in adapter.quotes("RM-GULA-001") if q["LIFNR"] == "100109")
    recent = rpt_features.recent_ordered_kg(adapter.po_lines(), "100109", "2026-09-30")
    row = rpt_features.hypothetical_row("CAND-100109", quote, 10_000, "2026-09-30", 17550.0,
                                        adapter.receipts("100109"), recent)
    assert row["EBELN"] == "CAND-100109" and row["SPLIT"] == "predict"
    assert (row["BEDAT"], row["EINDT"], row["LEAD_DAYS"]) == ("2026-09-30", "2026-10-03", 3)
    assert row["PRICE_VS_MARKET"] == round(17550 / 17550.0, 4)
    assert row["RAINY_SEASON"] == 0 and row["LEBARAN_WINDOW"] == 0
    assert row["LATE"] is None and row["DELAY_DAYS"] is None
    assert 0 < row["SUPPLIER_UTIL_30D"]
