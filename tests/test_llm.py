"""
Regression tests for core.llm's Gemini model-chain bookkeeping. Only the Gemini
client (network) and time.sleep (clock) are faked.
"""
import json

import pytest

import core.llm as llm


class _FakeResponse:
    parsed = None

    def __init__(self, payload: dict):
        self.text = json.dumps(payload)


class _FakeClient:
    """Plays back one outcome per generate_content call: an Exception is raised, a dict is returned as the response."""

    def __init__(self, outcomes: list):
        self._outcomes = list(outcomes)
        self.models_called = []
        self.models = self

    def generate_content(self, model, contents, config):
        self.models_called.append(model)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)


@pytest.fixture(autouse=True)
def fresh_chain_state(monkeypatch):
    monkeypatch.setattr(llm, "_gemini_model_idx", 0)
    monkeypatch.setattr(llm, "GEMINI_EXHAUSTED", False)
    monkeypatch.setattr(llm, "GEMINI_ERROR_COUNT", 0)
    monkeypatch.setattr(llm, "_last_gemini_call", 0.0)
    monkeypatch.setattr(llm.time, "sleep", lambda _seconds: None)


def _use_client(monkeypatch, outcomes: list) -> _FakeClient:
    client = _FakeClient(outcomes)
    monkeypatch.setattr(llm, "get_gemini_client", lambda: client)
    return client


def test_concurrent_failures_of_one_model_advance_the_chain_once():
    failed_model = llm.GEMINI_MODELS[0]

    llm._advance_gemini_model(failed_model)
    llm._advance_gemini_model(failed_model)

    assert llm._gemini_model_idx == 1


def test_transient_503_is_retried_on_the_same_model(monkeypatch):
    client = _use_client(monkeypatch, [Exception("503 UNAVAILABLE. high demand"), {"rooms": 3.0}])

    result = llm._get_llm_raw_result("prompt")

    assert (result, client.models_called) == ({"rooms": 3.0}, [llm.GEMINI_MODELS[0]] * 2)


def test_quota_error_moves_the_next_attempt_to_the_next_model(monkeypatch):
    client = _use_client(monkeypatch, [Exception("429 RESOURCE_EXHAUSTED"), {"rooms": 3.0}])

    llm._get_llm_raw_result("prompt")

    assert client.models_called == llm.GEMINI_MODELS[:2]
