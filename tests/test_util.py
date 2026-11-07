"""
core.util.timed(): per-phase wall-time accumulation used by the end-of-run timing line.
"""
import time

import pytest

from core import util


@pytest.fixture(autouse=True)
def clean_phase_totals():
    util._phase_seconds.clear()
    yield
    util._phase_seconds.clear()


def test_timed_accumulates_across_blocks_of_the_same_phase():
    with util.timed("llm"):
        time.sleep(0.02)
    with util.timed("llm"):
        time.sleep(0.02)

    assert util.get_phase_seconds()["llm"] >= 0.04


def test_timed_keeps_phases_separate():
    with util.timed("maps"):
        pass

    assert "llm" not in util.get_phase_seconds()


def test_timed_records_time_even_when_the_block_raises():
    with pytest.raises(ValueError):
        with util.timed("sheets"):
            raise ValueError("boom")

    assert "sheets" in util.get_phase_seconds()
