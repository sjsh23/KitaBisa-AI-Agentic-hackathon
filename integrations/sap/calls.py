"""Timing of every external call (NFR-1): SAP-RPT requests, Gen AI Hub answers, SAP AI Core
token fetches and deployment lookups, plus SAP-RPT answers served from the cache.

A process-wide, thread-safe record. `agents.supervisor.StepNode` takes a mark before a
step and copies the calls made since into the plan log (STEPS[].CALLS), so the log shows
where each step's seconds go. Calls made on other threads (parallel model texts) are
recorded the same way. No request body, answer or key is ever recorded.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Iterator

SAP_RPT = "SAP-RPT"
GEN_AI_HUB = "GEN_AI_HUB"
AICORE_TOKEN = "AICORE_TOKEN"
AICORE_DEPLOYMENT = "AICORE_DEPLOYMENT"

KEEP = 500   # newest calls kept in memory

_LOCK = threading.Lock()
_CALLS: deque[tuple[int, dict[str, Any]]] = deque(maxlen=KEEP)
_COUNT = 0


def record(kind: str, what: str, seconds: float, result: str = "ok") -> None:
    """Add one call; RESULT is ok, error or cached."""
    global _COUNT
    with _LOCK:
        _COUNT += 1
        _CALLS.append((_COUNT, {"KIND": kind, "WHAT": what, "SECONDS": round(seconds, 2),
                                "RESULT": result, "THREAD": threading.current_thread().name}))


@contextmanager
def timed(kind: str, what: str) -> Iterator[None]:
    """Record the seconds of the block as one call; an exception marks it error."""
    start = time.perf_counter()
    result = "ok"
    try:
        yield
    except BaseException:
        result = "error"
        raise
    finally:
        record(kind, what, time.perf_counter() - start, result)


def mark() -> int:
    """A position in the record; `since(mark)` returns the calls made after it."""
    with _LOCK:
        return _COUNT


def since(position: int) -> list[dict[str, Any]]:
    with _LOCK:
        return [dict(call) for n, call in _CALLS if n > position]


def rpt_status(calls: list[dict[str, Any]]) -> str | None:
    """"SAP-RPT: called", "SAP-RPT: cached" or "SAP-RPT: called and cached" for a step's
    calls, or None when the step used no SAP-RPT prediction."""
    results = {c["RESULT"] for c in calls if c["KIND"] == SAP_RPT}
    if not results:
        return None
    called = bool(results - {"cached"})
    cached = "cached" in results
    return "SAP-RPT: " + " and ".join(w for w, on in (("called", called), ("cached", cached))
                                      if on)
