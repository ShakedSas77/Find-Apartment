"""
=== LLM extraction accuracy check ===

Scores the LLM's raw extraction of the fixture posts in test_posts/ against
hand-written answers in test_posts/expected.json, field by field — the
evidence for (or against) a prompt or model change, instead of eyeballing a
few posts. `python apartment_bot.py --eval` runs it: one real LLM call per
fixture, through the same model chain the scraper uses.

Only fields a post states unambiguously are in its expected answer; a field
left out isn't scored for that post.
"""
import json
from pathlib import Path

from core.normalize import _parse_floor

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "test_posts"


def score_extraction(expected: dict, actual: dict | None) -> dict[str, bool]:
    """field -> correct? for every field in `expected`. A missing extraction scores every field wrong."""
    actual = actual or {}
    return {field: _matches(field, want, actual.get(field)) for field, want in expected.items()}


def run_eval(ollama_model: str | None = None) -> dict[str, list[bool]]:
    """
    Runs every fixture through the LLM, prints each mismatch, the per-field accuracy and the average time
    per post, returns the scores. ollama_model skips the Gemini chain and scores that local model instead
    — how a local fallback candidate is compared without editing config.
    """
    import time
    import core.llm as llm

    if ollama_model:
        llm.OLLAMA_MODEL = ollama_model
        llm.GEMINI_EXHAUSTED = True
    expected_by_file = json.loads((FIXTURES_DIR / "expected.json").read_text(encoding="utf-8"))
    by_field: dict[str, list[bool]] = {}
    started = time.perf_counter()
    for name, expected in expected_by_file.items():
        text = (FIXTURES_DIR / name).read_text(encoding="utf-8")
        actual = llm.analyze_post_with_llm(text)
        last_model_used = llm.last_model_used
        scores = score_extraction(expected, actual)
        for field, ok in scores.items():
            by_field.setdefault(field, []).append(ok)
            if not ok:
                print(f"  MISS {name} [{last_model_used()}] {field}: expected {expected[field]!r}, got {(actual or {}).get(field)!r}")
    total = [ok for oks in by_field.values() for ok in oks]
    print("\nPer-field accuracy:")
    for field, oks in by_field.items():
        print(f"  {field:<10} {sum(oks)}/{len(oks)}")
    print(f"Overall: {sum(total)}/{len(total)} ({100 * sum(total) / max(len(total), 1):.0f}%), "
          f"{(time.perf_counter() - started) / max(len(expected_by_file), 1):.1f}s per post")
    return by_field


def _matches(field: str, want, got) -> bool:
    if field == "address":
        return bool(got) and want in got
    if field == "floor":
        return _parse_floor(str(got) if got is not None else "") == want
    if isinstance(want, bool):
        return got is want
    try:
        return float(got) == float(want)
    except (TypeError, ValueError):
        return False
