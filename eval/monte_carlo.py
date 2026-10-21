"""Monte Carlo of GESIT's pick against simple baselines (FR-EVA-4, simulated data).

    python -m eval.monte_carlo --scenario all --runs 1000 --seed 42 [--json PATH]
        [--worlds 1,2,3 | --worlds predeclared] [--downtime-sweep 50000000,150000000]
        [--baseline-table rpt|same|both] [--summary]

Options (all numbers simulated):
- --worlds: extra simulated worlds (eval/worlds.py), each generated into a temporary folder
  with another generator seed; the tracked dataset is always the first world. Per world the
  cases are built once per mode with the real agents code, then simulated. A world whose
  predictions fell back to the posterior (after one retry), or whose ranked table lacks a
  strategy's row, is excluded from the cross-world summary and listed.
- --downtime-sweep: (a) decisions fixed at the dataset's downtime cost, realised cost at
  each value; (b) decisions re-made: the ranked tables rebuilt with the downtime cost of
  the inventory row overridden in eval only (DowntimeAdapter), then realised at that value.
  SAP-RPT answers are replayed from the main build where the rows are the same.
- --baseline-table: "rpt" = baselines from the RPT_ONLY table for both GESIT strategies
  (as before); "same" = GESIT_LEARNING against the baselines of its own LEARNING table;
  "both" (default) = the RPT_ONLY block, then a labelled LEARNING-table block.
- --summary: one compact table per scenario and for the total, instead of the full tables.

Cases: the three cases of the one-world run, built exactly as `python -m agents.run all`
builds them (Detection with every notice on file, Supplier Evaluation with Detection's full
flagged list, spare capacity in urgency order, EXCLUDE_FLAGGED_SUPPLIERS as the code runs
it, the fastest mark only for new orders that cover the full quantity from one supplier).
Scenario S1 = the case for PO 4500000431, S2 = PO 4500000432, S3 = PO 4500000433.

The cases are built twice, once per delay-risk mode (GESIT_RELIABILITY_BLEND):
- RPT_ONLY: P(late) from SAP-RPT alone (the switch off, GESIT before the blend),
- LEARNING: the 50:50 blend of SAP-RPT and the reliability posterior (the switch on).

Strategies per case:
- GESIT_RPT_ONLY: the #1 row of the RPT_ONLY ranked table.
- GESIT_LEARNING: the #1 row of the LEARNING ranked table.
- ALWAYS_CHEAPEST, ALWAYS_FASTEST, KEEP_CURRENT_PO: the cheapest row, the fastest row and
  the keep row of the RPT_ONLY ranked table (the baselines as before). A case without a
  fastest row stops the run.

Predictions (SAP-RPT, or its posterior fallback) are made once per case and mode before
the loop, by Detection and Supplier Evaluation. The loop calls no predictor and no LLM.

Outcome model: the agent decided with predicted P(late); the world draws with its true
behaviour, `World.sample_delivery(lifnr, promised_date, qty, rng=...)` from
data_gen/world.py (true on-time probability, drift, season; utilization 0, since every
order fits in spare capacity). Common random numbers: in run r, the supplier LIFNR with
promised date d gets one draw from a generator seeded with (seed, r, LIFNR, d), shared by
every strategy and case that orders from it on that date.

Realised stock-out days of an order mix = quantity-weighted average over its parts of
max(0, real arrival - stock-out date), real arrival = promised date + sampled delay
(today + LEAD_DAYS for a new order; the expected arrival from Detection for keeping the
PO: S1 the delay notice's date, S3 the remainder's date, S2 the PO date). Short shipments
(the received quantity) are not counted.
Realised cost = purchase cost of the row (kg x NETPR, the revised price for keeping the PO)
+ realised stock-out days x DOWNTIME_COST_PER_DAY from the same ranked table.

The calibration table puts the predicted P(late) (SAP-RPT, posterior, used) next to the
world's true late probability at the promised date, for every supplier in a ranked table.

Only eval/ may use data_gen/world.py (D-1).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol

import numpy as np

from adapters import CsvAdapter, default_adapter
from agents.detection import DetectionAgent
from agents.run import DEFAULT_MATNR, notices_on_file
from agents.supplier_evaluation import SupplierEvaluationAgent
from eval import worlds as world_gen
from integrations.sap.rpt_client import RptClient
from scoring import reliability, supplier_score

DATA_GEN = Path(__file__).resolve().parent.parent / "data_gen"

GESIT_RPT_ONLY = "GESIT_RPT_ONLY"
GESIT_LEARNING = "GESIT_LEARNING"
ALWAYS_CHEAPEST = "ALWAYS_CHEAPEST"
ALWAYS_FASTEST = "ALWAYS_FASTEST"
KEEP_CURRENT_PO = "KEEP_CURRENT_PO"
STRATEGIES = (GESIT_RPT_ONLY, GESIT_LEARNING, ALWAYS_CHEAPEST, ALWAYS_FASTEST, KEEP_CURRENT_PO)
BASELINES = (ALWAYS_CHEAPEST, ALWAYS_FASTEST, KEEP_CURRENT_PO)
# Paired comparisons printed per scenario: (strategy, the ones it is compared against).
COMPARISONS = ((GESIT_RPT_ONLY, BASELINES),
               (GESIT_LEARNING, BASELINES + (GESIT_RPT_ONLY,)))
MODES = {"RPT_ONLY": False, "LEARNING": True}   # mode -> GESIT_RELIABILITY_BLEND
SCENARIOS = ("S1", "S2", "S3")
Z_95 = 1.96   # normal approximation for the 95% interval of a mean paired difference
# 100108's built-in drift starts on 1 Mar 2026 (PRD section 6, Dataset status); the history
# row splits its visible goods receipts there.
DRIFT_SUPPLIER, DRIFT_SPLIT_DATE = "100108", "2026-03-01"

STOCKOUT_DEFINITION = ("realised stock-out days = quantity-weighted average over the parts of "
                       "an order of max(0, real arrival - stock-out date); real arrival = "
                       "promised date + delay drawn from the simulated world's true behaviour")
# The baselines read from the LEARNING ranked table carry this suffix (--baseline-table).
SAME_SUFFIX = "@LEARNING_TABLE"
BASELINE_TABLES = ("rpt", "same", "both")
CHEAPER_HEADER, SAME_HEADER = "GESIT cheaper in % of runs", "equal cost in % of runs"
JUTA = 1_000_000


class DeliveryWorld(Protocol):
    def sample_delivery(self, lifnr: str, promised_date: Any, qty: float,
                        utilization: float = 0.0, rng: Any = None) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Part:
    """One supplier's share of an order: kilograms and promised arrival (ISO date)."""
    lifnr: str
    menge: float
    promised: str


@dataclass(frozen=True)
class Choice:
    """What one strategy orders for one case, read from a ranked row."""
    strategy: str
    label: str
    parts: tuple[Part, ...]
    purchase_cost: int
    predicted_cost: int


@dataclass(frozen=True)
class Case:
    scenario: str
    ebeln: str
    qty: float
    stockout: str
    downtime_cost_per_day: int
    choices: dict[str, Choice]
    prediction_model: str
    # Calibration rows: LIFNR, promised date, P_RPT, P_POST, P_USED (eval/ only).
    calibration: tuple[dict[str, Any], ...] = field(default=())
    # The three baselines read from the LEARNING ranked table (--baseline-table same).
    same_table: dict[str, Choice] = field(default_factory=dict)


def all_choices(case: Case) -> dict[str, Choice]:
    """Every order simulated for a case: the strategies, then the LEARNING-table baselines."""
    return {**case.choices, **{name + SAME_SUFFIX: ch for name, ch in case.same_table.items()}}


class DowntimeAdapter(CsvAdapter):
    """The dataset with another downtime cost per day in the inventory row (eval only, for
    the re-made decisions of the downtime sweep). Every other table is read unchanged."""

    def __init__(self, data_dir: str | os.PathLike[str], downtime_cost_per_day: int) -> None:
        super().__init__(data_dir)
        self.downtime_cost_per_day = int(downtime_cost_per_day)

    def inventory(self, matnr: str) -> dict[str, Any] | None:
        row = super().inventory(matnr)
        return None if row is None else {**row, "DOWNTIME_COST_PER_DAY": self.downtime_cost_per_day}


def _rows_key(rows: list[Mapping[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


class ReplayRpt:
    """Wraps an RPT client. While recording it passes every call through (the main build
    calls SAP-RPT exactly as without it) and keeps each answer by blend switch, context and
    query rows; while replaying it answers a recorded query from the record and calls
    SAP-RPT only for rows it has not seen. Errors are passed through and never recorded."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.record: dict[tuple[str, str, str], dict[str, float]] = {}
        self.replay = False
        self.replayed = 0

    @property
    def model_id(self) -> str:
        return self.inner.model_id

    @property
    def retries(self) -> list[str]:
        return getattr(self.inner, "retries", [])

    def predict_late(self, context_rows: list[Mapping[str, Any]],
                     query_rows: list[Mapping[str, Any]]) -> dict[str, float]:
        key = (os.environ.get("GESIT_RELIABILITY_BLEND", ""), _rows_key(context_rows),
               _rows_key(query_rows))
        if self.replay and key in self.record:
            self.replayed += 1
            return dict(self.record[key])
        out = self.inner.predict_late(context_rows, query_rows)
        self.record.setdefault(key, dict(out))
        return out


def load_world() -> DeliveryWorld:
    """The simulated world's true delivery behaviour (data_gen/world.py; prices not needed)."""
    if str(DATA_GEN) not in sys.path:
        sys.path.insert(0, str(DATA_GEN))
    from world import World   # noqa: PLC0415  (only eval/ may import it, D-1)
    return World(anchors={})


@contextmanager
def blend_switch(on: bool) -> Iterator[None]:
    """GESIT_RELIABILITY_BLEND for one mode, restored afterwards."""
    before = os.environ.get("GESIT_RELIABILITY_BLEND")
    os.environ["GESIT_RELIABILITY_BLEND"] = "1" if on else "0"
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("GESIT_RELIABILITY_BLEND", None)
        else:
            os.environ["GESIT_RELIABILITY_BLEND"] = before


def parts_of(row: dict[str, Any]) -> tuple[Part, ...]:
    """The parts a ranked row orders: one supplier, or the two of a split order."""
    parts = [Part(row["LIFNR"], float(row["MENGE"]), row["ARRIVAL"])]
    if row.get("SPLIT_WITH"):
        split = row["SPLIT_WITH"]
        parts.append(Part(split["LIFNR"], float(split["MENGE"]), split["ARRIVAL"]))
    return tuple(parts)


def label_of(row: dict[str, Any]) -> str:
    if row["OPTION"] == supplier_score.OPTION_KEEP:
        return f"keep PO {row['KEEP_EBELN']}"
    if row.get("SPLIT_WITH"):
        split = row["SPLIT_WITH"]
        return f"{row['LIFNR']} {row['MENGE']:,.0f} kg + {split['LIFNR']} {split['MENGE']:,.0f} kg"
    return f"{row['LIFNR']} {row['MENGE']:,.0f} kg"


def _choice(name: str, row: dict[str, Any]) -> Choice:
    return Choice(name, label_of(row), parts_of(row), int(row["PURCHASE_COST"]),
                  int(row["EXPECTED_COST"]))


def choices_for(table: dict[str, Any], gesit: str = GESIT_RPT_ONLY,
                with_baselines: bool = True) -> dict[str, Choice]:
    """Strategies' rows from one ranked table (FR-SEL-2, FR-SEL-3 marks): the #1 row as
    `gesit`, plus the three baselines unless `with_baselines` is False."""
    rows = table["CANDIDATES"]
    picked = {gesit: rows[0] if rows else None}
    if with_baselines:
        picked.update({ALWAYS_CHEAPEST: next((r for r in rows if r["IS_CHEAPEST"]), None),
                       ALWAYS_FASTEST: next((r for r in rows if r["IS_FASTEST"]), None),
                       KEEP_CURRENT_PO: table.get("KEEP")})
    missing = [name for name, row in picked.items() if row is None]
    if missing:
        raise ValueError(f"no row for {', '.join(missing)} in the ranked table; the strategy "
                         "is not defined for this case")
    return {name: _choice(name, row) for name, row in picked.items()}


def scenario_of(ebeln: str, scenarios: list[dict[str, Any]]) -> str | None:
    """The scripted scenario of a PO, or None for a PO flagged without a scripted notice."""
    return next((s["id"].upper() for s in scenarios if s["ebeln"] == ebeln), None)


def _evaluate(adapter: CsvAdapter, rpt_client: RptClient) -> tuple[dict[str, Any], dict[str, Any]]:
    """Detection and Supplier Evaluation once, as `agents.run all --no-llm` runs them."""
    detection = DetectionAgent(adapter=adapter, rpt_client=rpt_client).detect(
        DEFAULT_MATNR, notices_on_file("all", adapter.scenarios()))
    evaluation = SupplierEvaluationAgent(adapter=adapter, rpt_client=rpt_client).evaluate(
        detection, detection["FLAGGED_POS"])
    return detection, evaluation


def _model_text(pred: dict[str, Any]) -> str:
    return pred["MODEL_ID"] + (" (fallback)" if pred["FALLBACK"] else "")


def calibration_rows(case: dict[str, Any], learning_table: dict[str, Any],
                     rpt_table: dict[str, Any], adapter: CsvAdapter,
                     today: str) -> tuple[dict[str, Any], ...]:
    """P_RPT, P_POST and P_USED per supplier in either ranked table, at its promised date."""
    sources = learning_table.get("P_LATE_SOURCES") or {}
    rpt_p = {r["LIFNR"]: r["P_LATE"] for r in rpt_table["CANDIDATES"]}
    arrivals: dict[str, str] = {}
    for table in (rpt_table, learning_table):
        for row in table["CANDIDATES"]:
            for part in parts_of(row):
                arrivals.setdefault(part.lifnr, part.promised)
    out = []
    for lifnr, promised in sorted(arrivals.items()):
        if row_is_keep := (lifnr == case["LIFNR"]):
            src = case["CURRENT_PO"].get("P_LATE_SOURCES") or {}
        else:
            src = sources.get(lifnr) or {}
        if src:
            p_rpt, p_post, p_used = src["P_RPT"], src["P_POST"], src["P_USED"]
        else:   # only in the RPT_ONLY table (no spare capacity left in the LEARNING run)
            p_rpt = rpt_p.get(lifnr)
            p_post = reliability.posterior_before(adapter.receipts(lifnr), today).p_late
            p_used = reliability.blend(p_rpt, p_post)
        out.append({"LIFNR": lifnr, "PROMISED": promised, "KEEP": row_is_keep,
                    "P_RPT": p_rpt, "P_POST": round(p_post, 4), "P_USED": round(p_used, 4)})
    return tuple(out)


def build_cases(adapter: CsvAdapter | None = None,
                rpt_client: RptClient | None = None) -> tuple[list[Case], dict[str, Any]]:
    """The cases in both modes: the RPT_ONLY tables give GESIT_RPT_ONLY and the baselines,
    the LEARNING tables give GESIT_LEARNING."""
    adapter = adapter or default_adapter()
    rpt_client = rpt_client or RptClient()
    runs = {}
    for mode, on in MODES.items():
        with blend_switch(on):
            runs[mode] = _evaluate(adapter, rpt_client)
    rpt_det, rpt_ev = runs["RPT_ONLY"]
    learn_det, learn_ev = runs["LEARNING"]
    learning = {c["EBELN"]: c for c in learn_ev["CASES"]}
    scenarios = adapter.scenarios()
    cases, same_errors = [], []
    for case in rpt_ev["CASES"]:
        scenario = scenario_of(case["EBELN"], scenarios)
        if scenario is None:   # flagged without a scripted notice: it only takes capacity
            continue
        table, other = case["SCORING"], learning[case["EBELN"]]
        choices = {**choices_for(table, GESIT_RPT_ONLY),
                   **choices_for(other["SCORING"], GESIT_LEARNING, with_baselines=False)}
        try:
            same = choices_for(other["SCORING"], GESIT_LEARNING)
            same_table = {name: same[name] for name in BASELINES}
        except ValueError as exc:   # only the LEARNING-table variant is undefined then
            same_table = {}
            same_errors.append(f"{scenario}: {exc}")
        cases.append(Case(
            scenario=scenario, ebeln=case["EBELN"],
            qty=float(case["QTY"]), stockout=case["NEED_BY"],
            downtime_cost_per_day=int(table["DOWNTIME_COST_PER_DAY"]),
            choices={name: choices[name] for name in STRATEGIES},
            prediction_model=(f"RPT_ONLY {_model_text(table['PREDICTION'])}, LEARNING "
                              f"{_model_text(other['SCORING']['PREDICTION'])} + posterior"),
            calibration=calibration_rows(other, other["SCORING"], table, adapter,
                                         learn_det["TODAY"]),
            same_table=same_table))
    predictions = [det["PREDICTION"] for det, _ in runs.values()] + [
        c["SCORING"]["PREDICTION"] for _, ev in runs.values() for c in ev["CASES"]]
    meta = {"DETECTION_MODEL": f"RPT_ONLY {_model_text(rpt_det['PREDICTION'])}, LEARNING "
                               f"{_model_text(learn_det['PREDICTION'])} + posterior",
            "BLEND_RULE": reliability.BLEND_RULE_TEXT,
            "EXCLUDE_FLAGGED_SUPPLIERS": rpt_ev["EXCLUDE_FLAGGED_SUPPLIERS"],
            "TODAY": rpt_det["TODAY"],
            "DRIFT_HISTORY": drift_history(adapter, rpt_det["TODAY"]),
            # Any SAP-RPT call of this build answered by the posterior fallback (FR-DET-4).
            "FALLBACK": any(p["FALLBACK"] for p in predictions),
            # Detection's urgency order per mode: scenario id, or "other PO <EBELN>".
            "FLAGGED_ORDER": {mode: [scenario_of(po["EBELN"], scenarios)
                                     or f"other PO {po['EBELN']}" for po in det["FLAGGED_POS"]]
                              for mode, (det, _) in runs.items()},
            "LEARNING_TABLE_ERRORS": same_errors}
    return cases, meta


def drift_history(adapter: CsvAdapter, today: str, lifnr: str = DRIFT_SUPPLIER,
                  split: str = DRIFT_SPLIT_DATE) -> dict[str, Any]:
    """Observed late share in the goods receipts the agents can see (EKBE, before today),
    before and after `split`. Not the world's truth."""
    receipts = [r for r in adapter.receipts(lifnr) if r["BUDAT"] < today]
    before = [r for r in receipts if r["BUDAT"] < split]
    after = [r for r in receipts if r["BUDAT"] >= split]
    return {"LIFNR": lifnr, "SPLIT_DATE": split,
            "BEFORE": {"LATE": sum(r["LATE"] for r in before), "N": len(before)},
            "AFTER": {"LATE": sum(r["LATE"] for r in after), "N": len(after)}}


def true_p_late(world: Any, lifnr: str, promised: str) -> float | None:
    """The world's true P(late) at the promised date (eval/ only), when the world tells it."""
    on_time = getattr(world, "on_time_prob", None)
    return None if on_time is None else 1.0 - float(on_time(lifnr, promised))


def realised_stockout_days(parts: tuple[Part, ...], delays: dict[tuple[str, str], int],
                           stockout: str) -> float:
    """Quantity-weighted average over the parts of max(0, real arrival - stock-out date)."""
    out = date.fromisoformat(stockout)
    total = sum(p.menge for p in parts)
    late = sum(p.menge * max(0, (date.fromisoformat(p.promised)
                                 + timedelta(days=delays[(p.lifnr, p.promised)]) - out).days)
               for p in parts)
    return late / total


def draw_delays(world: DeliveryWorld, keys: list[tuple[str, str]], seed: int,
                run: int) -> dict[tuple[str, str], int]:
    """One true delivery draw per (LIFNR, promised date) for run `run` (common random numbers)."""
    delays = {}
    for lifnr, promised in keys:
        rng = np.random.default_rng([seed, run, int(lifnr), date.fromisoformat(promised).toordinal()])
        delays[(lifnr, promised)] = int(world.sample_delivery(lifnr, promised, 1.0, rng=rng)["delay_days"])
    return delays


def simulate(cases: list[Case], world: DeliveryWorld, runs: int,
             seed: int) -> dict[str, dict[str, dict[str, np.ndarray]]]:
    """Per scenario and strategy: arrays of realised cost and stock-out days, one per run."""
    keys = sorted({(p.lifnr, p.promised) for c in cases for ch in all_choices(c).values()
                   for p in ch.parts})
    out = {c.scenario: {s: {"COST": np.zeros(runs), "OUT_DAYS": np.zeros(runs)}
                        for s in all_choices(c)} for c in cases}
    for run in range(runs):
        delays = draw_delays(world, keys, seed, run)
        for case in cases:
            for name, choice in all_choices(case).items():
                days = realised_stockout_days(choice.parts, delays, case.stockout)
                out[case.scenario][name]["OUT_DAYS"][run] = days
                out[case.scenario][name]["COST"][run] = (choice.purchase_cost
                                                         + days * case.downtime_cost_per_day)
    return out


def with_total(results: dict[str, dict[str, dict[str, np.ndarray]]]) -> dict[str, Any]:
    """Add TOTAL: per run, the sum over the scenarios of cost and stock-out days."""
    names = list(results)
    common = [s for s in results[names[0]] if all(s in results[n] for n in names)]
    total = {s: {k: sum(results[n][s][k] for n in names) for k in ("COST", "OUT_DAYS")}
             for s in common}
    return {**results, "TOTAL": total}


def paired(a: np.ndarray, b: np.ndarray) -> dict[str, Any]:
    """Mean of a - b per run with a 95% interval (normal approximation); negative: a costs less."""
    diff = a - b
    half = Z_95 * float(diff.std(ddof=1)) / math.sqrt(len(diff)) if len(diff) > 1 else 0.0
    return {"MEAN_DIFF": float(diff.mean()),
            "CI95": [float(diff.mean()) - half, float(diff.mean()) + half],
            "SHARE_CHEAPER": float((diff < 0).mean()), "SHARE_SAME": float((diff == 0).mean())}


def summarise(series: dict[str, dict[str, np.ndarray]]) -> dict[str, Any]:
    """Statistics for one scenario: per strategy, and the paired comparisons."""
    per = {}
    for name in series:
        cost, days = series[name]["COST"], series[name]["OUT_DAYS"]
        per[name] = {"MEAN_COST": float(cost.mean()), "P90_COST": float(np.percentile(cost, 90)),
                     "MEAN_OUT_DAYS": float(days.mean()), "P_ANY_OUT": float((days > 0).mean())}
    versus = {strategy: {other: paired(series[strategy]["COST"], series[other]["COST"])
                         for other in others}
              for strategy, others in COMPARISONS}
    out = {"STRATEGIES": per, "VERSUS": versus,
           # For --summary: each strategy minus GESIT_LEARNING (positive: it costs more).
           "VS_GESIT_LEARNING": {s: paired(series[s]["COST"], series[GESIT_LEARNING]["COST"])
                                 for s in STRATEGIES if s != GESIT_LEARNING}}
    if all(b + SAME_SUFFIX in series for b in BASELINES):
        out["VERSUS_LEARNING_TABLE"] = {GESIT_LEARNING: {
            b: paired(series[GESIT_LEARNING]["COST"], series[b + SAME_SUFFIX]["COST"])
            for b in BASELINES}}
    return out


def select_cases(cases: list[Case], scenario: str, baseline_table: str = "both") -> list[Case]:
    """The cases of `scenario` ("all" or S1 to S3) in scenario order; with "rpt" the
    LEARNING-table baselines are left out, so only the orders of before are simulated."""
    wanted = SCENARIOS if scenario.upper() == "ALL" else (scenario.upper(),)
    picked = sorted((c for c in cases if c.scenario in wanted), key=lambda c: c.scenario)
    return [replace(c, same_table={}) for c in picked] if baseline_table == "rpt" else picked


def run(scenario: str = "all", runs: int = 1000, seed: int = 42,
        adapter: CsvAdapter | None = None, rpt_client: RptClient | None = None,
        world: DeliveryWorld | None = None, baseline_table: str = "both") -> dict[str, Any]:
    """Build the cases once per mode, simulate, and summarise (the numbers of the tables)."""
    report, _, _ = run_detailed(scenario, runs, seed, adapter, rpt_client, world, baseline_table)
    return report


def run_detailed(scenario: str = "all", runs: int = 1000, seed: int = 42,
                 adapter: CsvAdapter | None = None, rpt_client: Any = None,
                 world: DeliveryWorld | None = None, baseline_table: str = "both",
                 built: tuple[list[Case], dict[str, Any]] | None = None,
                 ) -> tuple[dict[str, Any], list[Case], dict[str, Any]]:
    """`run`, also returning the cases and the per-run arrays (for the downtime sweep).
    `built` reuses cases already built for this dataset."""
    cases, meta = built or build_cases(adapter, rpt_client)
    cases = select_cases(cases, scenario, baseline_table)
    world = world or load_world()
    started = time.perf_counter()
    results = simulate(cases, world, runs, seed)
    if len(cases) > 1:
        results = with_total(results)
    calibration = []
    for c in cases:
        for row in c.calibration:
            truth = true_p_late(world, row["LIFNR"], row["PROMISED"])
            calibration.append({"SCENARIO": c.scenario, **row,
                                "P_TRUE": None if truth is None else round(truth, 4)})
    def choice_rows(choices: dict[str, Choice]) -> dict[str, Any]:
        return {n: {"LABEL": ch.label, "PURCHASE_COST": ch.purchase_cost,
                    "PREDICTED_EXPECTED_COST": ch.predicted_cost} for n, ch in choices.items()}

    report = {
        "DATA_NOTE": "simulated data", "SEED": seed, "RUNS": runs, **meta,
        "BASELINE_TABLE": baseline_table,
        "LOOP_SECONDS": round(time.perf_counter() - started, 2),
        "STOCKOUT_DEFINITION": STOCKOUT_DEFINITION,
        "CALIBRATION": calibration,
        "CASES": {c.scenario: {"EBELN": c.ebeln, "QTY": c.qty, "STOCKOUT_DATE": c.stockout,
                               "DOWNTIME_COST_PER_DAY": c.downtime_cost_per_day,
                               "PREDICTION_MODEL": c.prediction_model,
                               "CHOICES": choice_rows(c.choices),
                               "CHOICES_LEARNING_TABLE": choice_rows(c.same_table)}
                  for c in cases},
        "RESULTS": {name: summarise(series) for name, series in results.items()},
    }
    return report, cases, results


# ------------------------------------------------------------------ downtime cost sweep
def sweep_fixed(cases: list[Case], results: dict[str, Any],
                values: list[int]) -> list[dict[str, Any]]:
    """Part (a): every strategy's order as decided at the dataset's downtime cost; realised
    cost per run = purchase cost + realised stock-out days x each value in the sweep."""
    rows = []
    for value in values:
        totals = {}
        for s in STRATEGIES:
            per_run = sum(c.choices[s].purchase_cost + results[c.scenario][s]["OUT_DAYS"] * value
                          for c in cases)
            totals[s] = float(per_run.mean())
        rows.append({"DOWNTIME_COST_PER_DAY": value, "TOTAL_MEAN_COST": totals,
                     "WINNER": winners(totals)})
    return rows


def winners(totals: dict[str, float]) -> list[str]:
    """The strategies with the lowest mean cost (several on an exact tie)."""
    low = min(totals.values())
    return [s for s, v in totals.items() if v == low]


def sweep_remade(data_dir: Path, rpt: ReplayRpt, world: DeliveryWorld, values: list[int],
                 scenario: str, runs: int, seed: int) -> list[dict[str, Any]]:
    """Part (b): the ranked tables rebuilt with each downtime cost (DowntimeAdapter, eval
    only; scoring code unchanged), so every strategy's choice can change, then realised at
    the same value with the same draws. SAP-RPT answers are replayed where the rows match
    the main build, so the dataset's own value reproduces the main results."""
    rpt.replay = True
    rows = []
    for value in values:
        cases, meta = build_cases(DowntimeAdapter(data_dir, value), rpt)
        cases = select_cases(cases, scenario, "rpt")
        results = simulate(cases, world, runs, seed)
        totals = {s: float(sum(results[c.scenario][s]["COST"] for c in cases).mean())
                  for s in STRATEGIES}
        rows.append({"DOWNTIME_COST_PER_DAY": value, "TOTAL_MEAN_COST": totals,
                     "WINNER": winners(totals), "FALLBACK": meta["FALLBACK"],
                     "CHOICES": {c.scenario: {s: c.choices[s].label for s in STRATEGIES}
                                 for c in cases}})
    return rows


# ------------------------------------------------------------------ many worlds
def derived_seed(seed: int, generator_seed: int) -> int:
    """Simulation seed of an extra world, reproducible from --seed and its generator seed."""
    return int(np.random.SeedSequence([seed, generator_seed]).generate_state(1)[0])


def world_result(label: str, generator_seed: int, adapter: CsvAdapter, rpt_client: Any,
                 world: DeliveryWorld, runs: int, seed: int, scenario: str,
                 baseline_table: str,
                 built: tuple[list[Case], dict[str, Any]] | None = None) -> dict[str, Any]:
    """One world: build (retried once on a posterior fallback), simulate, summarise.
    STATUS "ok" is used in the cross-world summary; "fallback" and "undefined" are not."""
    started = time.perf_counter()
    status, reason, report, attempts = "ok", None, None, 0
    for attempts in (1, 2):
        try:
            report, _, _ = run_detailed(scenario, runs, seed, adapter, rpt_client, world,
                                        baseline_table, built if attempts == 1 else None)
        except ValueError as exc:   # a ranked table without a strategy's row
            status, reason, report = "undefined", str(exc), None
            break
        if not report["FALLBACK"]:
            break
    wanted = SCENARIOS if scenario.upper() == "ALL" else (scenario.upper(),)
    if report and report["FALLBACK"]:
        status, reason = "fallback", "SAP-RPT fell back to the posterior, also after one retry"
    elif report and tuple(report["CASES"]) != tuple(wanted):
        status, reason = "undefined", f"cases found: {', '.join(report['CASES']) or 'none'}"
    return {"WORLD": label, "GENERATOR_SEED": generator_seed, "SIM_SEED": seed,
            "STATUS": status, "REASON": reason, "ATTEMPTS": attempts,
            "SECONDS": round(time.perf_counter() - started, 1),
            "FLAGGED_ORDER": report["FLAGGED_ORDER"] if report else None,
            "CHOICES": ({s: {n: ch["LABEL"] for n, ch in c["CHOICES"].items()}
                         for s, c in report["CASES"].items()} if report else None),
            "RESULTS": report["RESULTS"] if report else None}


def run_worlds(generator_seeds: tuple[int, ...], scenario: str, runs: int, seed: int,
               baseline_table: str, tracked: dict[str, Any],
               rpt_for: Callable[[str], Any], world: DeliveryWorld | None = None,
               progress: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    """The tracked world (already simulated, `tracked`) plus one generated world per seed."""
    world = world or load_world()
    say = progress or (lambda text: None)
    out = [tracked]
    total = len(generator_seeds) + 1
    for n, generator_seed in enumerate(generator_seeds, 2):
        label = f"seed {generator_seed}"
        say(f"World {n} of {total} (generator seed {generator_seed}): generating and building ...")
        started = time.perf_counter()
        with world_gen.temporary_world(generator_seed) as folder:
            result = world_result(label, generator_seed, CsvAdapter(folder), rpt_for(label),
                                  world, runs, derived_seed(seed, generator_seed), scenario,
                                  baseline_table)
        result["SECONDS"] = round(time.perf_counter() - started, 1)
        say(f"World {n} of {total}: {result['STATUS']}, {result['SECONDS']} s")
        out.append(result)
    return out


def _stats(diffs: list[float]) -> dict[str, Any]:
    return {"MEAN_DIFF": float(np.mean(diffs)) if diffs else None,
            "CHEAPER_IN": sum(d < 0 for d in diffs), "EQUAL_IN": sum(d == 0 for d in diffs),
            "OF": len(diffs), "MIN_DIFF": min(diffs) if diffs else None,
            "MAX_DIFF": max(diffs) if diffs else None}


def cross_world(worlds: list[dict[str, Any]]) -> dict[str, Any]:
    """Over the worlds with STATUS ok: the mean of each strategy's total mean cost, and for
    each GESIT strategy against each baseline the mean, the count of worlds where GESIT is
    cheaper, and the smallest and largest difference (GESIT minus the other one)."""
    used = [w for w in worlds if w["STATUS"] == "ok"]
    key = lambda w: "TOTAL" if "TOTAL" in w["RESULTS"] else next(iter(w["RESULTS"]))
    res = [w["RESULTS"][key(w)] for w in used]
    out: dict[str, Any] = {
        "WORLDS_USED": [w["WORLD"] for w in used],
        "WORLDS_EXCLUDED": [{"WORLD": w["WORLD"], "STATUS": w["STATUS"], "REASON": w["REASON"]}
                            for w in worlds if w["STATUS"] != "ok"],
        "MEAN_TOTAL_COST": {s: (float(np.mean([r["STRATEGIES"][s]["MEAN_COST"] for r in res]))
                                if res else None) for s in STRATEGIES},
        "VERSUS": {g: {o: _stats([r["VERSUS"][g][o]["MEAN_DIFF"] for r in res]) for o in others}
                   for g, others in COMPARISONS},
    }
    same = [r for r in res if "VERSUS_LEARNING_TABLE" in r]
    if same:
        out["VERSUS_LEARNING_TABLE"] = {GESIT_LEARNING: {
            b: _stats([r["VERSUS_LEARNING_TABLE"][GESIT_LEARNING][b]["MEAN_DIFF"] for r in same])
            for b in BASELINES}}
    return out


def _rp(x: float) -> str:
    return f"Rp{x:,.0f}"


def _signed_rp(x: float) -> str:
    return ("-" if x < 0 else "") + _rp(abs(x))


def _p(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def format_calibration(report: dict[str, Any]) -> list[str]:
    lines = ["Calibration: predicted P(late) against the simulated world's true P(late) at the "
             "promised date",
             f"{'SCENARIO':<10}{'LIFNR':<8}{'PROMISED':<12}{'P_RPT':>7}{'P_POST':>8}{'P_USED':>8}"
             f"{'TRUE':>7}  NOTE"]
    for row in report["CALIBRATION"]:
        lines.append(f"{row['SCENARIO']:<10}{row['LIFNR']:<8}{row['PROMISED']:<12}"
                     f"{_p(row['P_RPT']):>7}{_p(row['P_POST']):>8}{_p(row['P_USED']):>8}"
                     f"{_p(row['P_TRUE']):>7}  {'keep the current PO' if row['KEEP'] else '-'}")
    h = report["DRIFT_HISTORY"]
    b, a = h["BEFORE"], h["AFTER"]
    lines.append(f"History the agents see, {h['LIFNR']} goods receipts before "
                 f"{report['TODAY']}: before {h['SPLIT_DATE']} {b['LATE']} late of {b['N']} "
                 f"({b['LATE'] / max(1, b['N']):.0%}), from {h['SPLIT_DATE']} {a['LATE']} late "
                 f"of {a['N']} ({a['LATE'] / max(1, a['N']):.0%}).")
    lines.append("P_RPT = SAP-RPT, P_POST = reliability posterior, P_USED = what GESIT_LEARNING "
                 "decided with, TRUE = 1 - World.on_time_prob (eval/ only, never seen by the agents).")
    return lines


def _versus_header(strategy: str) -> str:
    return (f"{strategy + ' vs':<20}{'MEAN DIFF':>16}{'95% INTERVAL':>40}"
            f"{CHEAPER_HEADER:>28}{SAME_HEADER:>25}")


def _versus_line(other: str, v: dict[str, Any]) -> str:
    interval = f"[{_signed_rp(v['CI95'][0])}, {_signed_rp(v['CI95'][1])}]"
    return (f"  {other:<18}{_signed_rp(v['MEAN_DIFF']):>16}{interval:>40}"
            f"{v['SHARE_CHEAPER']:>28.1%}{v['SHARE_SAME']:>25.1%}")


def limitations(report: dict[str, Any]) -> list[str]:
    return [
        "Limitations: the stock-out date is fixed at the one Detection computed on "
        f"{report['TODAY']}; short shipments are not counted; there is one decision point "
        f"({report['TODAY']}); extra worlds differ only by generator seed (the history, "
        "quotes and spare capacity the agents see; the true supplier behaviour is the same "
        "in every world); all data is simulated.",
    ]


def format_report(report: dict[str, Any], baseline_table: str = "rpt") -> str:
    """The full tables. With "same" the GESIT_LEARNING comparisons against the RPT_ONLY
    baselines are left out here (see `format_learning_table`)."""
    lines = [f"GESIT Monte Carlo against baselines ({report['DATA_NOTE']}), FR-EVA-4", ""]
    lines += format_calibration(report)
    comparisons = COMPARISONS if baseline_table != "same" else COMPARISONS[:1]
    for name, res in report["RESULTS"].items():
        case = report["CASES"].get(name)
        lines.append("")
        if case:
            lines.append(f"Scenario {name}: replacement for PO {case['EBELN']}, "
                         f"{case['QTY']:,.0f} kg, stock-out date {case['STOCKOUT_DATE']}, "
                         f"predictions {case['PREDICTION_MODEL']}")
        else:
            lines.append(f"TOTAL over {', '.join(report['CASES'])} (per run, costs and stock-out "
                         "days added up)")
        lines.append(f"{'STRATEGY':<17}{'CHOICE':<36}{'PREDICTED Rp':>15}{'MEAN Rp':>15}"
                     f"{'P90 Rp':>15}{'MEAN OUT d':>11}{'P(any out)':>11}")
        for s in STRATEGIES:
            st = res["STRATEGIES"][s]
            label = case["CHOICES"][s]["LABEL"] if case else "-"
            predicted = (f"{case['CHOICES'][s]['PREDICTED_EXPECTED_COST']:,}" if case
                         else f"{sum(c['CHOICES'][s]['PREDICTED_EXPECTED_COST'] for c in report['CASES'].values()):,}")
            lines.append(f"{s:<17}{label:<36}{predicted:>15}{st['MEAN_COST']:>15,.0f}"
                         f"{st['P90_COST']:>15,.0f}{st['MEAN_OUT_DAYS']:>11.3f}"
                         f"{st['P_ANY_OUT']:>11.1%}")
        for strategy, others in comparisons:
            lines.append(_versus_header(strategy))
            for other in others:
                lines.append(_versus_line(other, res["VERSUS"][strategy][other]))
    lines += ["", *format_footer(report)]
    return "\n".join(lines)


def format_footer(report: dict[str, Any]) -> list[str]:
    downtime = {c["DOWNTIME_COST_PER_DAY"] for c in report["CASES"].values()}
    return [
        f"All figures are simulated data. Seed {report['SEED']}, {report['RUNS']:,} runs per "
        f"scenario and strategy, loop {report['LOOP_SECONDS']} s.",
        f"Predictions (made once per case and mode, before the loop): Detection "
        f"{report['DETECTION_MODEL']}. No SAP-RPT or LLM call inside the loop.",
        f"GESIT_RPT_ONLY decides with SAP-RPT alone; GESIT_LEARNING with {report['BLEND_RULE']}.",
        "ALWAYS_CHEAPEST, ALWAYS_FASTEST and KEEP_CURRENT_PO are built from the RPT_ONLY "
        "ranked table, as before.",
        f"Outcomes: true delivery behaviour of the simulated world (data_gen/world.py), one draw "
        f"per supplier and promised date per run, shared by all strategies.",
        f"Stock-out: {STOCKOUT_DEFINITION}. Short shipments are not counted.",
        "Cost = purchase cost (kg x NETPR; the revised price when keeping the PO) + stock-out "
        f"days x {', '.join(_rp(d) for d in sorted(downtime))} per day of downtime.",
        "PREDICTED = the expected cost of that row in the strategy's own ranked table (FR-SEL-2). "
        "MEAN DIFF = the strategy minus the other one per run; negative means the strategy costs "
        f"less. {CHEAPER_HEADER} and {SAME_HEADER} = share of runs where it costs less, or "
        "the same.",
        f"EXCLUDE_FLAGGED_SUPPLIERS: {report['EXCLUDE_FLAGGED_SUPPLIERS']}.",
        *limitations(report),
    ]


def format_learning_table(report: dict[str, Any]) -> list[str]:
    """--baseline-table same or both: GESIT_LEARNING against the baselines of its own table."""
    lines = ["", "Baselines from the LEARNING ranked table (--baseline-table same, simulated data): "
             "the cheapest, fastest and keep rows of the table GESIT_LEARNING decided from (the "
             "same spare capacity and marks it saw). GESIT_RPT_ONLY keeps the RPT_ONLY baselines."]
    if report.get("LEARNING_TABLE_ERRORS"):
        return lines + [f"Not defined: {'; '.join(report['LEARNING_TABLE_ERRORS'])}"]
    for name, res in report["RESULTS"].items():
        if "VERSUS_LEARNING_TABLE" not in res:
            continue
        case = report["CASES"].get(name)
        lines += ["", f"Scenario {name}, LEARNING table" if case
                  else f"TOTAL over {', '.join(report['CASES'])}, LEARNING table",
                  f"{'STRATEGY':<17}{'CHOICE':<36}{'MEAN Rp':>15}{'MEAN OUT d':>11}"
                  f"{'P(any out)':>11}  RPT_ONLY TABLE"]
        for s in (GESIT_LEARNING, *BASELINES):
            key = s if s == GESIT_LEARNING else s + SAME_SUFFIX
            st = res["STRATEGIES"][key]
            label, note = "-", "-"
            if case:
                label = (case["CHOICES"] if s == GESIT_LEARNING
                         else case["CHOICES_LEARNING_TABLE"])[s]["LABEL"]
                note = ("-" if s == GESIT_LEARNING else "same order"
                        if label == case["CHOICES"][s]["LABEL"] else "differs")
            lines.append(f"{s:<17}{label:<36}{st['MEAN_COST']:>15,.0f}"
                         f"{st['MEAN_OUT_DAYS']:>11.3f}{st['P_ANY_OUT']:>11.1%}  {note}")
        lines.append(_versus_header(GESIT_LEARNING))
        for other in BASELINES:
            lines.append(_versus_line(other, res["VERSUS_LEARNING_TABLE"][GESIT_LEARNING][other]))
    return lines


def _juta(x: float | None) -> str:
    return "n/a" if x is None else f"{x / JUTA:,.1f}"


def _signed_juta(x: float) -> str:
    return f"{x / JUTA:+,.1f}"


def format_summary(report: dict[str, Any]) -> list[str]:
    """--summary: the compact table per scenario and for the total (slide, demo step 3e)."""
    lines = [f"GESIT Monte Carlo summary ({report['DATA_NOTE']}), FR-EVA-4: seed "
             f"{report['SEED']}, {report['RUNS']:,} runs, costs in Rp juta (Rp1,000,000)"]
    for name, res in report["RESULTS"].items():
        lines += ["", f"{'Scenario ' + name if name != 'TOTAL' else 'TOTAL':<17}"
                      f"{'MEAN':>10}{'MINUS GESIT_LEARNING [95% interval]':>40}{'P(any out)':>12}"]
        for s in STRATEGIES:
            st = res["STRATEGIES"][s]
            if s == GESIT_LEARNING:
                diff = "-"
            else:
                v = res["VS_GESIT_LEARNING"][s]
                diff = (f"{_signed_juta(v['MEAN_DIFF'])} [{_signed_juta(v['CI95'][0])}, "
                        f"{_signed_juta(v['CI95'][1])}]")
            lines.append(f"{s:<17}{_juta(st['MEAN_COST']):>10}{diff:>40}{st['P_ANY_OUT']:>12.1%}")
    lines += ["", "MINUS GESIT_LEARNING = the strategy's cost minus GESIT_LEARNING's in the same "
              "run, mean and 95% interval; positive means the strategy costs more. Baselines from "
              "the RPT_ONLY ranked table.", *format_footer(report)]
    return lines


def format_worlds(worlds: list[dict[str, Any]], cross: dict[str, Any]) -> list[str]:
    """Per-world totals and the cross-world summary (Rp juta, simulated)."""
    tracked = worlds[0]
    base_order = (tracked["FLAGGED_ORDER"] or {}).get("RPT_ONLY")
    norm = lambda order: [x if not x.startswith("other PO") else "other PO" for x in order or []]
    short = {GESIT_RPT_ONLY: "RPT_ONLY", GESIT_LEARNING: "LEARNING", ALWAYS_CHEAPEST: "CHEAPEST",
             ALWAYS_FASTEST: "FASTEST", KEEP_CURRENT_PO: "KEEP"}
    lines = ["", f"Many worlds ({len(worlds)}, simulated data): total mean cost per world in Rp "
             "juta, each world simulated with its own seed derived from --seed and the "
             "generator seed (the tracked world uses --seed itself)",
             f"{'WORLD':<18}{'STATUS':<10}" + "".join(f"{short[s]:>10}" for s in STRATEGIES)
             + f"{'SECONDS':>9}  FLAGGED ORDER (Detection, RPT_ONLY / LEARNING)"]
    for w in worlds:
        key = None if not w["RESULTS"] else ("TOTAL" if "TOTAL" in w["RESULTS"]
                                             else next(iter(w["RESULTS"])))
        costs = "".join(f"{_juta(w['RESULTS'][key]['STRATEGIES'][s]['MEAN_COST']) if key else 'n/a':>10}"
                        for s in STRATEGIES)
        order = w["FLAGGED_ORDER"] or {}
        flagged = " / ".join(", ".join(order.get(m) or []) for m in MODES) or "n/a"
        lines.append(f"{w['WORLD']:<18}{w['STATUS']:<10}{costs}{w['SECONDS']:>9}  {flagged}")
    for w in worlds[1:]:
        order = w["FLAGGED_ORDER"] or {}
        if any(norm(order.get(m)) != norm((tracked["FLAGGED_ORDER"] or {}).get(m)) for m in MODES):
            lines.append(f"  {w['WORLD']}: Detection's flagged set or order differs from the "
                         f"tracked world ({', '.join(base_order or [])}); this world's own list "
                         "was used.")
    excluded = cross["WORLDS_EXCLUDED"]
    lines += ["", f"Cross-world summary over {len(cross['WORLDS_USED'])} of {len(worlds)} worlds "
              f"({len(excluded)} excluded"
              + "".join(f"; {e['WORLD']} {e['STATUS']}: {e['REASON']}" for e in excluded)
              + "), simulated data, Rp juta",
              "Mean over worlds of the total mean cost: "
              + ", ".join(f"{s} {_juta(v)}" for s, v in cross["MEAN_TOTAL_COST"].items())]

    def block(title: str, versus: dict[str, Any]) -> list[str]:
        out = [title, f"{'':<20}{'vs':<18}{'MEAN DIFF':>11}{'GESIT CHEAPER IN':>19}"
                      f"{'EQUAL IN':>10}{'SMALLEST DIFF':>15}{'LARGEST DIFF':>14}"]
        for g, others in versus.items():
            for o, st in others.items():
                if st["OF"] == 0:
                    out.append(f"{g:<20}{o:<18}{'n/a':>11}")
                    continue
                out.append(f"{g:<20}{o:<18}{_signed_juta(st['MEAN_DIFF']):>11}"
                           f"{str(st['CHEAPER_IN']) + ' of ' + str(st['OF']) + ' worlds':>19}"
                           f"{st['EQUAL_IN']:>10}"
                           f"{_signed_juta(st['MIN_DIFF']):>15}{_signed_juta(st['MAX_DIFF']):>14}")
        return out

    lines += block("Baselines from the RPT_ONLY ranked table (difference = GESIT minus the other "
                   "one, per world the mean over runs; negative: GESIT cheaper)", cross["VERSUS"])
    if "VERSUS_LEARNING_TABLE" in cross:
        lines += block("Baselines from the LEARNING ranked table (GESIT_LEARNING only)",
                       cross["VERSUS_LEARNING_TABLE"])
    return lines


def format_sweep(fixed: list[dict[str, Any]], remade: list[dict[str, Any]] | None) -> list[str]:
    short = {GESIT_RPT_ONLY: "RPT_ONLY", GESIT_LEARNING: "LEARNING", ALWAYS_CHEAPEST: "CHEAPEST",
             ALWAYS_FASTEST: "FASTEST", KEEP_CURRENT_PO: "KEEP"}
    head = (f"{'DOWNTIME Rp/day':>16}" + "".join(f"{short[s]:>10}" for s in STRATEGIES)
            + "  LOWEST TOTAL MEAN COST")
    lines = ["", "Downtime cost sensitivity, part (a): decisions fixed as made with the "
             "dataset's downtime cost, realised cost recomputed at each value (total mean cost, "
             "Rp juta, simulated data)", head]
    for row in fixed:
        lines.append(f"{row['DOWNTIME_COST_PER_DAY']:>16,}"
                     + "".join(f"{_juta(row['TOTAL_MEAN_COST'][s]):>10}" for s in STRATEGIES)
                     + "  " + ", ".join(row["WINNER"]))
    if remade is None:
        return lines
    lines += ["", "Downtime cost sensitivity, part (b): decisions re-made (ranked tables rebuilt "
              "with each value, eval-only override of the inventory row; scoring code unchanged), "
              "realised with the same value (total mean cost, Rp juta, simulated data)", head]
    for row in remade:
        lines.append(f"{row['DOWNTIME_COST_PER_DAY']:>16,}"
                     + "".join(f"{_juta(row['TOTAL_MEAN_COST'][s]):>10}" for s in STRATEGIES)
                     + "  " + ", ".join(row["WINNER"])
                     + ("  (SAP-RPT fallback in this build)" if row["FALLBACK"] else ""))
    lines.append("Choices per case in part (b) (GESIT_LEARNING; RPT_ONLY / CHEAPEST / FASTEST):")
    for row in remade:
        for scenario, ch in row["CHOICES"].items():
            lines.append(f"  {row['DOWNTIME_COST_PER_DAY']:>13,} {scenario}: {ch[GESIT_LEARNING]}; "
                         f"{ch[GESIT_RPT_ONLY]} / {ch[ALWAYS_CHEAPEST]} / {ch[ALWAYS_FASTEST]}")
    return lines


def dataset_seed(data_dir: Path) -> int | None:
    """Generator seed of a dataset folder (its meta.json), when it has one."""
    meta = Path(data_dir) / "meta.json"
    return int(json.loads(meta.read_text(encoding="utf-8"))["seed"]) if meta.exists() else None


def evaluate(scenario: str = "all", runs: int = 1000, seed: int = 42,
             baseline_table: str = "both", world_seeds: tuple[int, ...] = (),
             downtime_values: list[int] | None = None, adapter: CsvAdapter | None = None,
             rpt_client: Any = None, rpt_for: Callable[[str], Any] | None = None,
             world: DeliveryWorld | None = None,
             progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Everything the CLI prints, as one report: the main results on the tracked dataset,
    then (when asked) the many-worlds run and the downtime cost sweep."""
    started = time.perf_counter()
    adapter = adapter or default_adapter()
    inner = rpt_client or RptClient()
    rpt = ReplayRpt(inner) if downtime_values else inner
    world = world or load_world()
    built = build_cases(adapter, rpt)
    report, cases, results = run_detailed(scenario, runs, seed, adapter, rpt, world,
                                          baseline_table, built)
    out: dict[str, Any] = {
        "simulated": True,
        "SETTINGS": {"SCENARIO": scenario, "RUNS": runs, "SEED": seed,
                     "BASELINE_TABLE": baseline_table, "WORLDS_GENERATOR_SEEDS": list(world_seeds),
                     "TRACKED_GENERATOR_SEED": dataset_seed(adapter.data_dir),
                     "DOWNTIME_SWEEP": list(downtime_values or []),
                     "BLEND_WEIGHT_RPT": reliability.BLEND_WEIGHT_RPT},
        **report}
    if world_seeds:
        tracked_label = f"tracked (seed {dataset_seed(adapter.data_dir)})"
        tracked = world_result(tracked_label, dataset_seed(adapter.data_dir) or 0, adapter, rpt,
                               world, runs, seed, scenario, baseline_table, built)
        tracked["SECONDS"] = round(time.perf_counter() - started, 1)   # its build included
        if progress:
            progress(f"World 1 of {len(world_seeds) + 1} ({tracked_label}): {tracked['STATUS']}")
        out["WORLDS"] = run_worlds(tuple(world_seeds), scenario, runs, seed, baseline_table,
                                   tracked, rpt_for or (lambda label: inner), world, progress)
        out["CROSS_WORLD"] = cross_world(out["WORLDS"])
    if downtime_values:
        out["SENSITIVITY"] = {
            "DATASET_DOWNTIME_COST_PER_DAY": sorted({c.downtime_cost_per_day for c in cases}),
            "DECISIONS_FIXED": sweep_fixed(cases, results, downtime_values),
            "DECISIONS_REMADE": sweep_remade(adapter.data_dir, rpt, world, downtime_values,
                                             scenario, runs, seed),
            "SAP_RPT_ANSWERS_REPLAYED": rpt.replayed}
    out["TOTAL_SECONDS"] = round(time.perf_counter() - started, 1)
    return out


def format_all(out: dict[str, Any], summary: bool = False) -> str:
    baseline_table = out["SETTINGS"]["BASELINE_TABLE"]
    lines = format_summary(out) if summary else [format_report(out, baseline_table)]
    if baseline_table in ("same", "both"):
        lines += format_learning_table(out)
    if "WORLDS" in out:
        lines += format_worlds(out["WORLDS"], out["CROSS_WORLD"])
    if "SENSITIVITY" in out:
        s = out["SENSITIVITY"]
        lines += format_sweep(s["DECISIONS_FIXED"], s["DECISIONS_REMADE"])
    if "WORLDS" in out or "SENSITIVITY" in out:
        lines += ["", f"Whole run {out['TOTAL_SECONDS']} s. All figures are simulated data."]
    return "\n".join(lines)


def write_json(out: dict[str, Any], path: str) -> None:
    """--json: never into the tracked dataset folder."""
    target = Path(path).resolve()
    tracked = world_gen.TRACKED_OUT.resolve()
    if target == tracked or tracked in target.parents:
        raise SystemExit("--json: do not write into data_gen/out (tracked data)")
    target.write_text(json.dumps(out, indent=2), encoding="utf-8")


def _int_list(text: str) -> list[int]:
    return [int(x) for x in text.replace(" ", "").split(",") if x]


def _world_seeds(text: str) -> tuple[int, ...]:
    if text.strip().lower() == "predeclared":
        return world_gen.PREDECLARED_SEEDS
    return tuple(_int_list(text))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monte Carlo of GESIT against baselines (FR-EVA-4).")
    parser.add_argument("--scenario", default="all", choices=["S1", "S2", "S3", "all"])
    parser.add_argument("--runs", type=int, default=1000, help="runs per world (default 1000)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--json", metavar="PATH", help="also write every number as JSON to PATH")
    parser.add_argument("--worlds", type=_world_seeds, default=(), metavar="LIST",
                        help="extra worlds: comma-separated generator seeds, or 'predeclared' "
                             f"({','.join(map(str, world_gen.PREDECLARED_SEEDS))}); the tracked "
                             "dataset is always the first world")
    parser.add_argument("--downtime-sweep", type=_int_list, default=None, metavar="LIST",
                        help="downtime costs per day in Rp, comma-separated")
    parser.add_argument("--baseline-table", choices=BASELINE_TABLES, default="both")
    parser.add_argument("--summary", action="store_true",
                        help="compact tables instead of the full ones")
    args = parser.parse_args(argv)
    try:   # SAP-RPT credentials for the predictions, from .env (NFR-4)
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    progress = lambda text: print(text, file=sys.stderr, flush=True)
    out = evaluate(args.scenario, args.runs, args.seed, args.baseline_table, args.worlds,
                   args.downtime_sweep, progress=progress)
    print(format_all(out, args.summary))
    if args.json:
        write_json(out, args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
