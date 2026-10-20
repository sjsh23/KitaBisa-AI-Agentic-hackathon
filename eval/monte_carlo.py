"""Monte Carlo of GESIT's pick against simple baselines (FR-EVA-4, simulated data).

    python -m eval.monte_carlo --scenario all --runs 1000 --seed 42 [--json PATH]

Cases: the three cases of the one-world run, built exactly as `python -m agents.run all`
builds them (Detection with every notice on file, Supplier Evaluation with Detection's full
flagged list, spare capacity in urgency order, EXCLUDE_FLAGGED_SUPPLIERS as the code runs
it, the fastest mark only for new orders that cover the full quantity from one supplier).
Scenario S1 = the case for PO 4500000431, S2 = PO 4500000432, S3 = PO 4500000433.

Strategies per case, all read from the same ranked table (`tools.score_suppliers`):
- GESIT: the #1 row (new order, split order, or keep the current PO).
- ALWAYS_CHEAPEST: the row marked cheapest new order.
- ALWAYS_FASTEST: the row marked fastest new order. A case without one stops the run.
- KEEP_CURRENT_PO: the keep row (for S3 the 7,200 kg remainder), as it stands.

Predictions (SAP-RPT, or its posterior fallback) are made once per case before the loop,
by Detection and Supplier Evaluation. The loop calls no predictor and no LLM.

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

Only eval/ may use data_gen/world.py (D-1).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from adapters import CsvAdapter, default_adapter
from agents.detection import DetectionAgent
from agents.run import DEFAULT_MATNR, notices_on_file
from agents.supplier_evaluation import SupplierEvaluationAgent
from integrations.sap.rpt_client import RptClient
from scoring import supplier_score

DATA_GEN = Path(__file__).resolve().parent.parent / "data_gen"

GESIT = "GESIT"
ALWAYS_CHEAPEST = "ALWAYS_CHEAPEST"
ALWAYS_FASTEST = "ALWAYS_FASTEST"
KEEP_CURRENT_PO = "KEEP_CURRENT_PO"
STRATEGIES = (GESIT, ALWAYS_CHEAPEST, ALWAYS_FASTEST, KEEP_CURRENT_PO)
BASELINES = STRATEGIES[1:]
SCENARIOS = ("S1", "S2", "S3")
Z_95 = 1.96   # normal approximation for the 95% interval of a mean paired difference

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


def load_world() -> DeliveryWorld:
    """The simulated world's true delivery behaviour (data_gen/world.py; prices not needed)."""
    if str(DATA_GEN) not in sys.path:
        sys.path.insert(0, str(DATA_GEN))
    from world import World   # noqa: PLC0415  (only eval/ may import it, D-1)
    return World(anchors={})


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


def choices_for(table: dict[str, Any]) -> dict[str, Choice]:
    """The four strategies' rows from one ranked table (FR-SEL-2, FR-SEL-3 marks)."""
    rows = table["CANDIDATES"]
    picked = {
        GESIT: rows[0] if rows else None,
        ALWAYS_CHEAPEST: next((r for r in rows if r["IS_CHEAPEST"]), None),
        ALWAYS_FASTEST: next((r for r in rows if r["IS_FASTEST"]), None),
        KEEP_CURRENT_PO: table.get("KEEP"),
    }
    missing = [name for name, row in picked.items() if row is None]
    if missing:
        raise ValueError(f"no row for {', '.join(missing)} in the ranked table; the strategy "
                         "is not defined for this case")
    return {name: Choice(name, label_of(row), parts_of(row), int(row["PURCHASE_COST"]),
                         int(row["EXPECTED_COST"]))
            for name, row in picked.items()}


def scenario_of(ebeln: str, scenarios: list[dict[str, Any]]) -> str:
    return next(s["id"].upper() for s in scenarios if s["ebeln"] == ebeln)


def build_cases(adapter: CsvAdapter | None = None,
                rpt_client: RptClient | None = None) -> tuple[list[Case], dict[str, Any]]:
    """Detection and Supplier Evaluation once, as `agents.run all --no-llm` runs them."""
    adapter = adapter or default_adapter()
    rpt_client = rpt_client or RptClient()
    detection = DetectionAgent(adapter=adapter, rpt_client=rpt_client).detect(
        DEFAULT_MATNR, notices_on_file("all", adapter.scenarios()))
    evaluation = SupplierEvaluationAgent(adapter=adapter, rpt_client=rpt_client).evaluate(
        detection, detection["FLAGGED_POS"])
    cases = []
    for case in evaluation["CASES"]:
        table = case["SCORING"]
        pred = table["PREDICTION"]
        cases.append(Case(
            scenario=scenario_of(case["EBELN"], adapter.scenarios()), ebeln=case["EBELN"],
            qty=float(case["QTY"]), stockout=case["NEED_BY"],
            downtime_cost_per_day=int(table["DOWNTIME_COST_PER_DAY"]),
            choices=choices_for(table),
            prediction_model=pred["MODEL_ID"] + (" (fallback)" if pred["FALLBACK"] else "")))
    meta = {"DETECTION_MODEL": detection["PREDICTION"]["MODEL_ID"]
            + (" (fallback)" if detection["PREDICTION"]["FALLBACK"] else ""),
            "EXCLUDE_FLAGGED_SUPPLIERS": evaluation["EXCLUDE_FLAGGED_SUPPLIERS"],
            "TODAY": detection["TODAY"]}
    return cases, meta


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
                        for s in STRATEGIES} for c in cases}
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


def summarise(series: dict[str, dict[str, np.ndarray]]) -> dict[str, Any]:
    """Statistics for one scenario: per strategy, and GESIT against each baseline."""
    per = {}
    for name in STRATEGIES:
        cost, days = series[name]["COST"], series[name]["OUT_DAYS"]
        per[name] = {"MEAN_COST": float(cost.mean()), "P90_COST": float(np.percentile(cost, 90)),
                     "MEAN_OUT_DAYS": float(days.mean()), "P_ANY_OUT": float((days > 0).mean())}
    versus = {}
    for name in BASELINES:
        diff = series[GESIT]["COST"] - series[name]["COST"]   # negative: GESIT costs less
        half = Z_95 * float(diff.std(ddof=1)) / math.sqrt(len(diff)) if len(diff) > 1 else 0.0
        versus[name] = {"MEAN_DIFF": float(diff.mean()),
                        "CI95": [float(diff.mean()) - half, float(diff.mean()) + half],
                        "SHARE_GESIT_CHEAPER": float((diff < 0).mean()),
                        "SHARE_SAME": float((diff == 0).mean())}
    return {"STRATEGIES": per, "GESIT_VS": versus}


def run(scenario: str = "all", runs: int = 1000, seed: int = 42,
        adapter: CsvAdapter | None = None, rpt_client: RptClient | None = None,
        world: DeliveryWorld | None = None) -> dict[str, Any]:
    """Build the cases once, simulate, and summarise (the numbers of the printed tables)."""
    cases, meta = build_cases(adapter, rpt_client)
    wanted = SCENARIOS if scenario.upper() == "ALL" else (scenario.upper(),)
    cases = sorted((c for c in cases if c.scenario in wanted), key=lambda c: c.scenario)
    started = time.perf_counter()
    results = simulate(cases, world or load_world(), runs, seed)
    if len(cases) > 1:
        results = with_total(results)
    return {
        "DATA_NOTE": "simulated data", "SEED": seed, "RUNS": runs, **meta,
        "LOOP_SECONDS": round(time.perf_counter() - started, 2),
        "STOCKOUT_DEFINITION": STOCKOUT_DEFINITION,
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


def format_report(report: dict[str, Any]) -> str:
    lines = [f"GESIT Monte Carlo against baselines ({report['DATA_NOTE']}), FR-EVA-4"]
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
        lines.append(f"{'GESIT vs':<17}{'MEAN DIFF':>16}{'95% INTERVAL':>40}{'GESIT CHEAPER':>15}"
                     f"{'SAME':>8}")
        for s in BASELINES:
            v = res["GESIT_VS"][s]
            interval = f"[{_signed_rp(v['CI95'][0])}, {_signed_rp(v['CI95'][1])}]"
            lines.append(f"{s:<17}{_signed_rp(v['MEAN_DIFF']):>16}{interval:>40}"
                         f"{v['SHARE_GESIT_CHEAPER']:>15.1%}{v['SHARE_SAME']:>8.1%}")
    downtime = {c["DOWNTIME_COST_PER_DAY"] for c in report["CASES"].values()}
    lines += [
        "",
        f"All figures are simulated data. Seed {report['SEED']}, {report['RUNS']:,} runs per "
        f"scenario and strategy, loop {report['LOOP_SECONDS']} s.",
        f"Predictions (made once per case, before the loop): Detection {report['DETECTION_MODEL']}; "
        + "; ".join(f"{n} {c['PREDICTION_MODEL']}" for n, c in report["CASES"].items())
        + ". No SAP-RPT or LLM call inside the loop.",
        f"Outcomes: true delivery behaviour of the simulated world (data_gen/world.py), one draw "
        f"per supplier and promised date per run, shared by all strategies.",
        f"Stock-out: {STOCKOUT_DEFINITION}. Short shipments are not counted.",
        "Cost = purchase cost (kg x NETPR; the revised price when keeping the PO) + stock-out "
        f"days x {', '.join(_rp(d) for d in sorted(downtime))} per day of downtime.",
        "PREDICTED = GESIT's expected cost for that row (FR-SEL-2). MEAN DIFF = GESIT minus the "
        "baseline per run; negative means GESIT costs less. SAME = runs with equal cost.",
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
