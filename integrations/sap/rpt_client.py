"""SAP-RPT-1.6 client for P(late) per PO line (FR-DET-1).

SAP-RPT is an in-context tabular model: one request carries labelled history
rows (context) plus the rows to predict, whose target cell holds `[PREDICT]`.

Two access paths, chosen from environment variables (NFR-4, no secrets in code):

- `aicore`: a SAP-RPT deployment on SAP AI Core.
  `AICORE_AUTH_URL`, `AICORE_CLIENT_ID`, `AICORE_CLIENT_SECRET`, `AICORE_BASE_URL`,
  `AICORE_RESOURCE_GROUP` (default `default`), `SAP_RPT_DEPLOYMENT_ID`.
- `playground`: the public RPT API at rpt.cloud.sap, for development (PRD section 15).
  `SAP_RPT_API_TOKEN`, optional `SAP_RPT_API_URL`.

`SAP_RPT_MODE` forces one path. With no credentials the client raises
`RptUnavailable` and the caller falls back to the reliability posterior (FR-DET-4).

Retries (NFR-6): the token request and the predict call are tried up to RPT_ATTEMPTS
times when the connection fails or the server answers HTTP 429 or 5xx, so one network
drop does not switch a demo run to the fallback. A read timeout is not retried: the
request reached SAP-RPT, and another 30 s wait would push the run past NFR-1. Each
retry is noted in `retries` for the run report.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import requests

PREDICT_PLACEHOLDER = "[PREDICT]"
TARGET_COLUMN = "LATE"
INDEX_COLUMN = "EBELN"
# String labels, so the target is read as a class and never as a number.
LABEL_LATE = "LATE"
LABEL_ON_TIME = "ON_TIME"

# Features known on the order date (docs/data_dictionary.md, rpt_po_lines).
# DELAY_DAYS is a label and is never sent.
FEATURE_COLUMNS = (
    "LIFNR", "BEDAT", "EINDT", "ORDER_MONTH", "MENGE", "LEAD_DAYS", "NETPR",
    "PRICE_VS_MARKET", "RAINY_SEASON", "LEBARAN_WINDOW", "SUPPLIER_UTIL_30D",
    "SUP_N_RECEIPTS_ALL", "SUP_N_RECEIPTS_90D", "SUP_LATE_RATE_90D", "SUP_AVG_DELAY_90D",
)

DEFAULT_PLAYGROUND_URL = "https://rpt.cloud.sap/api/predict"
DEFAULT_MODEL_ID = "sap-rpt-1.6"
MAX_QUERY_ROWS = 128   # rows scored per call (PRD section 16)
# Waits before try 2 and 3 after a lost connection or an HTTP 429 / 5xx answer.
RPT_RETRY_WAITS_SECONDS = (2.0, 5.0)
RPT_ATTEMPTS = len(RPT_RETRY_WAITS_SECONDS) + 1
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class RptUnavailable(RuntimeError):
    """SAP-RPT cannot be reached or returned something unusable."""


@dataclass(frozen=True)
class RptConfig:
    mode: str                      # "aicore" or "playground"
    model_id: str = DEFAULT_MODEL_ID
    # aicore
    auth_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    base_url: str = ""
    resource_group: str = "default"
    deployment_id: str = ""
    # playground
    api_url: str = DEFAULT_PLAYGROUND_URL
    api_token: str = ""


def config_from_env(env: Mapping[str, str] | None = None) -> RptConfig | None:
    """Config from environment variables, or None when no credentials are set."""
    env = os.environ if env is None else env
    model_id = env.get("SAP_RPT_MODEL_ID", DEFAULT_MODEL_ID)
    aicore_keys = ("AICORE_AUTH_URL", "AICORE_CLIENT_ID", "AICORE_CLIENT_SECRET",
                   "AICORE_BASE_URL", "SAP_RPT_DEPLOYMENT_ID")
    has_aicore = all(env.get(k) for k in aicore_keys)
    has_playground = bool(env.get("SAP_RPT_API_TOKEN"))
    mode = env.get("SAP_RPT_MODE", "").lower() or ("aicore" if has_aicore else "playground")

    if mode == "aicore" and has_aicore:
        return RptConfig(
            mode="aicore", model_id=model_id,
            auth_url=env["AICORE_AUTH_URL"], client_id=env["AICORE_CLIENT_ID"],
            client_secret=env["AICORE_CLIENT_SECRET"], base_url=env["AICORE_BASE_URL"],
            resource_group=env.get("AICORE_RESOURCE_GROUP", "default"),
            deployment_id=env["SAP_RPT_DEPLOYMENT_ID"],
        )
    if mode == "playground" and has_playground:
        return RptConfig(
            mode="playground", model_id=model_id,
            api_url=env.get("SAP_RPT_API_URL", DEFAULT_PLAYGROUND_URL),
            api_token=env["SAP_RPT_API_TOKEN"],
        )
    return None


def _feature_cells(row: Mapping[str, Any]) -> dict[str, Any]:
    cells: dict[str, Any] = {INDEX_COLUMN: str(row[INDEX_COLUMN])}
    for column in FEATURE_COLUMNS:
        cells[column] = row.get(column)
    return cells


def build_rows(
    context_rows: list[Mapping[str, Any]], query_rows: list[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Request rows: labelled context first, then the rows to predict."""
    rows = []
    for row in context_rows:
        if row.get(TARGET_COLUMN) is None:
            continue   # an unlabelled row cannot serve as context
        cells = _feature_cells(row)
        cells[TARGET_COLUMN] = LABEL_LATE if int(row[TARGET_COLUMN]) == 1 else LABEL_ON_TIME
        rows.append(cells)
    for row in query_rows:
        cells = _feature_cells(row)
        cells[TARGET_COLUMN] = PREDICT_PLACEHOLDER
        rows.append(cells)
    return rows


def parse_predictions(body: Mapping[str, Any]) -> dict[str, float]:
    """P(late) per EBELN from an RPT response.

    The model returns the predicted class and its confidence. P(late) is the
    confidence when the class is LATE, and 1 minus the confidence otherwise.
    """
    payload = body.get("prediction", body)   # the playground wraps the AI Core payload
    status = payload.get("status") or {}
    if status.get("code", 0) != 0:
        raise RptUnavailable(f"SAP-RPT status {status.get('code')}: {status.get('message')}")
    result: dict[str, float] = {}
    for entry in payload.get("predictions") or []:
        cells = entry.get(TARGET_COLUMN) or []
        if INDEX_COLUMN not in entry or not cells:
            raise RptUnavailable("SAP-RPT response has no prediction for the LATE column")
        label, confidence = cells[0].get("prediction"), cells[0].get("confidence")
        if confidence is None or label not in (LABEL_LATE, LABEL_ON_TIME):
            raise RptUnavailable("SAP-RPT response has no usable class confidence")
        p_late = float(confidence) if label == LABEL_LATE else 1.0 - float(confidence)
        result[str(entry[INDEX_COLUMN])] = min(1.0, max(0.0, p_late))
    return result


class RptClient:
    """Calls SAP-RPT. `session` is injectable so tests never touch the network."""

    def __init__(self, config: RptConfig | None = None, session: Any = None,
                 timeout: float = 30.0, sleep: Callable[[float], None] = time.sleep) -> None:
        self.config = config if config is not None else config_from_env()
        self.session = session or requests.Session()
        self.timeout = timeout
        self.sleep = sleep
        self.retries: list[str] = []   # one line per retry, newest last
        self._token: str | None = None
        self._token_expires = 0.0

    @property
    def model_id(self) -> str:
        return self.config.model_id if self.config else DEFAULT_MODEL_ID

    def predict_late(
        self, context_rows: list[Mapping[str, Any]], query_rows: list[Mapping[str, Any]]
    ) -> dict[str, float]:
        """P(late) per EBELN for `query_rows`, using `context_rows` as labelled history."""
        if self.config is None:
            raise RptUnavailable("SAP-RPT credentials are not configured")
        result: dict[str, float] = {}
        for start in range(0, len(query_rows), MAX_QUERY_ROWS):
            batch = query_rows[start:start + MAX_QUERY_ROWS]
            result.update(self._predict_batch(context_rows, batch))
        missing = [str(r[INDEX_COLUMN]) for r in query_rows if str(r[INDEX_COLUMN]) not in result]
        if missing:
            raise RptUnavailable(f"SAP-RPT returned no prediction for {', '.join(missing)}")
        return result

    # ------------------------------------------------------------- internals
    def _predict_batch(self, context_rows: list[Mapping[str, Any]],
                       query_rows: list[Mapping[str, Any]]) -> dict[str, float]:
        rows = build_rows(context_rows, query_rows)
        if self.config.mode == "aicore":
            url = self._aicore_url()
            headers = {"Authorization": f"Bearer {self._aicore_token()}",
                       "AI-Resource-Group": self.config.resource_group}
            body: dict[str, Any] = {
                "prediction_config": {"target_columns": [{
                    "name": TARGET_COLUMN, "prediction_placeholder": PREDICT_PLACEHOLDER,
                    "task_type": "classification"}]},
                "index_column": INDEX_COLUMN,
                "rows": rows,
            }
        else:
            url = self.config.api_url
            headers = {"Authorization": f"Bearer {self.config.api_token}"}
            body = {"rows": rows, "index_column": INDEX_COLUMN, "scenario": "gesit-delay-risk"}
        response = self._post("SAP-RPT request", url, json=body, headers=headers)
        try:
            return parse_predictions(response.json())
        except ValueError as exc:
            raise RptUnavailable("SAP-RPT returned a body that is not JSON") from exc

    def _aicore_url(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/v2"):
            base = base[:-3]
        return f"{base}/v2/inference/deployments/{self.config.deployment_id}/predict"

    def _aicore_token(self) -> str:
        """OAuth client-credentials token, cached until shortly before it expires."""
        if self._token and time.time() < self._token_expires:
            return self._token
        url = self.config.auth_url.rstrip("/")
        if not url.endswith("/oauth/token"):
            url += "/oauth/token"
        response = self._post("SAP AI Core token request", url,
                              data={"grant_type": "client_credentials"},
                              auth=(self.config.client_id, self.config.client_secret))
        payload = response.json()
        self._token = payload["access_token"]
        self._token_expires = time.time() + float(payload.get("expires_in", 600)) - 60
        return self._token

    def _post(self, what: str, url: str, **kwargs: Any) -> Any:
        """POST that returns an HTTP 200 response or raises `RptUnavailable`, retrying a lost
        connection or an HTTP 429 / 5xx answer after RPT_RETRY_WAITS_SECONDS."""
        for attempt in range(1, RPT_ATTEMPTS + 1):
            try:
                response = self.session.post(url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                if attempt == RPT_ATTEMPTS or not is_retryable(exc):
                    raise RptUnavailable(f"{what} failed: {type(exc).__name__}"
                                         + tries_text(attempt)) from exc
                problem = type(exc).__name__
            else:
                if response.status_code == 200:
                    return response
                if attempt == RPT_ATTEMPTS or response.status_code not in RETRY_STATUS:
                    raise RptUnavailable(f"{what} returned HTTP {response.status_code}"
                                         + tries_text(attempt))
                problem = f"HTTP {response.status_code}"
            self.retries.append(f"{what}: {problem}, tried again")
            self.sleep(RPT_RETRY_WAITS_SECONDS[attempt - 1])
        raise AssertionError("unreachable")


def is_retryable(exc: requests.RequestException) -> bool:
    """A lost or refused connection is worth another try; a read timeout is not (NFR-1)."""
    if isinstance(exc, requests.ReadTimeout):
        return False
    return isinstance(exc, (requests.ConnectionError, requests.exceptions.ChunkedEncodingError))


def tries_text(attempt: int) -> str:
    return f" after {attempt} tries" if attempt > 1 else ""
