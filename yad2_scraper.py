"""
=== Yad2 scraper ===

Second listing source alongside the Facebook-groups scraper in fb_scraper.py.
Yad2 search pages sit behind Radware bot-protection (confirmed: a plain HTTP
fetch returns a "Verifying your browser before proceeding..." challenge, not
listing content) — this module owns everything Yad2-DOM-specific (navigation,
pagination, challenge handling, its own anti-bot pacing).

Everything past "have a (url, text, post_date) tuple" is deliberately NOT
reimplemented here — it's imported from the shared core/ modules (the
apartment_bot.py -> core/* extraction validated these pieces take no
FB-specific input): `analyze_post_with_llm` (core/llm.py), `_evaluate_post_data`,
`_build_row`, `process_candidate_listing` (LLM parse -> evaluate -> build row
-> queue), `_replay_text_prefilters` (core/evaluate.py), `_apply_stealth`,
`_CHROME_LAUNCH_ARGS` (browser.py).
"""

import storage
from core.util import _safe_print, _with_retries, _sheet_lock, _checkpoint_lock, _resume_event
from core.errors import GmapsQuotaHalted
from core.normalize import BIDI_RE
from browser import _apply_stealth, _CHROME_LAUNCH_ARGS
from core.llm import analyze_post_with_llm
from core.evaluate import _evaluate_post_data, _build_row, process_candidate_listing, _replay_text_prefilters


class Yad2ChallengeAbort(Exception):
    """Raised when a Radware "Verifying your browser..." challenge doesn't
    self-clear within the configured wait — mirrors HeadlessCheckpointAbort's
    role for the FB checkpoint flow (fb_scraper.py)."""
    pass
