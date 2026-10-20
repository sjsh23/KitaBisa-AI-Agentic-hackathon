"""SAP-RPT client and the predict_delay_risk tool (FR-DET-1, FR-DET-4). No network."""
import pytest

from adapters import CsvAdapter
from integrations.sap.rpt_client import (
    RptClient, RptConfig, RptUnavailable, build_rows, config_from_env, parse_predictions,
)
from scoring.reliability import posterior
from tools import predict_delay_risk

CONTEXT = [
    {"EBELN": "1", "LIFNR": "100101", "MENGE": 5000.0, "LATE": 1, "DELAY_DAYS": 3, "SPLIT": "train"},
    {"EBELN": "2", "LIFNR": "100102", "MENGE": 6000.0, "LATE": 0, "DELAY_DAYS": 0, "SPLIT": "train"},
]
QUERY = [{"EBELN": "9", "LIFNR": "100101", "MENGE": 7000.0, "LATE": None, "DELAY_DAYS": None}]


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code, self._body = status_code, body or {}

    def json(self):
        return self._body


class FakeSession:
    """Answers the token call and the predict call; records what was sent."""

    def __init__(self, predict_response):
        self.predict_response, self.calls = predict_response, []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/oauth/token"):
            return FakeResponse(body={"access_token": "tok", "expires_in": 3600})
        return self.predict_response


def rpt_body(label, confidence, ebeln="9"):
    return {"status": {"code": 0, "message": "ok"},
            "predictions": [{"EBELN": ebeln, "LATE": [{"prediction": label, "confidence": confidence}]}]}


AICORE = RptConfig(mode="aicore", auth_url="https://auth.example", client_id="id",
                   client_secret="secret", base_url="https://api.example/v2",
                   resource_group="rg", deployment_id="d123")


def test_build_rows_labels_context_and_never_sends_delay_days():
    rows = build_rows(CONTEXT, QUERY)
    assert [r["LATE"] for r in rows] == ["LATE", "ON_TIME", "[PREDICT]"]
    assert all("DELAY_DAYS" not in r and "SPLIT" not in r for r in rows)
    assert rows[2]["EBELN"] == "9" and rows[2]["MENGE"] == 7000.0


def test_parse_predictions_turns_class_confidence_into_p_late():
    assert parse_predictions(rpt_body("LATE", 0.8)) == {"9": pytest.approx(0.8)}
    assert parse_predictions(rpt_body("ON_TIME", 0.8)) == {"9": pytest.approx(0.2)}
    assert parse_predictions({"prediction": rpt_body("LATE", 0.6)}) == {"9": pytest.approx(0.6)}


def test_parse_predictions_rejects_missing_confidence():
    with pytest.raises(RptUnavailable):
        parse_predictions(rpt_body("LATE", None))


def test_config_from_env():
    assert config_from_env({}) is None
    assert config_from_env({"SAP_RPT_API_TOKEN": "t"}).mode == "playground"
    env = {"AICORE_AUTH_URL": "a", "AICORE_CLIENT_ID": "b", "AICORE_CLIENT_SECRET": "c",
           "AICORE_BASE_URL": "d", "SAP_RPT_DEPLOYMENT_ID": "e", "SAP_RPT_API_TOKEN": "t"}
    assert config_from_env(env).mode == "aicore"
    assert config_from_env({**env, "SAP_RPT_MODE": "playground"}).mode == "playground"


def test_aicore_call_uses_token_resource_group_and_deployment_url():
    session = FakeSession(FakeResponse(body=rpt_body("LATE", 0.7)))
    client = RptClient(AICORE, session=session)
    assert client.predict_late(CONTEXT, QUERY) == {"9": pytest.approx(0.7)}
    url, kwargs = session.calls[-1]
    assert url == "https://api.example/v2/inference/deployments/d123/predict"
    assert kwargs["headers"] == {"Authorization": "Bearer tok", "AI-Resource-Group": "rg"}
    target = kwargs["json"]["prediction_config"]["target_columns"][0]
    assert target == {"name": "LATE", "prediction_placeholder": "[PREDICT]",
                      "task_type": "classification"}
    client.predict_late(CONTEXT, QUERY)
    assert sum(1 for u, _ in session.calls if u.endswith("/oauth/token")) == 1   # token cached


def test_http_error_and_missing_credentials_raise_unavailable():
    waits = []
    with pytest.raises(RptUnavailable, match="HTTP 503 after 3 tries"):
        RptClient(AICORE, session=FakeSession(FakeResponse(503)),
                  sleep=waits.append).predict_late(CONTEXT, QUERY)
    assert waits == [2.0, 5.0]
    with pytest.raises(RptUnavailable, match="HTTP 400$"):     # a bad request is not retried
        RptClient(AICORE, session=FakeSession(FakeResponse(400)),
                  sleep=waits.append).predict_late(CONTEXT, QUERY)
    assert waits == [2.0, 5.0]
    with pytest.raises(RptUnavailable, match="not configured"):
        RptClient(config=None, session=FakeSession(FakeResponse())).predict_late(CONTEXT, QUERY)


class FlakySession(FakeSession):
    """Raises the queued errors (or returns the queued responses) first, then answers."""

    def __init__(self, predict_response, failures):
        super().__init__(predict_response)
        self.failures = list(failures)

    def post(self, url, **kwargs):
        if self.failures:
            self.calls.append((url, kwargs))
            failure = self.failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return failure
        return super().post(url, **kwargs)


def test_lost_connection_on_the_token_request_is_retried():
    import requests
    waits = []
    session = FlakySession(FakeResponse(body=rpt_body("LATE", 0.7)),
                           [requests.ConnectionError("reset"), FakeResponse(502)])
    client = RptClient(AICORE, session=session, sleep=waits.append)
    assert client.predict_late(CONTEXT, QUERY) == {"9": pytest.approx(0.7)}
    assert waits == [2.0, 5.0]
    assert client.retries == ["SAP AI Core token request: ConnectionError, tried again",
                              "SAP AI Core token request: HTTP 502, tried again"]


def test_connection_lost_three_times_or_read_timeout_gives_up():
    import requests
    waits = []
    session = FlakySession(FakeResponse(), [requests.ConnectionError("down")] * 3)
    with pytest.raises(RptUnavailable, match="token request failed: ConnectionError after 3 tries"):
        RptClient(AICORE, session=session, sleep=waits.append).predict_late(CONTEXT, QUERY)
    assert waits == [2.0, 5.0]

    waits.clear()     # a read timeout reached SAP-RPT: no second 30 s wait (NFR-1)
    session = FlakySession(FakeResponse(), [requests.ReadTimeout("slow")])
    with pytest.raises(RptUnavailable, match="token request failed: ReadTimeout$"):
        RptClient(AICORE, session=session, sleep=waits.append).predict_late(CONTEXT, QUERY)
    assert waits == []


def test_tool_reports_the_retries_of_its_own_call():
    import requests
    adapter = CsvAdapter()
    rows = adapter.rpt_rows(("predict",))[:1]
    session = FlakySession(FakeResponse(body=rpt_body("LATE", 0.9, ebeln=rows[0]["EBELN"])),
                           [requests.ConnectionError("reset")])
    client = RptClient(AICORE, session=session, sleep=lambda seconds: None)
    result = predict_delay_risk(rows, adapter=adapter, client=client)
    assert result["fallback"] is False
    assert result["retries"] == ["SAP AI Core token request: ConnectionError, tried again"]
    assert predict_delay_risk(rows, adapter=adapter, client=client)["retries"] == []


def test_tool_uses_rpt_when_available():
    adapter = CsvAdapter()
    rows = adapter.rpt_rows(("predict",))[:1]
    session = FakeSession(FakeResponse(body=rpt_body("LATE", 0.9, ebeln=rows[0]["EBELN"])))
    result = predict_delay_risk(rows, adapter=adapter, client=RptClient(AICORE, session=session))
    assert result["fallback"] is False and result["model_id"] == "sap-rpt-1.6"
    assert result["predictions"][0]["P_LATE"] == 0.9
    sent = session.calls[-1][1]["json"]["rows"]
    assert len(sent) == 340 + 74 + 1          # train and test as context, holdout_recent left out
    assert sent[-1]["LATE"] == "[PREDICT]"


def test_tool_falls_back_to_posterior_and_says_so():
    adapter = CsvAdapter()
    rows = adapter.rpt_rows(("predict",))
    client = RptClient(config=None, session=FakeSession(FakeResponse()))
    result = predict_delay_risk(rows, adapter=adapter, client=client)
    assert result["fallback"] is True and result["model_id"] == "beta-posterior-fallback"
    assert "not configured" in result["fallback_reason"]
    first = result["predictions"][0]
    expected = posterior(r["LATE"] for r in adapter.receipts(first["LIFNR"])).p_late
    assert first["P_LATE"] == round(expected, 4)
