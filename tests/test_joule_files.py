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
    assert len(SCENARIOS) == 11
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


def texts(messages):
    """The text messages of a function; the quick reply buttons after them are left out."""
    return [m for m in messages if m["message"]["type"] == "text"]


def test_every_function_calls_a_real_route_and_shows_summary(routes):
    for name, path in FUNCTIONS.items():
        actions = load(path)["action_groups"][0]["actions"]
        request, *messages = actions
        assert request["type"] == "api-request" and request["system_alias"] == "GESIT_API"
        route = request["path"].replace("<? ebeln ?>", "{ebeln}")
        assert (request["method"], route) in routes, name
        first = messages[0]["message"]   # SUMMARY first, as text or as the buttons' title
        assert first == {"type": "text", "markdown": True,
                         "content": "<? apiResponse.body.SUMMARY ?>"} or (
            first["type"] == "quickReplies"
            and first["content"]["title"] == "<? apiResponse.body.SUMMARY ?>"), name
        assert all(m["type"] == "message" for m in messages)
        assert all(m["message"]["markdown"] is True for m in texts(messages))
        buttons = [m for m in messages if m["message"]["type"] == "quickReplies"]
        assert messages[:len(texts(messages))] == texts(messages)   # buttons come last
        for m in buttons:
            assert all(b["title"] and b["value"] for b in m["message"]["content"]["buttons"])


def test_run_shows_summary_table_and_plan():
    for name in ("run_scenario_function", "run_all_function"):
        actions = load(FUNCTIONS[name])["action_groups"][0]["actions"]
        assert [a["message"]["content"] for a in texts(actions[1:])] == [
            "<? apiResponse.body.SUMMARY ?>", "<? apiResponse.body.TABLE_MD ?>",
            "<? apiResponse.body.PLAN_MD ?>"], name


def test_one_scenario_and_all_are_separate():
    """B1: the one-scenario slot never takes all; all has its own scenario without a slot."""
    one = load(CAPABILITY / "scenarios" / "run_scenario_scenario.yaml")
    [slot] = one["slots"]
    assert "one of S1, S2 or S3" in slot["description"] and "Never use all" in slot["description"]
    every = load(CAPABILITY / "scenarios" / "run_all_scenario.yaml")
    assert "slots" not in every and every["target"]["name"] == "run_all_function"
    body = load(FUNCTIONS["run_all_function"])["action_groups"][0]["actions"][0]["body"]
    assert '"scenario": "all"' in body


def test_status_and_evaluation_scenarios():
    status = load(CAPABILITY / "scenarios" / "status_scenario.yaml")
    assert status["target"]["name"] == "status_function"
    actions = load(FUNCTIONS["evaluation_function"])["action_groups"][0]["actions"]
    assert actions[0]["path"] == "/evaluation"
    assert [a["message"]["content"] for a in texts(actions[1:])] == [
        "<? apiResponse.body.SUMMARY ?>", "<? apiResponse.body.TABLE_MD ?>"]


def test_buyer_and_manager_approve_send_different_actors():
    buyer = load(FUNCTIONS["approve_po_buyer_function"])["action_groups"][0]["actions"][0]
    manager = load(FUNCTIONS["approve_po_function"])["action_groups"][0]["actions"][0]
    assert '"as": "BUYER"' in buyer["body"] and '"as": "MANAGER01"' in manager["body"]
