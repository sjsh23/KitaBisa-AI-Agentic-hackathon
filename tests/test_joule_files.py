"""The Joule capability in ui/joule/ matches the API (no Joule CLI on the dev machine, so this
stands in for `joule lint` on what the API depends on): every file parses, every scenario
targets a function that exists, every slot copies the user's words and asks when missing,
every function calls a real API route on GESIT_API and shows SUMMARY as Markdown."""
from pathlib import Path

import pytest
import yaml
from fastapi.routing import APIRoute

from integrations.api.app import create_app

CAPABILITY = Path(__file__).resolve().parent.parent / "ui" / "joule" / "gesit_capability"
SCENARIOS = sorted((CAPABILITY / "scenarios").glob("*.yaml"))
FUNCTIONS = {p.stem: p for p in (CAPABILITY / "functions").glob("*.yaml")}


def load(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def routes(tmp_path_factory):
    import os
    os.environ.setdefault("GESIT_API_KEY", "lint-only")
    app = create_app(load_env=False, warm_up=False)
    return {(m, r.path) for r in app.routes if isinstance(r, APIRoute) for m in r.methods}


def test_every_scenario_targets_an_existing_function():
    assert len(SCENARIOS) == 8
    for path in SCENARIOS:
        scenario = load(path)
        assert scenario["description"]
        assert scenario["target"] == {"name": scenario["target"]["name"], "type": "function"}
        assert scenario["target"]["name"] in FUNCTIONS, path.name


def test_every_slot_copies_the_users_words_and_asks():
    for path in SCENARIOS:
        for slot in load(path).get("slots", []):
            text = slot["description"].lower()
            assert "ask" in text, (path.name, slot["name"])
            assert "exactly" in text or "word for word" in text, (path.name, slot["name"])


def test_every_function_calls_a_real_route_and_shows_summary(routes):
    for name, path in FUNCTIONS.items():
        actions = load(path)["action_groups"][0]["actions"]
        request, *messages = actions
        assert request["type"] == "api-request" and request["system_alias"] == "GESIT_API"
        route = request["path"].replace("<? ebeln ?>", "{ebeln}")
        assert (request["method"], route) in routes, name
        assert messages[0]["message"] == {"type": "text", "markdown": True,
                                          "content": "<? apiResponse.body.SUMMARY ?>"}
        assert all(m["type"] == "message" and m["message"]["markdown"] is True for m in messages)


def test_run_shows_summary_table_and_plan():
    actions = load(FUNCTIONS["run_scenario_function"])["action_groups"][0]["actions"]
    assert [a["message"]["content"] for a in actions[1:]] == [
        "<? apiResponse.body.SUMMARY ?>", "<? apiResponse.body.TABLE_MD ?>",
        "<? apiResponse.body.PLAN_MD ?>"]


def test_buyer_and_manager_approve_send_different_actors():
    buyer = load(FUNCTIONS["approve_po_buyer_function"])["action_groups"][0]["actions"][0]
    manager = load(FUNCTIONS["approve_po_function"])["action_groups"][0]["actions"][0]
    assert '"as": "BUYER"' in buyer["body"] and '"as": "MANAGER01"' in manager["body"]
