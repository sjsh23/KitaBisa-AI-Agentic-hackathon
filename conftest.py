"""Puts the repository root on sys.path so tests can import adapters, scoring, tools, agents.

The tests written before the reliability blend expect SAP-RPT-only P(late), so the test
session runs with GESIT_RELIABILITY_BLEND=0. Tests of the blend switch it on themselves
(`monkeypatch.setenv("GESIT_RELIABILITY_BLEND", "1")` or `use_blend=True`).
"""
import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def reliability_blend_off_by_default():
    before = os.environ.get("GESIT_RELIABILITY_BLEND")
    os.environ["GESIT_RELIABILITY_BLEND"] = "0"
    yield
    if before is None:
        os.environ.pop("GESIT_RELIABILITY_BLEND", None)
    else:
        os.environ["GESIT_RELIABILITY_BLEND"] = before
