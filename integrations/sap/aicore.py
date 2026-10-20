"""SAP AI Core access shared by the Gen AI Hub models (agents/llm.py).

Environment variables (NFR-4, the same service key as SAP-RPT in `rpt_client.py`):
`AICORE_AUTH_URL`, `AICORE_CLIENT_ID`, `AICORE_CLIENT_SECRET`, `AICORE_BASE_URL`,
`AICORE_RESOURCE_GROUP` (default `default`).

A Gen AI Hub model is found by its name (for example `gpt-5.4`) among the RUNNING
deployments of the `foundation-models` scenario, so no deployment id is hard-coded.
The deployment's model version is reported with every answer (NFR-2).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Mapping

import requests

FOUNDATION_MODELS = "foundation-models"
ENV_KEYS = ("AICORE_AUTH_URL", "AICORE_CLIENT_ID", "AICORE_CLIENT_SECRET", "AICORE_BASE_URL")


class AiCoreUnavailable(RuntimeError):
    """SAP AI Core cannot be reached, or the model is not deployed."""


@dataclass(frozen=True)
class AiCoreConfig:
    auth_url: str
    client_id: str
    client_secret: str
    base_url: str
    resource_group: str = "default"


@dataclass(frozen=True)
class Deployment:
    id: str
    url: str                       # inference URL, ends with /v2/inference/deployments/<id>
    model: str
    version: str


def config_from_env(env: Mapping[str, str] | None = None) -> AiCoreConfig | None:
    """Config from environment variables, or None when a key is missing."""
    env = os.environ if env is None else env
    if not all(env.get(k) for k in ENV_KEYS):
        return None
    return AiCoreConfig(auth_url=env["AICORE_AUTH_URL"], client_id=env["AICORE_CLIENT_ID"],
                        client_secret=env["AICORE_CLIENT_SECRET"], base_url=env["AICORE_BASE_URL"],
                        resource_group=env.get("AICORE_RESOURCE_GROUP") or "default")


def api_url(config: AiCoreConfig) -> str:
    """`AICORE_BASE_URL` with exactly one `/v2` at the end."""
    base = config.base_url.rstrip("/")
    return base if base.endswith("/v2") else base + "/v2"


def find_deployment(resources: list[Mapping[str, Any]], model: str) -> Deployment | None:
    """The RUNNING foundation-models deployment serving `model`, newest version first."""
    found = []
    for item in resources:
        backend = ((item.get("details") or {}).get("resources") or {}).get("backend_details") or {}
        info = backend.get("model") or {}
        if (item.get("status") == "RUNNING" and item.get("scenarioId") == FOUNDATION_MODELS
                and info.get("name") == model and item.get("deploymentUrl")):
            found.append(Deployment(id=item["id"], url=item["deploymentUrl"].rstrip("/"),
                                    model=model, version=str(info.get("version", ""))))
    return max(found, key=lambda d: d.version) if found else None


class AiCoreSession:
    """OAuth token and deployment lookup. `session` is injectable so tests never touch the network."""

    def __init__(self, config: AiCoreConfig, session: Any = None, timeout: float = 30.0) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.timeout = timeout
        self._token: str | None = None
        self._token_expires = 0.0
        self._deployments: dict[str, Deployment] = {}

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}",
                "AI-Resource-Group": self.config.resource_group}

    def token(self) -> str:
        """OAuth client-credentials token, cached until shortly before it expires."""
        if self._token and time.time() < self._token_expires:
            return self._token
        url = self.config.auth_url.rstrip("/")
        if not url.endswith("/oauth/token"):
            url += "/oauth/token"
        try:
            response = self.session.post(
                url, data={"grant_type": "client_credentials"},
                auth=(self.config.client_id, self.config.client_secret), timeout=self.timeout)
        except requests.RequestException as exc:
            raise AiCoreUnavailable(f"SAP AI Core token request failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise AiCoreUnavailable(f"SAP AI Core token request returned HTTP {response.status_code}")
        payload = response.json()
        self._token = payload["access_token"]
        self._token_expires = time.time() + float(payload.get("expires_in", 600)) - 60
        return self._token

    def deployment(self, model: str) -> Deployment:
        """The deployment serving `model`, looked up once per process."""
        if model in self._deployments:
            return self._deployments[model]
        try:
            response = self.session.get(
                api_url(self.config) + "/lm/deployments", headers=self.headers(),
                params={"scenarioId": FOUNDATION_MODELS, "status": "RUNNING"}, timeout=self.timeout)
        except requests.RequestException as exc:
            raise AiCoreUnavailable(f"SAP AI Core deployment list failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise AiCoreUnavailable(f"SAP AI Core deployment list returned HTTP {response.status_code}")
        found = find_deployment(response.json().get("resources", []), model)
        if found is None:
            raise AiCoreUnavailable(f"no RUNNING SAP AI Core deployment for {model} "
                                    f"in resource group {self.config.resource_group}")
        self._deployments[model] = found
        return found
