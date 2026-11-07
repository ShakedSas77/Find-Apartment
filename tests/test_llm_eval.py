"""
Tests for llm_eval's field scoring — the comparison every prompt/model change is judged by.
"""
import json

from core.llm_eval import FIXTURES_DIR, score_extraction


def test_matching_extraction_scores_every_field_right():
    expected = {"rooms": 3, "price": 6200, "is_agent": False, "floor": 3, "address": "טור הברושים"}
    actual = {"rooms": 3.0, "price": 6200, "is_agent": False, "floor": "3 מתוך 5", "address": "טור הברושים 5, רמת גן"}

    assert all(score_extraction(expected, actual).values())


def test_old_price_picked_instead_of_new_one_is_a_miss():
    assert score_extraction({"price": 7200}, {"price": 6500}) == {"price": False}


def test_ground_floor_word_matches_floor_zero():
    assert score_extraction({"floor": 0}, {"floor": "קרקע"}) == {"floor": True}


def test_unknown_boolean_is_not_counted_as_false():
    assert score_extraction({"elevator": False}, {"elevator": None}) == {"elevator": False}


def test_failed_extraction_scores_every_field_wrong():
    assert score_extraction({"rooms": 3, "price": 5900}, None) == {"rooms": False, "price": False}


def test_every_fixture_has_an_expected_answer():
    expected = json.loads((FIXTURES_DIR / "expected.json").read_text(encoding="utf-8"))

    assert {p.name for p in FIXTURES_DIR.glob("*.txt")} == set(expected)


def test_eval_with_an_ollama_model_scores_that_local_model(monkeypatch):
    import core.llm as llm
    from core import llm_eval

    used = []
    monkeypatch.setattr(llm, "GEMINI_EXHAUSTED", False)
    monkeypatch.setattr(llm, "OLLAMA_MODEL", llm.OLLAMA_MODEL)
    monkeypatch.setattr(llm, "_ensure_ollama_running", lambda: None)
    monkeypatch.setattr(llm.ollama, "chat", lambda **kwargs: used.append(kwargs["model"]) or {"message": {"content": '{"rooms": 3}'}})

    llm_eval.run_eval(ollama_model="aya-expanse:8b")

    assert set(used) == {"aya-expanse:8b"}
