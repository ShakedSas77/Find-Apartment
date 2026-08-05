"""
=== LLM orchestration (Gemini chain / Ollama dual-path) ===

analyze_post_with_llm() is the public entry point: schema-validated JSON
extraction from Hebrew post text. Gemini is tried through an ordered model
chain (GEMINI_MODELS in config.py) — a model-level error (quota/not-found,
or GEMINI_MAX_CONSECUTIVE_ERRORS transient errors) advances to the next
entry rather than dropping straight to Ollama, since an outage is usually
model-specific. Local Ollama is the last resort, once the whole chain is
exhausted. One retry on schema validation failure. Everything else here is
private plumbing.
"""
import json
import os
import subprocess
import sys
import threading
import time

import ollama
import requests
from google import genai
from google.genai import types
from pydantic import BaseModel
from typing import Optional

import env
from config import GEMINI_MODELS, GEMINI_MAX_CONSECUTIVE_ERRORS
from prompts import get_apartment_prompt_improved
from core.util import _safe_print, _with_retries
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

# Index into GEMINI_MODELS of the model currently being tried. Advances on a model-level
# failure instead of setting GEMINI_EXHAUSTED outright — see _advance_gemini_model().
_gemini_model_idx = 0

# ─── Concurrency primitives (groups scan in parallel tabs) ────────────────────────
_gemini_lock = threading.Lock()

_ollama_lock = threading.Lock()

_gemini_rate_lock = threading.Lock()

_last_gemini_call = 0.0

_OLLAMA_BASE = os.environ.get("LLM_BASE_URL", "http://localhost:11434")
_ollama_verified = False

def _ensure_ollama_running():
    """Starts local Ollama if the Gemini-fallback path needs it and it isn't up yet. Checked once per process."""
    global _ollama_verified
    if _ollama_verified:
        return
    try:
        if requests.get(f"{_OLLAMA_BASE}/api/tags", timeout=3).status_code == 200:
            _ollama_verified = True
            return
    except Exception:
        pass
    _safe_print("\n    Ollama not running — starting it for the Gemini fallback...")
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception as e:
        _safe_print(f"\n    WARNING: could not start Ollama automatically ({e}). Fallback will fail until it's running.")
        return
    for _ in range(10):
        time.sleep(1)
        try:
            if requests.get(f"{_OLLAMA_BASE}/api/tags", timeout=3).status_code == 200:
                _ollama_verified = True
                _safe_print("\n    Ollama is up.")
                return
        except Exception:
            continue
    _safe_print("\n    WARNING: Ollama did not become reachable within 10s.")

def _advance_gemini_model():
    """Moves the chain pointer to the next model, resetting its error count. Runs
    Gemini fully exhausted (-> Ollama) once the pointer passes the last entry.
    Caller must hold _gemini_lock."""
    global _gemini_model_idx, GEMINI_ERROR_COUNT, GEMINI_EXHAUSTED
    _gemini_model_idx += 1
    GEMINI_ERROR_COUNT = 0
    if _gemini_model_idx >= len(GEMINI_MODELS):
        GEMINI_EXHAUSTED = True

def _get_llm_raw_result(prompt: str) -> dict | None:
    """
    Runs a single LLM parsing attempt: walks the Gemini model chain (up to 2
    models per post, so one malformed post can't burn every model's error
    budget), falling back to Ollama once the chain is exhausted or both
    per-post attempts fail. Returns a raw dict, before schema validation —
    validation happens at the analyze_post_with_llm level so it applies
    identically to both paths.
    """
    global GEMINI_EXHAUSTED, GEMINI_ERROR_COUNT, _last_gemini_call

    if GEMINI_EXHAUSTED:
        _safe_print("[Local Ollama] ")
    else:
        models_tried = 0
        while not GEMINI_EXHAUSTED and models_tried < 2:
            with _gemini_lock:
                model = GEMINI_MODELS[_gemini_model_idx]
            models_tried += 1

            with _gemini_rate_lock:
                now = time.time()
                elapsed = now - _last_gemini_call
                if elapsed < 4.0:
                    time.sleep(4.0 - elapsed)
                _last_gemini_call = time.time()

            try:
                response = get_gemini_client().models.generate_content(
                    model=model,
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
                    _safe_print(f"\n    Gemini quota exhausted on '{model}'. Advancing to next model...")
                    with _gemini_lock:
                        _advance_gemini_model()
                elif "404" in error_msg or "NOT_FOUND" in error_msg:
                    _safe_print(f"\n    WARNING: Gemini model '{model}' not found ({error_msg}).")
                    _safe_print("    Advancing to next model — remove the dead entry from GEMINI_MODELS in config.py.")
                    with _gemini_lock:
                        _advance_gemini_model()
                else:
                    with _gemini_lock:
                        GEMINI_ERROR_COUNT += 1
                        current_count = GEMINI_ERROR_COUNT
                    _safe_print(f"\n    WARNING: Gemini error on '{model}' ({error_msg}). Falling back to Ollama for this post...")
                    if current_count >= GEMINI_MAX_CONSECUTIVE_ERRORS:
                        _safe_print(f"\n    {current_count} consecutive errors on '{model}'. Advancing to next model...")
                        with _gemini_lock:
                            _advance_gemini_model()
                    else:
                        break

    try:
        _ensure_ollama_running()
        with _ollama_lock:
            ollama_response = _with_retries(
                lambda: ollama.chat(
                    model='qwen2.5:7b',
                    messages=[{'role': 'user', 'content': prompt}],
                    format=ApartmentData.model_json_schema(),  # forces schema-compliant decoding — no manual JSON repair needed anymore
                    options={'temperature': 0, 'num_ctx': 4096},
                    keep_alive='10m',
                ),
                attempts=3,
                base_delay=2.0,
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
