"""SAP AI Core token and deployment lookup (integrations/sap/aicore.py), no network."""
import pytest

from integrations.sap import aicore

CONFIG = aicore.AiCoreConfig(auth_url="https://auth.example", client_id="id",
                             client_secret="secret", base_url="https://api.example/v2",
                             resource_group="default")


def deployment(name, version, status="RUNNING", scenario="foundation-models", id_="d1"):
    return {"id": id_, "status": status, "scenarioId": scenario,
            "deploymentUrl": f"https://api.example/v2/inference/deployments/{id_}/",
            "details": {"resources": {"backend_details": {"model": {"name": name, "version": version}}}}}


class Response:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, resources, deployments_status=200):
        self.resources, self.deployments_status = resources, deployments_status
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url))
        return Response(200, {"access_token": "token", "expires_in": 3600})

    def get(self, url, headers=None, params=None, **kwargs):
        self.calls.append(("GET", url, headers["AI-Resource-Group"], params["scenarioId"]))
        return Response(self.deployments_status, {"resources": self.resources})


def test_config_from_env():
    assert aicore.config_from_env({}) is None
    env = {"AICORE_AUTH_URL": "a", "AICORE_CLIENT_ID": "b", "AICORE_CLIENT_SECRET": "c",
           "AICORE_BASE_URL": "https://api.example", "AICORE_RESOURCE_GROUP": ""}
    config = aicore.config_from_env(env)
    assert config.resource_group == "default"
    assert aicore.api_url(config) == "https://api.example/v2"
    assert aicore.api_url(CONFIG) == "https://api.example/v2"


def test_find_deployment_matches_running_foundation_model_by_name():
    resources = [deployment("gpt-5.4", "2026-01-01", status="STOPPED", id_="old"),
                 deployment("sap-rpt-1.6", "1", id_="rpt"),
                 deployment("gpt-5.4", "2026-03-05", id_="gpt"),
                 deployment("gpt-5.4", "x", scenario="orchestration", id_="orch")]
    found = aicore.find_deployment(resources, "gpt-5.4")
    assert found == aicore.Deployment(id="gpt", model="gpt-5.4", version="2026-03-05",
                                      url="https://api.example/v2/inference/deployments/gpt")
    assert aicore.find_deployment(resources, "anthropic--claude-4.6-sonnet") is None


def test_session_caches_token_and_deployment():
    session = FakeSession([deployment("gpt-5.4", "2026-03-05")])
    client = aicore.AiCoreSession(CONFIG, session=session)
    assert client.deployment("gpt-5.4").version == "2026-03-05"
    assert client.deployment("gpt-5.4").id == "d1"
    assert session.calls == [("POST", "https://auth.example/oauth/token"),
                             ("GET", "https://api.example/v2/lm/deployments", "default",
                              "foundation-models")]


def test_missing_deployment_and_http_errors_raise():
    client = aicore.AiCoreSession(CONFIG, session=FakeSession([]))
    with pytest.raises(aicore.AiCoreUnavailable, match="no RUNNING SAP AI Core deployment for gpt-5.4"):
        client.deployment("gpt-5.4")
    client = aicore.AiCoreSession(CONFIG, session=FakeSession([], deployments_status=401))
    with pytest.raises(aicore.AiCoreUnavailable, match="HTTP 401"):
        client.deployment("gpt-5.4")
