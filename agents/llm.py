"""The reasoning model for the agents: Gemini Flash through the Strands Gemini provider.

Environment variables (NFR-4, never hard-code keys):
- `GEMINI_API_KEY`        required (`GOOGLE_API_KEY` is accepted too)
- `GESIT_LLM_MODEL`       default `gemini-3.5-flash`, a fixed version so the demo cannot
  change under us (NFR-2). The alias `gemini-flash-latest` returned HTTP 503 too often.
- `GESIT_LLM_TEMPERATURE` default `0.2` (NFR-2: at or below 0.2). Set it to an
  empty string to leave the temperature at the model default.
- `GESIT_LLM_FALLBACK_MODEL` optional, empty by default. A second Gemini model that is
  tried only when the main model still runs out of output tokens after the larger retry.
"""
from __future__ import annotations

import os
from typing import Any, Mapping

DEFAULT_MODEL_ID = "gemini-3.5-flash"
DEFAULT_TEMPERATURE = "0.2"
MAX_TEMPERATURE = 0.2
# Flash models spend most of this budget on thinking, so it is far above the 120-word answer.
# It is a ceiling, not a cost: only the tokens actually produced are counted.
MAX_OUTPUT_TOKENS = 8192
RETRY_OUTPUT_TOKENS = 32768     # second try when the first one runs out of output tokens


class LlmUnavailable(RuntimeError):
    """The reasoning model cannot be used (missing key or missing package)."""


def model_id(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return env.get("GESIT_LLM_MODEL") or DEFAULT_MODEL_ID


def fallback_model_id(env: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    return env.get("GESIT_LLM_FALLBACK_MODEL", "").strip() or None


def model_plan(env: Mapping[str, str] | None = None) -> list[tuple[str, int]]:
    """(model id, output token budget) to try in order; the next one is used only when
    the one before it ran out of output tokens."""
    main, fallback = model_id(env), fallback_model_id(env)
    plan = [(main, MAX_OUTPUT_TOKENS), (main, RETRY_OUTPUT_TOKENS)]
    if fallback and fallback != main:
        plan.append((fallback, RETRY_OUTPUT_TOKENS))
    return plan


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
