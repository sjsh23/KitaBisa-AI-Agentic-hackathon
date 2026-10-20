"""Monte Carlo of GESIT's pick against simple baselines (FR-EVA-4, simulated data).

    python -m eval.monte_carlo --scenario all --runs 1000 --seed 42 [--json PATH]

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
import json
import math
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterator, Protocol

import numpy as np

from adapters import CsvAdapter, default_adapter
from agents.detection import DetectionAgent
from agents.run import DEFAULT_MATNR, notices_on_file
from agents.supplier_evaluation import SupplierEvaluationAgent
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


def scenario_of(ebeln: str, scenarios: list[dict[str, Any]]) -> str:
    return next(s["id"].upper() for s in scenarios if s["ebeln"] == ebeln)


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
    cases = []
    for case in rpt_ev["CASES"]:
        table, other = case["SCORING"], learning[case["EBELN"]]
        choices = {**choices_for(table, GESIT_RPT_ONLY),
                   **choices_for(other["SCORING"], GESIT_LEARNING, with_baselines=False)}
        cases.append(Case(
            scenario=scenario_of(case["EBELN"], adapter.scenarios()), ebeln=case["EBELN"],
            qty=float(case["QTY"]), stockout=case["NEED_BY"],
            downtime_cost_per_day=int(table["DOWNTIME_COST_PER_DAY"]),
            choices={name: choices[name] for name in STRATEGIES},
            prediction_model=(f"RPT_ONLY {_model_text(table['PREDICTION'])}, LEARNING "
                              f"{_model_text(other['SCORING']['PREDICTION'])} + posterior"),
            calibration=calibration_rows(other, other["SCORING"], table, adapter,
                                         learn_det["TODAY"])))
    meta = {"DETECTION_MODEL": f"RPT_ONLY {_model_text(rpt_det['PREDICTION'])}, LEARNING "
                               f"{_model_text(learn_det['PREDICTION'])} + posterior",
            "BLEND_RULE": reliability.BLEND_RULE_TEXT,
            "EXCLUDE_FLAGGED_SUPPLIERS": rpt_ev["EXCLUDE_FLAGGED_SUPPLIERS"],
            "TODAY": rpt_det["TODAY"],
            "DRIFT_HISTORY": drift_history(adapter, rpt_det["TODAY"])}
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
    keys = sorted({(p.lifnr, p.promised) for c in cases for ch in c.choices.values()
                   for p in ch.parts})
    out = {c.scenario: {s: {"COST": np.zeros(runs), "OUT_DAYS": np.zeros(runs)}
                        for s in c.choices} for c in cases}
    for run in range(runs):
        delays = draw_delays(world, keys, seed, run)
        for case in cases:
            for name, choice in case.choices.items():
                days = realised_stockout_days(choice.parts, delays, case.stockout)
                out[case.scenario][name]["OUT_DAYS"][run] = days
                out[case.scenario][name]["COST"][run] = (choice.purchase_cost
                                                         + days * case.downtime_cost_per_day)
    return out


def with_total(results: dict[str, dict[str, dict[str, np.ndarray]]]) -> dict[str, Any]:
    """Add TOTAL: per run, the sum over the scenarios of cost and stock-out days."""
    names = list(results)
    total = {s: {k: sum(results[n][s][k] for n in names) for k in ("COST", "OUT_DAYS")}
             for s in STRATEGIES}
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
    for name in STRATEGIES:
        cost, days = series[name]["COST"], series[name]["OUT_DAYS"]
        per[name] = {"MEAN_COST": float(cost.mean()), "P90_COST": float(np.percentile(cost, 90)),
                     "MEAN_OUT_DAYS": float(days.mean()), "P_ANY_OUT": float((days > 0).mean())}
    versus = {strategy: {other: paired(series[strategy]["COST"], series[other]["COST"])
                         for other in others}
              for strategy, others in COMPARISONS}
    return {"STRATEGIES": per, "VERSUS": versus}


def run(scenario: str = "all", runs: int = 1000, seed: int = 42,
        adapter: CsvAdapter | None = None, rpt_client: RptClient | None = None,
        world: DeliveryWorld | None = None) -> dict[str, Any]:
    """Build the cases once per mode, simulate, and summarise (the numbers of the tables)."""
    cases, meta = build_cases(adapter, rpt_client)
    wanted = SCENARIOS if scenario.upper() == "ALL" else (scenario.upper(),)
    cases = sorted((c for c in cases if c.scenario in wanted), key=lambda c: c.scenario)
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
    return {
        "DATA_NOTE": "simulated data", "SEED": seed, "RUNS": runs, **meta,
        "LOOP_SECONDS": round(time.perf_counter() - started, 2),
        "STOCKOUT_DEFINITION": STOCKOUT_DEFINITION,
        "CALIBRATION": calibration,
        "CASES": {c.scenario: {"EBELN": c.ebeln, "QTY": c.qty, "STOCKOUT_DATE": c.stockout,
                               "DOWNTIME_COST_PER_DAY": c.downtime_cost_per_day,
                               "PREDICTION_MODEL": c.prediction_model,
                               "CHOICES": {n: {"LABEL": ch.label,
                                               "PURCHASE_COST": ch.purchase_cost,
                                               "PREDICTED_EXPECTED_COST": ch.predicted_cost}
                                           for n, ch in c.choices.items()}}
                  for c in cases},
        "RESULTS": {name: summarise(series) for name, series in results.items()},
    }


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


def format_report(report: dict[str, Any]) -> str:
    lines = [f"GESIT Monte Carlo against baselines ({report['DATA_NOTE']}), FR-EVA-4", ""]
    lines += format_calibration(report)
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
        for strategy, others in COMPARISONS:
            lines.append(f"{strategy + ' vs':<20}{'MEAN DIFF':>16}{'95% INTERVAL':>40}"
                         f"{'CHEAPER':>10}{'SAME':>8}")
            for other in others:
                v = res["VERSUS"][strategy][other]
                interval = f"[{_signed_rp(v['CI95'][0])}, {_signed_rp(v['CI95'][1])}]"
                lines.append(f"  {other:<18}{_signed_rp(v['MEAN_DIFF']):>16}{interval:>40}"
                             f"{v['SHARE_CHEAPER']:>10.1%}{v['SHARE_SAME']:>8.1%}")
    downtime = {c["DOWNTIME_COST_PER_DAY"] for c in report["CASES"].values()}
    lines += [
        "",
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
        "less. CHEAPER and SAME = share of runs where it costs less, or the same.",
        f"EXCLUDE_FLAGGED_SUPPLIERS: {report['EXCLUDE_FLAGGED_SUPPLIERS']}.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monte Carlo of GESIT against baselines (FR-EVA-4).")
    parser.add_argument("--scenario", default="all", choices=["S1", "S2", "S3", "all"])
    parser.add_argument("--runs", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--json", metavar="PATH", help="also write the numbers as JSON to PATH")
    args = parser.parse_args(argv)
    try:   # SAP-RPT credentials for the predictions, from .env (NFR-4)
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    report = run(args.scenario, args.runs, args.seed)
    print(format_report(report))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
