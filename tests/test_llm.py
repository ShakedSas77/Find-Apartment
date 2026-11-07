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
        self.configs = []
        self.models = self

    def generate_content(self, model, contents, config):
        self.models_called.append(model)
        self.configs.append(config)
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


def test_daily_quota_error_moves_the_next_attempt_to_the_next_model(monkeypatch):
    client = _use_client(monkeypatch, [
        Exception("429 RESOURCE_EXHAUSTED. quotaId: GenerateRequestsPerDayPerProjectPerModel"), {"rooms": 3.0}])

    llm._get_llm_raw_result("prompt")

    assert client.models_called == llm.GEMINI_MODELS[:2]


def test_per_minute_rate_limit_is_retried_on_the_same_model(monkeypatch):
    client = _use_client(monkeypatch, [
        Exception("429 RESOURCE_EXHAUSTED. quotaId: GenerateRequestsPerMinutePerProjectPerModel"), {"rooms": 3.0}])

    llm._get_llm_raw_result("prompt")

    assert client.models_called == [llm.GEMINI_MODELS[0]] * 2


def test_unnamed_rate_limit_is_retried_on_the_same_model(monkeypatch):
    client = _use_client(monkeypatch, [Exception("429 RESOURCE_EXHAUSTED"), {"rooms": 3.0}])

    llm._get_llm_raw_result("prompt")

    assert client.models_called == [llm.GEMINI_MODELS[0]] * 2


def test_rate_limit_that_keeps_failing_advances_the_chain(monkeypatch):
    _use_client(monkeypatch, [Exception("429 RESOURCE_EXHAUSTED")] * (llm.GEMINI_RATE_LIMIT_RETRIES + 1) + [{"rooms": 3.0}])

    llm._get_llm_raw_result("prompt")

    assert llm._gemini_model_idx == 1


def test_retry_delay_follows_googles_own_hint():
    assert llm._retry_delay_s('429 ... "retryDelay": "12s" ...', attempt=0) == 12.0


def test_retry_delay_is_capped():
    assert llm._retry_delay_s("Please retry in 300.5s.", attempt=0) == llm.GEMINI_RETRY_MAX_SLEEP_S


def test_gemini_extraction_runs_at_temperature_zero(monkeypatch):
    client = _use_client(monkeypatch, [{"rooms": 3.0}])

    llm._get_llm_raw_result("prompt")

    assert client.configs[0].temperature == 0


def test_the_model_that_answered_is_recorded(monkeypatch):
    _use_client(monkeypatch, [Exception("404 NOT_FOUND"), {"rooms": 3.0, "price": 6000}])

    llm.analyze_post_with_llm("להשכרה 3 חדרים")

    assert llm.last_model_used() == llm.GEMINI_MODELS[1]


def test_an_ollama_answer_is_recorded_as_ollama(monkeypatch):
    monkeypatch.setattr(llm, "GEMINI_EXHAUSTED", True)
    monkeypatch.setattr(llm, "_ensure_ollama_running", lambda: None)
    monkeypatch.setattr(llm.ollama, "chat", lambda **kwargs: {"message": {"content": '{"rooms": 3.0}'}})

    llm.analyze_post_with_llm("להשכרה 3 חדרים")

    assert llm.last_model_used() == f"ollama:{llm.OLLAMA_MODEL}"
