"""The reasoning model for the agents: GPT-5.4 on SAP Gen AI Hub (SAP AI Core), with
Claude Sonnet 4.6 on the same Gen AI Hub as the fallback.

Both models are deployments of the `foundation-models` scenario in SAP AI Core and are
reached with the AI Core service key that SAP-RPT uses (integrations/sap/aicore.py):
- GPT models through the Strands OpenAI provider, on the deployment's
  `/chat/completions` endpoint (Azure OpenAI style, `api-version` query parameter).
- `anthropic--` models through the Strands Bedrock provider, on the deployment's
  `/converse` endpoint (Bedrock Converse format, AI Core bearer token instead of AWS signing).

Environment variables (NFR-4, never hard-code keys):
- `AICORE_AUTH_URL`, `AICORE_CLIENT_ID`, `AICORE_CLIENT_SECRET`, `AICORE_BASE_URL`,
  `AICORE_RESOURCE_GROUP` (default `default`); required.
- `GESIT_LLM_MODEL`       default `gpt-5.4`, the Gen AI Hub model name, fixed so the demo
  cannot change under us (NFR-2). The deployment's version is shown in `docs/testing.md`.
- `GESIT_LLM_TEMPERATURE` default `0.2` (NFR-2: at or below 0.2). Set it to an
  empty string to leave the temperature at the model default.
- `GESIT_LLM_FALLBACK_MODEL` default `anthropic--claude-4.6-sonnet`; a comma-separated list
  is allowed, `none` switches the fallback off. A fallback model is used only when the
  model before it is not deployed, runs out of output tokens twice, is rate limited, or
  is still overloaded (HTTP 5xx) after its retries.

Rate limits (NFR-1, NFR-6): an HTTP 429 is retried on the same model after
LLM_RETRY_WAITS_SECONDS. A model still rate limited after that is skipped for the rest
of the process and the next model in the chain answers. When every model is rate
limited the call fails at once with `LlmQuotaExceeded`; it never sits in a long retry loop.

Overload (NFR-1, NFR-6): an HTTP 5xx or a lost connection is retried the same way. If
the model is still unavailable, the next model in the chain answers this call only; the
next call tries the main model again, because an overload usually passes within a minute.
"""
from __future__ import annotations

import os
import random
import time
from typing import Any, Mapping

from integrations.sap import aicore

DEFAULT_MODEL_ID = "gpt-5.4"
DEFAULT_FALLBACK_MODELS = "anthropic--claude-4.6-sonnet"
NO_FALLBACK = "none"
DEFAULT_TEMPERATURE = "0.2"
MAX_TEMPERATURE = 0.2
ANTHROPIC_PREFIX = "anthropic--"        # Gen AI Hub names of the Claude models
OPENAI_API_VERSION = "2025-04-01-preview"
BEDROCK_REGION = "us-west-2"            # label for the Bedrock client only; AI Core serves the call
REQUEST_TIMEOUT_SECONDS = 120.0
# Far above the 120-word answer, in case the model spends tokens on reasoning first.
# It is a ceiling, not a cost: only the tokens actually produced are counted.
MAX_OUTPUT_TOKENS = 8192
RETRY_OUTPUT_TOKENS = 32768     # second try when the first one runs out of output tokens

# Waits before try 2, 3 and 4 on the same model after an overload (HTTP 5xx) or rate limit.
LLM_RETRY_WAITS_SECONDS = (2.0, 5.0, 10.0)
LLM_ATTEMPTS = len(LLM_RETRY_WAITS_SECONDS) + 1
LLM_RETRY_JITTER = 0.25         # each wait is stretched by up to this share, at random

# Exception class names (OpenAI SDK, botocore, requests) for a call that never got an answer.
_CONNECTION_ERRORS = {"APIConnectionError", "APITimeoutError", "EndpointConnectionError",
                      "ReadTimeoutError", "ConnectTimeoutError", "ConnectionClosedError"}


class LlmUnavailable(RuntimeError):
    """The reasoning model cannot be used (missing credentials, package or deployment)."""


class LlmQuotaExceeded(LlmUnavailable):
    """Every model in the chain is rate limited."""


# Models still rate limited after their retries in this process: model id -> number of calls.
_USED_UP: dict[str, int] = {}
# Models still overloaded after their retries: model id -> (number of calls, last HTTP status).
# Only reported in the run notes; an overloaded model is not skipped on later calls.
_OVERLOADED: dict[str, tuple[int, int | None]] = {}
# Models that could not be built (for example not deployed): model id -> reason.
_UNAVAILABLE: dict[str, str] = {}
_SESSIONS: dict[aicore.AiCoreConfig, aicore.AiCoreSession] = {}


def model_id(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return env.get("GESIT_LLM_MODEL") or DEFAULT_MODEL_ID


def fallback_model_ids(env: Mapping[str, str] | None = None) -> list[str]:
    """Fallback models in order; Claude Sonnet 4.6 unless the environment says otherwise."""
    env = os.environ if env is None else env
    raw = env.get("GESIT_LLM_FALLBACK_MODEL", "").strip() or DEFAULT_FALLBACK_MODELS
    if raw.lower() == NO_FALLBACK:
        return []
    return [m.strip() for m in raw.split(",") if m.strip()]


def model_chain(env: Mapping[str, str] | None = None) -> list[str]:
    """The main model, then each fallback model once."""
    chain: list[str] = []
    for model in [model_id(env), *fallback_model_ids(env)]:
        if model not in chain:
            chain.append(model)
    return chain


def model_plan(env: Mapping[str, str] | None = None) -> list[tuple[str, int]]:
    """(model id, output token budget) to try in order: each model with the normal budget,
    then with the larger one, before the next model in the chain."""
    return [(model, budget) for model in model_chain(env)
            for budget in (MAX_OUTPUT_TOKENS, RETRY_OUTPUT_TOKENS)]


def model_params(env: Mapping[str, str] | None = None,
                 max_output_tokens: int = MAX_OUTPUT_TOKENS) -> dict[str, Any]:
    """Generation parameters; the temperature is capped at 0.2 (NFR-2)."""
    env = os.environ if env is None else env
    params: dict[str, Any] = {"max_output_tokens": max_output_tokens}
    raw = env.get("GESIT_LLM_TEMPERATURE", DEFAULT_TEMPERATURE).strip()
    if raw:
        params["temperature"] = min(float(raw), MAX_TEMPERATURE)
    return params


def is_anthropic(model: str) -> bool:
    return model.startswith(ANTHROPIC_PREFIX)


def build_model(env: Mapping[str, str] | None = None, model: str | None = None,
                max_output_tokens: int = MAX_OUTPUT_TOKENS) -> Any:
    """A Strands model for `model` (default `GESIT_LLM_MODEL`) on SAP Gen AI Hub,
    or `LlmUnavailable`."""
    env = os.environ if env is None else env
    name = model or model_id(env)
    session = aicore_session(env)
    try:
        deployment = session.deployment(name)
        token = session.token()
    except aicore.AiCoreUnavailable as exc:
        raise LlmUnavailable(str(exc)) from exc
    params = model_params(env, max_output_tokens)
    if is_anthropic(name):
        return _bedrock_model(deployment, token, session.config.resource_group, params)
    return _openai_model(deployment, token, session.config.resource_group, params)


def aicore_session(env: Mapping[str, str] | None = None) -> aicore.AiCoreSession:
    """One AI Core session per service key, so the token and deployment lookups are reused."""
    config = aicore.config_from_env(env)
    if config is None:
        raise LlmUnavailable("SAP AI Core credentials (AICORE_*) are not set")
    if config not in _SESSIONS:
        _SESSIONS[config] = aicore.AiCoreSession(config)
    return _SESSIONS[config]


def openai_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Chat completions parameters: GPT-5 models take `max_completion_tokens`."""
    out: dict[str, Any] = {"max_completion_tokens": params["max_output_tokens"], "stream": False}
    if "temperature" in params:
        out["temperature"] = params["temperature"]
    return out


def bedrock_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Bedrock Converse parameters (inferenceConfig)."""
    out: dict[str, Any] = {"max_tokens": params["max_output_tokens"], "streaming": False}
    if "temperature" in params:
        out["temperature"] = params["temperature"]
    return out


def _openai_model(deployment: aicore.Deployment, token: str, resource_group: str,
                  params: Mapping[str, Any]) -> Any:
    try:
        from strands.models.openai import OpenAIModel
    except ImportError as exc:
        raise LlmUnavailable("strands-agents[openai] is not installed") from exc
    client_args = {"api_key": token, "base_url": deployment.url,
                   "default_headers": {"AI-Resource-Group": resource_group},
                   "default_query": {"api-version": OPENAI_API_VERSION},
                   "max_retries": 0, "timeout": REQUEST_TIMEOUT_SECONDS}
    return OpenAIModel(client_args=client_args, model_id=deployment.model,
                       params=openai_params(params))


def _bedrock_model(deployment: aicore.Deployment, token: str, resource_group: str,
                   params: Mapping[str, Any]) -> Any:
    try:
        from botocore.config import Config
        from strands.models.bedrock import BedrockModel
    except ImportError as exc:
        raise LlmUnavailable("strands-agents (Bedrock provider) is not installed") from exc
    model = BedrockModel(model_id=deployment.model, region_name=BEDROCK_REGION,
                         endpoint_url=deployment.url, api_key=token,
                         boto_client_config=Config(retries={"max_attempts": 1},
                                                   read_timeout=REQUEST_TIMEOUT_SECONDS),
                         **bedrock_params(params))

    def to_aicore(request: Any, **_: Any) -> None:
        # Bedrock's path is /model/<id>/converse; AI Core's is <deployment url>/converse.
        request.url = deployment.url + "/converse"
        request.headers["AI-Resource-Group"] = resource_group

    model.client.meta.events.register("before-send.bedrock-runtime.Converse", to_aicore)
    return model


# ------------------------------------------------------------------ run notes
def used_up_models() -> dict[str, int]:
    """Models still rate limited after their retries in this process."""
    return dict(_USED_UP)


def overloaded_models() -> dict[str, tuple[int, int | None]]:
    """Models that stayed overloaded after their retries, with the number of calls the
    next model answered instead and the last HTTP status."""
    return dict(_OVERLOADED)


def reset_quota_memory() -> None:
    """Forget rate-limited, overloaded and unavailable models (tests, or a long-running process)."""
    _USED_UP.clear()
    _OVERLOADED.clear()
    _UNAVAILABLE.clear()


def run_notes() -> list[str]:
    """One line per model that did not answer every call, for the run report (NFR-1, NFR-6)."""
    notes = [f"{model} unavailable ({reason})" for model, reason in _UNAVAILABLE.items()]
    notes += [f"{model} rate limited (HTTP 429) after retries, skipped for the rest of the run"
              for model in _USED_UP]
    notes += [overload_note(model, calls, status) for model, (calls, status) in _OVERLOADED.items()]
    return notes


def overload_note(model: str, calls: int, status: int | None = None) -> str:
    """'gpt-5.4 overloaded (HTTP 503) on 1 call'."""
    cause = f"HTTP {status}" if status else "no connection"
    return f"{model} overloaded ({cause}) on {calls} call{'s' if calls != 1 else ''}"


# ------------------------------------------------------------------ one answer
def complete(system_prompt: str, prompt: str) -> tuple[str, str]:
    """One answer from a Strands agent without tools; returns the text and the model that wrote it.

    Walks `model_plan()`: a model that runs out of output tokens is retried with the larger
    budget, then the next model is used; a model that cannot be built or is rate limited
    is skipped; a model still overloaded after the retries in `_ask` is skipped for this
    call only.
    """
    last: Exception | None = None
    overload: Exception | None = None
    skipped_now: set[str] = set()
    for model_name, max_output_tokens in model_plan():
        if model_name in _USED_UP or model_name in skipped_now:
            continue
        try:
            model = build_model(model=model_name, max_output_tokens=max_output_tokens)
        except LlmUnavailable as exc:
            _UNAVAILABLE[model_name] = str(exc)
            skipped_now.add(model_name)
            last = last or exc
            continue
        try:
            return _ask(model, system_prompt, prompt), model_name
        except Exception as exc:
            last = exc
            if _is_out_of_tokens(exc):
                continue
            status = http_status(exc)
            if status == 429 or _is_throttled(exc):
                _USED_UP[model_name] = _USED_UP.get(model_name, 0) + 1
                continue
            if _is_server_error(exc):          # 5xx after every retry: next model, this call only
                skipped_now.add(model_name)
                calls = _OVERLOADED.get(model_name, (0, None))[0] + 1
                _OVERLOADED[model_name] = (calls, status)
                overload = exc
                continue
            raise
    if overload is not None:                   # an overload explains more than a later rate limit
        raise overload
    if last is None or http_status(last) == 429 or _is_throttled(last):
        raise LlmQuotaExceeded("every Gen AI Hub model is rate limited: "
                               + (", ".join(_USED_UP) or "no model in the chain"))
    raise last


def _retry_strategy() -> Any:
    """Strands' own retry off: by default it retries a throttled call six times over about
    4 minutes, which looks like a silent hang. GESIT does its own short retries."""
    try:
        from strands import ModelRetryStrategy
    except ImportError:   # older strands-agents without the option
        return None
    return ModelRetryStrategy(max_attempts=1)


def _ask(model: Any, system_prompt: str, prompt: str) -> str:
    """One answer, retrying transient errors on the same model."""
    from strands import Agent

    options: dict[str, Any] = {}
    strategy = _retry_strategy()
    if strategy is not None:
        options["retry_strategy"] = strategy
    for attempt in range(1, LLM_ATTEMPTS + 1):
        agent = Agent(model=model, system_prompt=system_prompt, tools=[], callback_handler=None,
                      **options)
        try:
            return str(agent(prompt))
        except Exception as exc:
            if attempt == LLM_ATTEMPTS or not _is_transient(exc):
                raise
            time.sleep(retry_wait(attempt))
    raise AssertionError("unreachable")


def retry_wait(attempt: int) -> float:
    """Seconds to wait after failed try `attempt`: LLM_RETRY_WAITS_SECONDS plus up to
    LLM_RETRY_JITTER of it, so parallel callers do not all retry at the same moment.
    Only the timing is random; the answer and every number stay the same (NFR-2)."""
    base = LLM_RETRY_WAITS_SECONDS[min(attempt, len(LLM_RETRY_WAITS_SECONDS)) - 1]
    return base * (1.0 + random.uniform(0.0, LLM_RETRY_JITTER))


def _chain(exc: BaseException) -> list[BaseException]:
    """The exception and its causes; Strands wraps the provider's error."""
    found: list[BaseException] = []
    error: BaseException | None = exc
    while error is not None and len(found) < 6:
        found.append(error)
        error = error.__cause__ or error.__context__
    return found


def http_status(exc: BaseException) -> int | None:
    """HTTP status of a provider error: OpenAI SDK `status_code`, botocore
    `ResponseMetadata.HTTPStatusCode`, or an integer `code`."""
    for error in _chain(exc):
        status = getattr(error, "status_code", None)
        if isinstance(status, int):
            return status
        response = getattr(error, "response", None)
        if isinstance(response, dict):
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if isinstance(status, int):
                return status
        code = getattr(error, "code", None)
        if isinstance(code, int):
            return code
    return None


def _is_transient(exc: Exception) -> bool:
    """Overload (HTTP 5xx), lost connections and rate limits are worth one more try."""
    return _is_server_error(exc) or _is_throttled(exc) or http_status(exc) == 429


def _is_throttled(exc: BaseException) -> bool:
    return any(type(e).__name__ == "ModelThrottledException" for e in _chain(exc))


def _is_server_error(exc: BaseException) -> bool:
    """HTTP 5xx (e.g. 503 'model is overloaded') or no answer at all (connection, timeout)."""
    status = http_status(exc)
    if status is not None and status >= 500:
        return True
    return any(type(e).__name__ in _CONNECTION_ERRORS for e in _chain(exc))


def _is_out_of_tokens(exc: Exception) -> bool:
    """The answer was cut off at the output token budget (finish reason length / max_tokens)."""
    return type(exc).__name__ == "MaxTokensReachedException"


def error_detail(exc: Exception) -> str:
    """Short, key-free description of a model failure, with the HTTP code when there is one."""
    if isinstance(exc, LlmUnavailable):
        return str(exc)
    status = http_status(exc)
    return type(exc).__name__ + (f" HTTP {status}" if status else "")
