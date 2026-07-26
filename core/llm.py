"""
=== LLM orchestration (Gemini/Ollama dual-path) ===

analyze_post_with_llm() is the public entry point: schema-validated JSON
extraction from Hebrew post text, Gemini primary with automatic permanent
fallback to local Ollama on quota/model errors, one retry on validation
failure. Everything else here is private plumbing.
"""
import json
import threading
import time

import ollama
from google import genai
from google.genai import types
from pydantic import BaseModel
from typing import Optional

import env
from config import GEMINI_MODEL, GEMINI_MAX_CONSECUTIVE_ERRORS
from prompts import get_apartment_prompt_improved
from core.util import _safe_print
from core.normalize import _clean_post_for_llm


class ApartmentData(BaseModel):
    """JSON schema forced onto Gemini's response (response_schema) — eliminates parsing failures on the Gemini path."""
    rooms: Optional[float] = None
    price: Optional[int] = None
    arnona: Optional[str] = None
    vaad: Optional[str] = None
    shelter: Optional[bool] = None
    parking: Optional[str] = None
    entry_date: Optional[str] = None
    floor: Optional[str] = None
    elevator: Optional[bool] = None
    is_agent: Optional[bool] = None
    address: Optional[str] = None

# --- API clients ---
# Lazy + double-checked-locked, not constructed at import time: importing this
# module (e.g. to unit-test a pure function) must work with no .env at all.
# Double-checking is required, not decorative — _scan_group_page runs a
# 5-worker ThreadPoolExecutor over process_candidate_listing, so first touch
# of either client is genuinely concurrent.
_gemini_client = None

_gemini_client_lock = threading.Lock()

def get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        with _gemini_client_lock:
            if _gemini_client is None:
                _gemini_client = genai.Client(api_key=env.get_gemini_api_key())
    return _gemini_client

GEMINI_EXHAUSTED = False

GEMINI_ERROR_COUNT = 0

# ─── Concurrency primitives (groups scan in parallel tabs) ────────────────────────
_gemini_lock = threading.Lock()

_ollama_lock = threading.Lock()

_gemini_rate_lock = threading.Lock()

_last_gemini_call = 0.0

def _get_llm_raw_result(prompt: str) -> dict | None:
    """
    Runs a single LLM parsing attempt (Gemini if not exhausted, otherwise
    Ollama). Returns a raw dict, before schema validation — validation happens
    at the analyze_post_with_llm level so it applies identically to both paths.
    """
    global GEMINI_EXHAUSTED, GEMINI_ERROR_COUNT, _last_gemini_call
    if not GEMINI_EXHAUSTED:
        with _gemini_rate_lock:
            now = time.time()
            elapsed = now - _last_gemini_call
            if elapsed < 4.0:
                time.sleep(4.0 - elapsed)
            _last_gemini_call = time.time()

        try:
            response = get_gemini_client().models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ApartmentData,
                ),
            )
            result = response.parsed.model_dump() if response.parsed else json.loads(response.text)
            with _gemini_lock:
                GEMINI_ERROR_COUNT = 0
            return result
        except Exception as gemini_err:
            error_msg = str(gemini_err)
            if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
                _safe_print("\n    Gemini quota exhausted. Switching permanently to Ollama...")
                with _gemini_lock:
                    GEMINI_EXHAUSTED = True
            elif "404" in error_msg or "NOT_FOUND" in error_msg:
                _safe_print(f"\n    WARNING: Gemini model '{GEMINI_MODEL}' not found ({error_msg}).")
                _safe_print("    The model is misconfigured or deprecated — update GEMINI_MODEL in config.py")
                with _gemini_lock:
                    GEMINI_EXHAUSTED = True
            else:
                with _gemini_lock:
                    GEMINI_ERROR_COUNT += 1
                    current_count = GEMINI_ERROR_COUNT
                _safe_print(f"\n    WARNING: Gemini error ({error_msg}). Falling back to Ollama...")
                if current_count >= GEMINI_MAX_CONSECUTIVE_ERRORS:
                    _safe_print(f"\n    {current_count} consecutive Gemini errors. Switching permanently to Ollama...")
                    with _gemini_lock:
                        GEMINI_EXHAUSTED = True
    else:
        _safe_print("[Local Ollama] ")

    try:
        with _ollama_lock:
            ollama_response = ollama.chat(
                model='qwen2.5:7b',
                messages=[{'role': 'user', 'content': prompt}],
                format=ApartmentData.model_json_schema(),  # forces schema-compliant decoding — no manual JSON repair needed anymore
                options={'temperature': 0, 'num_ctx': 4096},
                keep_alive='10m',
            )
        return json.loads(ollama_response['message']['content'])
    except Exception as ollama_err:
        _safe_print(f"\n    ERROR: local Ollama analysis failed: {ollama_err}")
        return None

def analyze_post_with_llm(text: str) -> dict | None:
    """
    Two attempts max: each attempt runs the LLM and then validates the output
    against ApartmentData. A raw call failure (LLM/network error) or a schema
    validation failure both count as one attempt; failure on both -> None
    (verdict parse_failed for the caller).
    """
    prompt = get_apartment_prompt_improved(_clean_post_for_llm(text))
    for attempt in range(2):
        raw = _get_llm_raw_result(prompt)
        if raw is None:
            _safe_print(f"    WARNING: LLM call returned no result (attempt {attempt + 1}/2)")
            continue
        try:
            return ApartmentData.model_validate(raw).model_dump()
        except Exception as validation_err:
            _safe_print(f"    WARNING: LLM output failed schema validation (attempt {attempt + 1}/2): {validation_err}")
    return None
