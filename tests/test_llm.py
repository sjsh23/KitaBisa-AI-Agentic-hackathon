"""Model chain and quota fallback in agents/llm.py (NFR-1, NFR-2, NFR-6)."""
import pytest

from agents import llm

PER_DAY = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
PER_MINUTE = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"


class ClientError(Exception):
    """Shape of google.genai's ClientError for a 429."""

    def __init__(self, quota_id, retry):
        super().__init__("429 RESOURCE_EXHAUSTED. You exceeded your current quota")
        self.code = 429
        self.details = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{"quotaId": quota_id, "quotaValue": "20"}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry},
        ]}}


class ModelThrottledException(Exception):
    """Same class name as the Strands wrapper."""


def throttled(quota_id=PER_DAY, retry="48700s"):
    try:
        raise ClientError(quota_id, retry)
    except ClientError as cause:
        try:
            raise ModelThrottledException("You exceeded your current quota") from cause
        except ModelThrottledException as exc:
            return exc


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    llm.reset_quota_memory()
    monkeypatch.setenv("GESIT_LLM_MODEL", "")
    monkeypatch.setenv("GESIT_LLM_FALLBACK_MODEL", "")
    monkeypatch.setattr(llm, "build_model",
                        lambda env=None, model=None, max_output_tokens=0: (model, max_output_tokens))
    yield
    llm.reset_quota_memory()


def test_model_chain():
    assert llm.model_chain({}) == ["gemini-3.5-flash", "gemini-2.5-flash"]
    assert llm.model_chain({"GESIT_LLM_FALLBACK_MODEL": "none"}) == ["gemini-3.5-flash"]
    assert llm.model_chain({"GESIT_LLM_FALLBACK_MODEL": "a, b,a"}) == ["gemini-3.5-flash", "a", "b"]
    assert llm.model_chain({"GESIT_LLM_MODEL": "gemini-2.5-flash"}) == ["gemini-2.5-flash"]


def test_quota_info_reads_the_wrapped_gemini_error():
    info = llm.quota_info(throttled())
    assert info == {"QUOTA_IDS": [PER_DAY], "RETRY_SECONDS": 48700.0}
    assert llm.is_used_up(info) is True
    assert llm.is_used_up(llm.quota_info(throttled(PER_MINUTE, "12s"))) is False
    assert llm.is_used_up(llm.quota_info(throttled(PER_MINUTE, "300s"))) is True
    assert llm.quota_info(ValueError("other")) is None
    assert llm.quota_note("gemini-3.5-flash", info) == (
        f"gemini-3.5-flash out of quota ({PER_DAY}, retry in 13.5 h)")


def test_daily_quota_switches_to_the_fallback_and_is_remembered(monkeypatch):
    calls = []

    def fake_ask(model, system_prompt, prompt):
        calls.append(model)
        if model[0] == "gemini-3.5-flash":
            raise throttled()
        return "ok"

    monkeypatch.setattr(llm, "_ask", fake_ask)
    assert llm.complete("s", "p") == ("ok", "gemini-2.5-flash")
    assert calls == [("gemini-3.5-flash", llm.MAX_OUTPUT_TOKENS),
                     ("gemini-2.5-flash", llm.MAX_OUTPUT_TOKENS)]
    # The second call in the same run skips the used-up model at once.
    assert llm.complete("s", "p") == ("ok", "gemini-2.5-flash")
    assert calls[2:] == [("gemini-2.5-flash", llm.MAX_OUTPUT_TOKENS)]
    assert list(llm.used_up_models()) == ["gemini-3.5-flash"]


def test_every_model_used_up_fails_fast(monkeypatch):
    calls = []

    def fake_ask(model, system_prompt, prompt):
        calls.append(model)
        raise throttled()

    monkeypatch.setattr(llm, "_ask", fake_ask)
    with pytest.raises(llm.LlmQuotaExceeded) as err:
        llm.complete("s", "p")
    assert len(calls) == 2                       # one try per model, no long retry loop
    assert "gemini-3.5-flash out of quota" in str(err.value)
    assert "gemini-2.5-flash out of quota" in str(err.value)
    with pytest.raises(llm.LlmQuotaExceeded):    # later calls do not even try
        llm.complete("s", "p")
    assert len(calls) == 2
    assert llm.error_detail(err.value).startswith("Gemini quota used up on every model")


class FakeAgent:
    """Stand-in for strands.Agent: fails with the queued errors, then answers."""
    errors: list = []
    kwargs: dict = {}

    def __init__(self, **kwargs):
        FakeAgent.kwargs = kwargs

    def __call__(self, prompt):
        if FakeAgent.errors:
            raise FakeAgent.errors.pop(0)
        return "answer"


def test_ask_waits_out_a_short_rate_limit_but_not_a_daily_quota(monkeypatch):
    import strands
    monkeypatch.setattr(strands, "Agent", FakeAgent)
    waits = []
    monkeypatch.setattr(llm.time, "sleep", waits.append)

    FakeAgent.errors = [throttled(PER_MINUTE, "12s")]
    assert llm._ask("model", "s", "p") == "answer" and waits == [12.0]
    assert FakeAgent.kwargs["retry_strategy"].__class__.__name__ == "ModelRetryStrategy"

    FakeAgent.errors = [throttled()]
    with pytest.raises(ModelThrottledException):
        llm._ask("model", "s", "p")
    assert waits == [12.0]                       # no wait for a used-up daily quota


class ServerError(Exception):
    """Shape of google.genai's ServerError for a 503."""

    def __init__(self):
        super().__init__("503 UNAVAILABLE. The model is overloaded. Please try again later.")
        self.code = 503
        self.status = "UNAVAILABLE"


def test_ask_retries_an_overload_with_longer_waits(monkeypatch):
    import strands
    monkeypatch.setattr(strands, "Agent", FakeAgent)
    monkeypatch.setattr(llm.random, "uniform", lambda low, high: 0.0)
    waits = []
    monkeypatch.setattr(llm.time, "sleep", waits.append)

    FakeAgent.errors = [ServerError(), ServerError(), ServerError()]
    assert llm._ask("model", "s", "p") == "answer"
    assert waits == [2.0, 5.0, 10.0]

    waits.clear()
    FakeAgent.errors = [ServerError() for _ in range(llm.LLM_ATTEMPTS)]
    with pytest.raises(ServerError):
        llm._ask("model", "s", "p")
    assert waits == [2.0, 5.0, 10.0]             # four tries, about 17 s, then give up


def test_retry_wait_jitter_stays_within_a_quarter(monkeypatch):
    monkeypatch.setattr(llm.random, "uniform", lambda low, high: high)
    assert llm.retry_wait(1) == pytest.approx(2.5)
    assert llm.retry_wait(3) == pytest.approx(12.5)
    assert llm.retry_wait(9) == pytest.approx(12.5)   # past the table: the longest wait


def test_overload_falls_back_for_this_call_only(monkeypatch):
    calls = []

    def fake_ask(model, system_prompt, prompt):
        calls.append(model)
        if model[0] == "gemini-3.5-flash" and len(calls) == 1:
            raise ServerError()
        return "ok"

    monkeypatch.setattr(llm, "_ask", fake_ask)
    assert llm.complete("s", "p") == ("ok", "gemini-2.5-flash")
    # The larger token budget of the overloaded model is not tried; the fallback answers.
    assert calls == [("gemini-3.5-flash", llm.MAX_OUTPUT_TOKENS),
                     ("gemini-2.5-flash", llm.MAX_OUTPUT_TOKENS)]
    # The next call tries the main model again: an overload is not a used-up quota.
    assert llm.complete("s", "p") == ("ok", "gemini-3.5-flash")
    assert llm.used_up_models() == {}
    assert llm.overloaded_models() == {"gemini-3.5-flash": 1}
    assert llm.overload_note("gemini-3.5-flash", 1) == "gemini-3.5-flash overloaded (HTTP 503) on 1 call"


def test_every_model_overloaded_raises_the_server_error(monkeypatch):
    calls = []

    def fake_ask(model, system_prompt, prompt):
        calls.append(model)
        raise ServerError()

    monkeypatch.setattr(llm, "_ask", fake_ask)
    with pytest.raises(ServerError) as err:
        llm.complete("s", "p")
    assert [model for model, _ in calls] == ["gemini-3.5-flash", "gemini-2.5-flash"]
    assert llm.error_detail(err.value) == "ServerError 503 UNAVAILABLE"
    assert llm.overloaded_models() == {"gemini-3.5-flash": 1, "gemini-2.5-flash": 1}


def test_overload_then_quota_reports_the_overload(monkeypatch):
    def fake_ask(model, system_prompt, prompt):
        if model[0] == "gemini-3.5-flash":
            raise ServerError()
        raise throttled()

    monkeypatch.setattr(llm, "_ask", fake_ask)
    with pytest.raises(ServerError):
        llm.complete("s", "p")
    assert list(llm.used_up_models()) == ["gemini-2.5-flash"]
