"""Model chain, rate-limit and overload fallback in agents/llm.py (NFR-1, NFR-2, NFR-6)."""
import pytest

from agents import llm
from integrations.sap import aicore

MAIN = "gpt-5.4"
FALLBACK = "anthropic--claude-4.6-sonnet"
REAL_BUILD_MODEL = llm.build_model   # before the fixture replaces it


class RateLimitError(Exception):
    """Shape of the OpenAI SDK's RateLimitError (HTTP 429)."""
    status_code = 429


class InternalServerError(Exception):
    """Shape of the OpenAI SDK's InternalServerError for a 503."""
    status_code = 503


class ClientError(Exception):
    """Shape of botocore's ClientError, as raised for the Claude deployment."""

    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.response = {"Error": {"Code": str(status)}, "ResponseMetadata": {"HTTPStatusCode": status}}


class ModelThrottledException(Exception):
    """Same class name as the Strands wrapper."""


class APIConnectionError(Exception):
    """Same class name as the OpenAI SDK's connection error (no HTTP status)."""


def throttled():
    try:
        raise RateLimitError("Too Many Requests")
    except RateLimitError as cause:
        try:
            raise ModelThrottledException("Too Many Requests") from cause
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
    assert llm.model_chain({}) == [MAIN, FALLBACK]
    assert llm.model_chain({"GESIT_LLM_FALLBACK_MODEL": "none"}) == [MAIN]
    assert llm.model_chain({"GESIT_LLM_FALLBACK_MODEL": "a, b,a"}) == [MAIN, "a", "b"]
    assert llm.model_chain({"GESIT_LLM_MODEL": FALLBACK}) == [FALLBACK]


def test_provider_parameters():
    params = llm.model_params({}, 4096)
    assert llm.openai_params(params) == {"max_completion_tokens": 4096, "stream": False,
                                         "temperature": 0.2}
    assert llm.bedrock_params(params) == {"max_tokens": 4096, "streaming": False, "temperature": 0.2}
    assert "temperature" not in llm.openai_params(llm.model_params({"GESIT_LLM_TEMPERATURE": ""}))
    assert llm.is_anthropic(FALLBACK) and not llm.is_anthropic(MAIN)


def test_http_status_reads_openai_botocore_and_wrapped_errors():
    assert llm.http_status(throttled()) == 429
    assert llm.http_status(ClientError(503)) == 503
    assert llm.http_status(ValueError("other")) is None
    assert llm.error_detail(InternalServerError("overloaded")) == "InternalServerError HTTP 503"
    assert llm.error_detail(APIConnectionError("reset")) == "APIConnectionError"


def test_build_model_needs_ai_core_credentials():
    with pytest.raises(llm.LlmUnavailable, match="AICORE"):
        REAL_BUILD_MODEL({})


def test_rate_limit_switches_to_the_fallback_and_is_remembered(monkeypatch):
    calls = []

    def fake_ask(model, system_prompt, prompt):
        calls.append(model)
        if model[0] == MAIN:
            raise throttled()
        return "ok"

    monkeypatch.setattr(llm, "_ask", fake_ask)
    assert llm.complete("s", "p") == ("ok", FALLBACK)
    assert calls == [(MAIN, llm.MAX_OUTPUT_TOKENS), (FALLBACK, llm.MAX_OUTPUT_TOKENS)]
    # The second call in the same run skips the rate-limited model at once.
    assert llm.complete("s", "p") == ("ok", FALLBACK)
    assert calls[2:] == [(FALLBACK, llm.MAX_OUTPUT_TOKENS)]
    assert list(llm.used_up_models()) == [MAIN]
    assert llm.run_notes() == [f"{MAIN} rate limited (HTTP 429) after retries, "
                               "skipped for the rest of the run"]


def test_every_model_rate_limited_fails_fast(monkeypatch):
    calls = []

    def fake_ask(model, system_prompt, prompt):
        calls.append(model)
        raise throttled()

    monkeypatch.setattr(llm, "_ask", fake_ask)
    with pytest.raises(llm.LlmQuotaExceeded) as err:
        llm.complete("s", "p")
    assert len(calls) == 2                       # one try per model, no long retry loop
    assert f"{MAIN}, {FALLBACK}" in str(err.value)
    with pytest.raises(llm.LlmQuotaExceeded):    # later calls do not even try
        llm.complete("s", "p")
    assert len(calls) == 2
    assert llm.error_detail(err.value).startswith("every Gen AI Hub model is rate limited")


def test_missing_deployment_moves_to_the_fallback(monkeypatch):
    def fake_build(env=None, model=None, max_output_tokens=0):
        if model == MAIN:
            raise llm.LlmUnavailable(f"no RUNNING SAP AI Core deployment for {MAIN}")
        return (model, max_output_tokens)

    monkeypatch.setattr(llm, "build_model", fake_build)
    monkeypatch.setattr(llm, "_ask", lambda model, s, p: "ok")
    assert llm.complete("s", "p") == ("ok", FALLBACK)
    assert llm.run_notes() == [f"{MAIN} unavailable (no RUNNING SAP AI Core deployment for {MAIN})"]


def test_no_credentials_raises_llm_unavailable(monkeypatch):
    def fake_build(env=None, model=None, max_output_tokens=0):
        raise llm.LlmUnavailable("SAP AI Core credentials (AICORE_*) are not set")

    monkeypatch.setattr(llm, "build_model", fake_build)
    with pytest.raises(llm.LlmUnavailable, match="AICORE"):
        llm.complete("s", "p")


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


def test_ask_retries_rate_limits_overloads_and_lost_connections(monkeypatch):
    import strands
    monkeypatch.setattr(strands, "Agent", FakeAgent)
    monkeypatch.setattr(llm.random, "uniform", lambda low, high: 0.0)
    waits = []
    monkeypatch.setattr(llm.time, "sleep", waits.append)

    FakeAgent.errors = [throttled(), ClientError(503), APIConnectionError()]
    assert llm._ask("model", "s", "p") == "answer"
    assert waits == [2.0, 5.0, 10.0]
    assert FakeAgent.kwargs["retry_strategy"].__class__.__name__ == "ModelRetryStrategy"

    waits.clear()
    FakeAgent.errors = [InternalServerError() for _ in range(llm.LLM_ATTEMPTS)]
    with pytest.raises(InternalServerError):
        llm._ask("model", "s", "p")
    assert waits == [2.0, 5.0, 10.0]             # four tries, about 17 s, then give up

    waits.clear()
    FakeAgent.errors = [ClientError(400)]        # a bad request is not retried
    with pytest.raises(ClientError):
        llm._ask("model", "s", "p")
    assert waits == []


def test_retry_wait_jitter_stays_within_a_quarter(monkeypatch):
    monkeypatch.setattr(llm.random, "uniform", lambda low, high: high)
    assert llm.retry_wait(1) == pytest.approx(2.5)
    assert llm.retry_wait(3) == pytest.approx(12.5)
    assert llm.retry_wait(9) == pytest.approx(12.5)   # past the table: the longest wait


def test_overload_falls_back_for_this_call_only(monkeypatch):
    calls = []

    def fake_ask(model, system_prompt, prompt):
        calls.append(model)
        if model[0] == MAIN and len(calls) == 1:
            raise InternalServerError()
        return "ok"

    monkeypatch.setattr(llm, "_ask", fake_ask)
    assert llm.complete("s", "p") == ("ok", FALLBACK)
    # The larger token budget of the overloaded model is not tried; the fallback answers.
    assert calls == [(MAIN, llm.MAX_OUTPUT_TOKENS), (FALLBACK, llm.MAX_OUTPUT_TOKENS)]
    # The next call tries the main model again: an overload is not a rate limit.
    assert llm.complete("s", "p") == ("ok", MAIN)
    assert llm.used_up_models() == {}
    assert llm.overloaded_models() == {MAIN: (1, 503)}
    assert llm.run_notes() == [f"{MAIN} overloaded (HTTP 503) on 1 call"]
    assert llm.overload_note(MAIN, 2) == f"{MAIN} overloaded (no connection) on 2 calls"


def test_every_model_overloaded_raises_the_server_error(monkeypatch):
    calls = []

    def fake_ask(model, system_prompt, prompt):
        calls.append(model)
        raise InternalServerError()

    monkeypatch.setattr(llm, "_ask", fake_ask)
    with pytest.raises(InternalServerError) as err:
        llm.complete("s", "p")
    assert [model for model, _ in calls] == [MAIN, FALLBACK]
    assert llm.error_detail(err.value) == "InternalServerError HTTP 503"
    assert llm.overloaded_models() == {MAIN: (1, 503), FALLBACK: (1, 503)}


def test_overload_then_rate_limit_reports_the_overload(monkeypatch):
    def fake_ask(model, system_prompt, prompt):
        if model[0] == MAIN:
            raise InternalServerError()
        raise throttled()

    monkeypatch.setattr(llm, "_ask", fake_ask)
    with pytest.raises(InternalServerError):
        llm.complete("s", "p")
    assert list(llm.used_up_models()) == [FALLBACK]


def test_aicore_session_is_shared_per_service_key():
    env = {"AICORE_AUTH_URL": "https://auth", "AICORE_CLIENT_ID": "id",
           "AICORE_CLIENT_SECRET": "secret", "AICORE_BASE_URL": "https://api"}
    first = llm.aicore_session(env)
    assert llm.aicore_session(dict(env)) is first
    assert isinstance(first, aicore.AiCoreSession) and first.config.resource_group == "default"
