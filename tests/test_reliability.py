"""Reliability posterior (FR-EVA-2, FR-EVA-3)."""
import pytest

from scoring.reliability import Reliability, posterior, update


def test_prior_is_beta_2_1():
    prior = Reliability()
    assert (prior.alpha, prior.beta, prior.n_receipts) == (2.0, 1.0, 0)
    assert prior.mean_on_time == pytest.approx(2 / 3)
    assert prior.p_late == pytest.approx(1 / 3)


def test_update_applies_forgetting_then_counts_the_receipt():
    on_time = update(Reliability(), late=False)
    assert on_time.alpha == pytest.approx(2 * 0.97 + 1)
    assert on_time.beta == pytest.approx(0.97)
    late = update(Reliability(), late=True)
    assert late.alpha == pytest.approx(2 * 0.97)
    assert late.beta == pytest.approx(0.97 + 1)
    assert late.n_receipts == 1


def test_recent_receipts_weigh_more():
    improving = posterior([1] * 10 + [0] * 10)
    deteriorating = posterior([0] * 10 + [1] * 10)
    assert deteriorating.p_late > improving.p_late


def test_interval_contains_mean_and_narrows_with_evidence():
    few, many = posterior([0, 0, 1]), posterior([0, 0, 1] * 20)
    low, high = many.interval()
    assert low < many.mean_on_time < high
    assert (high - low) < (few.interval()[1] - few.interval()[0])


def test_as_dict_is_json_ready():
    data = posterior([0, 1, 0]).as_dict()
    assert data["N_RECEIPTS"] == 3
    assert data["P_LATE"] == pytest.approx(1 - data["ON_TIME_MEAN"], abs=1e-4)
    assert 0 <= data["ON_TIME_P05"] < data["ON_TIME_P95"] <= 1
