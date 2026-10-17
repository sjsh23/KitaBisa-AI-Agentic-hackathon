"""The reasoning model for the agents: Gemini Flash through the Strands Gemini provider.

Environment variables (NFR-4, never hard-code keys):
- `GEMINI_API_KEY`        required (`GOOGLE_API_KEY` is accepted too)
- `GESIT_LLM_MODEL`       default `gemini-3.5-flash`, a fixed version so the demo cannot
  change under us (NFR-2). The alias `gemini-flash-latest` returned HTTP 503 too often.
- `GESIT_LLM_TEMPERATURE` default `0.2` (NFR-2: at or below 0.2). Set it to an
  empty string to leave the temperature at the model default.
- `GESIT_LLM_FALLBACK_MODEL` default `gemini-2.5-flash`; a comma-separated list is
  allowed, `none` switches the fallback off. A fallback model is used only when the
  model before it is out of quota, runs out of output tokens twice, or is still
  overloaded (HTTP 5xx) after its retries.

Quota (NFR-1, NFR-6): Gemini counts quotas per model. A 429 for a per-day quota, or
one that asks to wait longer than QUOTA_SWITCH_SECONDS, marks the model as used up
for the rest of the process and the next model in the chain answers. A short
per-minute 429 is waited out and retried. When every model is used up the call
fails at once with `LlmQuotaExceeded`; it never sits in a long retry loop.

Overload (NFR-1, NFR-6): an HTTP 503 "model is overloaded" from Gemini is retried on
the same model after LLM_RETRY_WAITS_SECONDS (with a little jitter). If the model is
still overloaded, the next model in the chain answers this call only; the next call
tries the main model again, because an overload usually passes within a minute.
"""
from __future__ import annotations

import os
import random
import re
import time
from typing import Any, Mapping

DEFAULT_MODEL_ID = "gemini-3.5-flash"
DEFAULT_FALLBACK_MODELS = "gemini-2.5-flash"
NO_FALLBACK = "none"
DEFAULT_TEMPERATURE = "0.2"
MAX_TEMPERATURE = 0.2
# Flash models spend most of this budget on thinking, so it is far above the 120-word answer.
# It is a ceiling, not a cost: only the tokens actually produced are counted.
MAX_OUTPUT_TOKENS = 8192
RETRY_OUTPUT_TOKENS = 32768     # second try when the first one runs out of output tokens

# Waits before try 2, 3 and 4 on the same model after an overload (HTTP 5xx) or rate limit.
# Two tries 2 and 4 s apart were too short: a 503 burst usually lasts longer than 6 s.
LLM_RETRY_WAITS_SECONDS = (2.0, 5.0, 10.0)
LLM_ATTEMPTS = len(LLM_RETRY_WAITS_SECONDS) + 1
LLM_RETRY_JITTER = 0.25         # each wait is stretched by up to this share, at random
QUOTA_SWITCH_SECONDS = 60.0     # a 429 asking to wait longer than this: switch model now
MAX_QUOTA_WAIT_SECONDS = 30.0   # longest wait for a short (per-minute) 429


class LlmUnavailable(RuntimeError):
    """The reasoning model cannot be used (missing key or missing package)."""


class LlmQuotaExceeded(LlmUnavailable):
    """Every model in the chain is out of quota."""


# Models found out of quota in this process: model id -> quota info (see `quota_info`).
_USED_UP: dict[str, dict[str, Any]] = {}
# Models that were still overloaded after their retries: model id -> number of calls.
# Only reported in the run notes; an overloaded model is not skipped on later calls.
_OVERLOADED: dict[str, int] = {}


def model_id(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return env.get("GESIT_LLM_MODEL") or DEFAULT_MODEL_ID


def fallback_model_ids(env: Mapping[str, str] | None = None) -> list[str]:
    """Fallback models in order; `gemini-2.5-flash` unless the environment says otherwise."""
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


def build_model(env: Mapping[str, str] | None = None, model: str | None = None,
                max_output_tokens: int = MAX_OUTPUT_TOKENS) -> Any:
    """A Strands `GeminiModel` for Gemini Flash (or `model`), or `LlmUnavailable`."""
    env = os.environ if env is None else env
    api_key = env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY")
    if not api_key:
        raise LlmUnavailable("GEMINI_API_KEY is not set")
    try:
        from strands.models.gemini import GeminiModel
    except ImportError as exc:
        raise LlmUnavailable("strands-agents[gemini] is not installed") from exc
    return GeminiModel(client_args={"api_key": api_key}, model_id=model or model_id(env),
                       params=model_params(env, max_output_tokens))


# ------------------------------------------------------------------ quota
def _seconds(text: Any) -> float | None:
    """'48700s' or '12.5s' (protobuf duration) as seconds."""
    match = re.fullmatch(r"\s*([\d.]+)s\s*", str(text or ""))
    return float(match.group(1)) if match else None


def quota_info(exc: BaseException) -> dict[str, Any] | None:
    """For a Gemini 429 (raised directly or wrapped by Strands): the quota ids it names and
    the wait it asks for. None when the error is not a 429."""
    chain: list[BaseException] = []
    error: BaseException | None = exc
    while error is not None and len(chain) < 6:
        chain.append(error)
        error = error.__cause__ or error.__context__
    # The Gemini ClientError (code 429) carries the details; Strands wraps it.
    hit = next((e for e in chain if getattr(e, "code", None) == 429), None)
    if hit is None:
        hit = next((e for e in chain if "RESOURCE_EXHAUSTED" in str(e)[:500]), None)
    if hit is None:
        return None
    details = getattr(hit, "details", None) or {}
    items = (details.get("error", {}).get("details") or []) if isinstance(details, dict) else []
    quota_ids, retry = [], None
    for item in items:
        kind = str(item.get("@type", "")).rsplit(".", 1)[-1]
        if kind == "QuotaFailure":
            quota_ids += [v.get("quotaId", "") for v in item.get("violations", [])]
        elif kind == "RetryInfo":
            retry = _seconds(item.get("retryDelay"))
    return {"QUOTA_IDS": [q for q in quota_ids if q], "RETRY_SECONDS": retry}


def is_used_up(info: Mapping[str, Any]) -> bool:
    """A per-day quota, or a wait longer than QUOTA_SWITCH_SECONDS: do not wait, switch model."""
    retry = info.get("RETRY_SECONDS")
    return (any("PerDay" in q for q in info.get("QUOTA_IDS", []))
            or (retry is not None and retry > QUOTA_SWITCH_SECONDS))


def used_up_models() -> dict[str, dict[str, Any]]:
    """Models found out of quota in this process, for the run report."""
    return dict(_USED_UP)


def overloaded_models() -> dict[str, int]:
    """Models that stayed overloaded after their retries in this process, with the number
    of calls the next model answered instead, for the run report."""
    return dict(_OVERLOADED)


def reset_quota_memory() -> None:
    """Forget used-up and overloaded models (tests, or a long-running process)."""
    _USED_UP.clear()
    _OVERLOADED.clear()


def overload_note(model: str, calls: int) -> str:
    """'gemini-3.5-flash overloaded (HTTP 503) on 1 call'."""
    return f"{model} overloaded (HTTP 503) on {calls} call{'s' if calls != 1 else ''}"


def quota_note(model: str, info: Mapping[str, Any]) -> str:
    """'gemini-3.5-flash out of quota (GenerateRequestsPerDay..., retry in 13.5 h)'."""
    parts = [", ".join(info.get("QUOTA_IDS") or []) or "rate limit"]
    retry = info.get("RETRY_SECONDS")
    if retry is not None:
        parts.append(f"retry in {retry / 3600:.1f} h" if retry >= 3600 else f"retry in {retry:.0f} s")
    return f"{model} out of quota ({', '.join(parts)})"


# ------------------------------------------------------------------ one answer
def complete(system_prompt: str, prompt: str) -> tuple[str, str]:
    """One answer from a Strands agent without tools; returns the text and the model that wrote it.

    Walks `model_plan()`: a model that runs out of output tokens is retried with the larger
    budget, then the next model is used; a model that is out of quota is skipped at once;
    a model still overloaded after the retries in `_ask` is skipped for this call only.
    """
    last: Exception | None = None
    overload: Exception | None = None
    overloaded_now: set[str] = set()
    for model_name, max_output_tokens in model_plan():
        if model_name in _USED_UP or model_name in overloaded_now:
            continue
        model = build_model(model=model_name, max_output_tokens=max_output_tokens)
        try:
            return _ask(model, system_prompt, prompt), model_name
        except Exception as exc:
            last = exc
            info = quota_info(exc)
            if info is not None:               # any 429 that _ask did not wait out
                _USED_UP[model_name] = info
                continue
            if _is_out_of_tokens(exc):
                continue
            if _is_server_error(exc):          # 5xx after every retry: next model, this call only
                overloaded_now.add(model_name)
                _OVERLOADED[model_name] = _OVERLOADED.get(model_name, 0) + 1
                overload = exc
                continue
            raise
    if overload is not None:                   # a 503 explains more than a later quota error
        raise overload
    if last is None or quota_info(last) is not None:
        raise LlmQuotaExceeded("Gemini quota used up on every model: " + "; ".join(
            quota_note(m, i) for m, i in _USED_UP.items()))
    raise last


def _retry_strategy() -> Any:
    """Strands' own retry off: by default it retries a 429 six times over about 4 minutes,
    which hid a used-up quota as a silent hang. GESIT does its own short retries."""
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
            info = quota_info(exc)
            if info is not None and is_used_up(info):
                raise                          # waiting will not help, switch model
            if attempt == LLM_ATTEMPTS or not _is_transient(exc):
                raise
            wait = (info or {}).get("RETRY_SECONDS") or retry_wait(attempt)
            time.sleep(min(wait, MAX_QUOTA_WAIT_SECONDS))
    raise AssertionError("unreachable")


def retry_wait(attempt: int) -> float:
    """Seconds to wait after failed try `attempt`: LLM_RETRY_WAITS_SECONDS plus up to
    LLM_RETRY_JITTER of it, so parallel callers do not all retry at the same moment.
    Only the timing is random; the answer and every number stay the same (NFR-2)."""
    base = LLM_RETRY_WAITS_SECONDS[min(attempt, len(LLM_RETRY_WAITS_SECONDS)) - 1]
    return base * (1.0 + random.uniform(0.0, LLM_RETRY_JITTER))


def _is_transient(exc: Exception) -> bool:
    """Overload (HTTP 5xx) and rate-limit errors are worth one more try."""
    return _is_server_error(exc) or type(exc).__name__ == "ModelThrottledException"


def _is_server_error(exc: Exception) -> bool:
    """Gemini's ServerError (HTTP 5xx, e.g. 503 UNAVAILABLE 'The model is overloaded')."""
    return type(exc).__name__ == "ServerError"


def _is_out_of_tokens(exc: Exception) -> bool:
    """The answer was cut off at the output token budget (Gemini finish reason MAX_TOKENS)."""
    return type(exc).__name__ == "MaxTokensReachedException"


def error_detail(exc: Exception) -> str:
    """Short, key-free description of a model failure, with the HTTP code when there is one."""
    if isinstance(exc, LlmUnavailable):
        return str(exc)
    parts = [type(exc).__name__]
    for attribute in ("code", "status"):
        value = getattr(exc, attribute, None)
        if isinstance(value, (int, str)) and value != "":
            parts.append(str(value))
    return " ".join(parts)
