"""Monte Carlo robustness options (FR-EVA-4, D-1, D-3, simulated data): many worlds, downtime
cost sweep, baselines from the same table, compact and JSON output. No network: SAP-RPT is
the recorded stub of test_monte_carlo.py or a counting stub in the generated worlds."""
import json
import re
from pathlib import Path

import numpy as np
import pytest

from adapters import CsvAdapter
from agents import llm
from eval import monte_carlo as mc
from eval import worlds as world_gen
from integrations.sap.rpt_client import RptUnavailable
from test_monte_carlo import OnTimeWorld, RecordedRpt

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = Path(__file__).resolve().parent / "monte_carlo_default_before.txt"
FAILING_WORLD = "seed 2"


class DefaultingRpt(RecordedRpt):
    """The recorded values, and 0.2 for a row the recording does not have (sweep part b)."""

    def predict_late(self, context_rows, query_rows):
        out = super().predict_late(context_rows, query_rows)
        return {k: (0.2 if v is None else v) for k, v in out.items()}


class CountingRpt:
    """Stub for a generated world: P(late) from the LIFNR, or SAP-RPT down when `fail`."""
    model_id = "sap-rpt-1.6"

    def __init__(self, fail: bool = False):
        self.calls, self.fail = 0, fail

    def predict_late(self, context_rows, query_rows):
        self.calls += 1
        if self.fail:
            raise RptUnavailable("stub: SAP-RPT timed out")
        return {r["EBELN"]: 0.1 + (int(r["LIFNR"]) % 7) * 0.04 for r in query_rows}


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("the Monte Carlo must not call an LLM")
    monkeypatch.setattr(llm, "complete", fail)


@pytest.fixture(scope="module")
def adapter():
    return CsvAdapter()


def normalise(text: str) -> list[str]:
    """Old and new default output compared without the renamed headers, the loop time and
    the footer lines the change added or reworded."""
    out = []
    for line in text.splitlines():
        if line.startswith(("PREDICTED = ", "Limitations: ")):
            continue
        line = line.replace(mc.CHEAPER_HEADER, "CHEAPER").replace(mc.SAME_HEADER, "SAME")
        line = re.sub(r"loop [0-9.]+ s", "loop s", line)
        out.append(" ".join(line.split()))
    return out


def test_default_output_unchanged_except_the_renamed_headers(adapter):
    out = mc.evaluate("all", 1000, 42, adapter=adapter, rpt_client=RecordedRpt())
    text = mc.format_all(out)
    main, _, learning = text.partition("\nBaselines from the LEARNING ranked table")
    assert normalise(main) == normalise(GOLDEN.read_text(encoding="utf-8"))
    assert learning   # default --baseline-table both: the LEARNING block comes after the footer
    for header in (mc.CHEAPER_HEADER, mc.SAME_HEADER):
        assert header in main
    assert "Limitations: the stock-out date is fixed" in main
    assert "short shipments are not counted" in main and "one decision point (2026-09-30)" in main
    assert "differ only by generator seed" in main and "all data is simulated" in main
    assert chr(0x2014) not in text and chr(0x2013) not in text   # em and en dash


@pytest.fixture(scope="module")
def many_worlds(adapter, tmp_path_factory):
    """Tracked dataset plus two generated worlds, 50 runs, stub predictors; the second
    generated world's SAP-RPT is down (fallback)."""
    before = world_gen.folder_hashes()
    made: list[Path] = []
    original = world_gen.tempfile.mkdtemp

    def mkdtemp(*args, **kwargs):
        made.append(Path(original(*args, **kwargs)))
        return str(made[-1])

    stubs: dict[str, CountingRpt] = {}

    def rpt_for(label):
        stubs[label] = CountingRpt(fail=label == FAILING_WORLD)
        return stubs[label]

    mp = pytest.MonkeyPatch()
    mp.setattr(world_gen.tempfile, "mkdtemp", mkdtemp)
    try:
        tracked = RecordedRpt()
        out = mc.evaluate("all", 50, 42, world_seeds=(1, 2), adapter=adapter,
                          rpt_client=tracked, rpt_for=rpt_for)
    finally:
        mp.undo()
    return {"out": out, "stubs": stubs, "tracked": tracked, "made": made,
            "before": before, "after": world_gen.folder_hashes()}


def test_many_worlds_predictor_calls_and_cleanup(many_worlds):
    out, stubs = many_worlds["out"], many_worlds["stubs"]
    per_build = len(mc.MODES) * (1 + len(mc.SCENARIOS))   # Detection + one table per case
    assert many_worlds["tracked"].calls == per_build
    assert stubs["seed 1"].calls == per_build
    assert stubs[FAILING_WORLD].calls == 2 * per_build    # retried once
    assert [w["WORLD"] for w in out["WORLDS"]] == ["tracked (seed 42)", "seed 1", FAILING_WORLD]
    assert len(many_worlds["made"]) == 2
    assert not any(p.exists() for p in many_worlds["made"])   # temporary folders deleted
    assert many_worlds["before"] == many_worlds["after"]      # tracked data untouched
    assert many_worlds["before"]                              # and the hashes were taken


def test_fallback_world_is_excluded_from_the_cross_world_summary(many_worlds):
    out = many_worlds["out"]
    status = {w["WORLD"]: w["STATUS"] for w in out["WORLDS"]}
    assert status == {"tracked (seed 42)": "ok", "seed 1": "ok", FAILING_WORLD: "fallback"}
    cross = out["CROSS_WORLD"]
    assert cross["WORLDS_USED"] == ["tracked (seed 42)", "seed 1"]
    assert [e["WORLD"] for e in cross["WORLDS_EXCLUDED"]] == [FAILING_WORLD]
    used = [w for w in out["WORLDS"] if w["STATUS"] == "ok"]
    means = [w["RESULTS"]["TOTAL"]["STRATEGIES"][mc.GESIT_LEARNING]["MEAN_COST"] for w in used]
    assert cross["MEAN_TOTAL_COST"][mc.GESIT_LEARNING] == pytest.approx(np.mean(means))
    st = cross["VERSUS"][mc.GESIT_LEARNING][mc.ALWAYS_FASTEST]
    diffs = [w["RESULTS"]["TOTAL"]["VERSUS"][mc.GESIT_LEARNING][mc.ALWAYS_FASTEST]["MEAN_DIFF"]
             for w in used]
    assert st["OF"] == 2 and st["CHEAPER_IN"] == sum(d < 0 for d in diffs)
    assert (st["MIN_DIFF"], st["MAX_DIFF"]) == (min(diffs), max(diffs))
    # The tracked world in the many-worlds run has the main results' numbers.
    assert out["WORLDS"][0]["RESULTS"] == out["RESULTS"]
    text = mc.format_all(out)
    assert "Cross-world summary over 2 of 3 worlds (1 excluded; seed 2 fallback" in text


def test_json_has_the_simulated_flag_and_the_cross_world_summary(many_worlds, tmp_path):
    path = tmp_path / "mc.json"
    mc.write_json(many_worlds["out"], str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["simulated"] is True
    assert data["SETTINGS"]["WORLDS_GENERATOR_SEEDS"] == [1, 2]
    assert data["CROSS_WORLD"]["WORLDS_USED"] == ["tracked (seed 42)", "seed 1"]
    assert {"RESULTS", "CALIBRATION", "WORLDS"} <= set(data)
    with pytest.raises(SystemExit):
        mc.write_json(many_worlds["out"], str(world_gen.TRACKED_OUT / "mc.json"))


def test_derived_world_seed_is_reproducible():
    assert mc.derived_seed(42, 7) == mc.derived_seed(42, 7)
    assert mc.derived_seed(42, 7) != mc.derived_seed(42, 8) != mc.derived_seed(43, 8)


@pytest.fixture(scope="module")
def main_run(adapter):
    return mc.run_detailed("all", 300, 42, adapter, RecordedRpt(), mc.load_world(), "rpt")


def test_sweep_fixed_at_the_dataset_value_equals_the_main_results(main_run):
    report, cases, results = main_run
    row = mc.sweep_fixed(cases, results, [150_000_000])[0]
    for s in mc.STRATEGIES:
        assert row["TOTAL_MEAN_COST"][s] == pytest.approx(
            report["RESULTS"]["TOTAL"]["STRATEGIES"][s]["MEAN_COST"], rel=1e-12)


def test_sweep_fixed_higher_value_raises_only_strategies_with_stock_out(main_run):
    _, cases, results = main_run
    low, high = mc.sweep_fixed(cases, results, [150_000_000, 300_000_000])
    for s in mc.STRATEGIES:
        out_days = float(sum(results[c.scenario][s]["OUT_DAYS"] for c in cases).mean())
        assert high["TOTAL_MEAN_COST"][s] - low["TOTAL_MEAN_COST"][s] == pytest.approx(
            150_000_000 * out_days)
    # A case where nothing is ever late: the downtime cost does not change its cost.
    part = mc.Part("100109", 5000.0, "2026-10-03")
    choices = {n: mc.Choice(n, "x", (part,), 90_000_000, 0) for n in mc.STRATEGIES}
    case = mc.Case("S1", "1", 5000.0, "2026-10-06", 150_000_000, choices, "stub")
    on_time = mc.simulate([case], OnTimeWorld(), runs=10, seed=1)
    rows = mc.sweep_fixed([case], on_time, [50_000_000, 300_000_000])
    assert rows[0]["TOTAL_MEAN_COST"] == rows[1]["TOTAL_MEAN_COST"]


def test_sweep_remade_at_the_dataset_value_equals_the_main_results(adapter):
    inner = DefaultingRpt()
    out = mc.evaluate("all", 200, 42, baseline_table="rpt", adapter=adapter, rpt_client=inner,
                      downtime_values=[150_000_000, 50_000_000])
    remade = {r["DOWNTIME_COST_PER_DAY"]: r for r in out["SENSITIVITY"]["DECISIONS_REMADE"]}
    at = remade[150_000_000]
    for s in mc.STRATEGIES:
        assert at["TOTAL_MEAN_COST"][s] == out["RESULTS"]["TOTAL"]["STRATEGIES"][s]["MEAN_COST"]
        for scenario, case in out["CASES"].items():
            assert at["CHOICES"][scenario][s] == case["CHOICES"][s]["LABEL"]
    # The 150,000,000 rebuild is answered from the recording: 8 calls, all replayed.
    assert out["SENSITIVITY"]["SAP_RPT_ANSWERS_REPLAYED"] >= len(mc.MODES) * (1 + 3)
    fixed = {r["DOWNTIME_COST_PER_DAY"]: r for r in out["SENSITIVITY"]["DECISIONS_FIXED"]}
    assert fixed[150_000_000]["TOTAL_MEAN_COST"] == pytest.approx(at["TOTAL_MEAN_COST"])
    text = mc.format_all(out)
    assert "part (a): decisions fixed" in text and "part (b): decisions re-made" in text


def test_downtime_adapter_changes_only_the_inventory_row(adapter):
    other = mc.DowntimeAdapter(adapter.data_dir, 50_000_000)
    base = adapter.inventory("RM-GULA-001")
    assert other.inventory("RM-GULA-001") == {**base, "DOWNTIME_COST_PER_DAY": 50_000_000}
    assert adapter.inventory("RM-GULA-001")["DOWNTIME_COST_PER_DAY"] == 150_000_000
    assert other.quotes("RM-GULA-001") == adapter.quotes("RM-GULA-001")


def test_baseline_table_rpt_reproduces_the_old_numbers(adapter):
    old = mc.run("all", 200, 42, adapter, RecordedRpt(), baseline_table="rpt")
    both = mc.run("all", 200, 42, adapter, RecordedRpt(), baseline_table="both")
    for name, res in old["RESULTS"].items():
        assert set(res["STRATEGIES"]) == set(mc.STRATEGIES)
        assert "VERSUS_LEARNING_TABLE" not in res
        for s in mc.STRATEGIES:
            assert res["STRATEGIES"][s] == both["RESULTS"][name]["STRATEGIES"][s]
        assert res["VERSUS"] == both["RESULTS"][name]["VERSUS"]
    assert "VERSUS_LEARNING_TABLE" in both["RESULTS"]["TOTAL"]


def test_baseline_table_same_differs_only_where_the_learning_table_differs(adapter):
    cases, _ = mc.build_cases(adapter, RecordedRpt())
    out = mc.simulate(cases, mc.load_world(), runs=200, seed=42)
    for c in cases:
        for b in mc.BASELINES:
            if c.same_table[b].label == c.choices[b].label:   # same order, same draws
                assert np.array_equal(out[c.scenario][b]["COST"],
                                      out[c.scenario][b + mc.SAME_SUFFIX]["COST"])
    # A LEARNING table whose cheapest row differs: only that strategy's numbers move.
    a, b = mc.Part("100109", 5000.0, "2026-10-03"), mc.Part("100108", 5000.0, "2026-10-04")
    choices = {n: mc.Choice(n, "a", (a,), 90_000_000, 0) for n in mc.STRATEGIES}
    same_table = {n: choices[n] for n in mc.BASELINES}
    same_table[mc.ALWAYS_CHEAPEST] = mc.Choice(mc.ALWAYS_CHEAPEST, "b", (b,), 80_000_000, 0)
    case = mc.Case("S1", "1", 5000.0, "2026-10-06", 150_000_000, choices, "stub",
                   same_table=same_table)
    sim = mc.simulate([case], mc.load_world(), runs=200, seed=42)["S1"]
    assert not np.array_equal(sim[mc.ALWAYS_CHEAPEST]["COST"],
                              sim[mc.ALWAYS_CHEAPEST + mc.SAME_SUFFIX]["COST"])
    for n in (mc.ALWAYS_FASTEST, mc.KEEP_CURRENT_PO):
        assert np.array_equal(sim[n]["COST"], sim[n + mc.SAME_SUFFIX]["COST"])


def test_summary_table(adapter):
    out = mc.evaluate("all", 100, 42, baseline_table="rpt", adapter=adapter,
                      rpt_client=RecordedRpt())
    text = mc.format_all(out, summary=True)
    assert "costs in Rp juta" in text and "MINUS GESIT_LEARNING [95% interval]" in text
    assert "Scenario S1" in text and "\nTOTAL " in text
    v = out["RESULTS"]["TOTAL"]["VS_GESIT_LEARNING"][mc.ALWAYS_FASTEST]
    assert f"{v['MEAN_DIFF'] / 1e6:+,.1f} [" in text
    assert "simulated" in text and chr(0x2014) not in text and chr(0x2013) not in text


def test_cli_lists():
    assert mc._world_seeds("predeclared") == world_gen.PREDECLARED_SEEDS == tuple(range(1, 11))
    assert mc._world_seeds("1,2, 3") == (1, 2, 3)
    assert mc._int_list("50000000,150000000") == [50_000_000, 150_000_000]


def test_no_code_outside_eval_imports_eval_or_reads_the_truth():
    # D-1: only eval/ (and the tests of eval/) may import eval or world.py, or read _truth.
    imports = re.compile(r"^\s*(from|import)\s+eval\b")
    hits = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in ("eval", "tests", ".venv") or "site-packages" in rel.parts:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if imports.search(line):
                hits.append(f"{rel}:{n}")
    assert hits == []
    # eval/worlds.py generates worlds; it never opens a path under _truth/ itself.
    source = (ROOT / "eval" / "worlds.py").read_text(encoding="utf-8")
    assert not re.search(r"""["']_truth""", source)
