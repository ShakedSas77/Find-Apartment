"""
=== Yad2 scraper ===

Second listing source alongside the Facebook-groups scraper in apartment_bot.py.
Yad2 search pages sit behind Radware bot-protection (confirmed: a plain HTTP
fetch returns a "Verifying your browser before proceeding..." challenge, not
listing content) — this module owns everything Yad2-DOM-specific (navigation,
pagination, challenge handling, its own anti-bot pacing).

Everything past "have a (url, text, post_date) tuple" is deliberately NOT
reimplemented here — it's imported from apartment_bot.py, which already
validated those pieces take no FB-specific input:
`analyze_post_with_llm`, `_evaluate_post_data`, `_build_row`,
`process_candidate_listing` (LLM parse -> evaluate -> build row -> queue),
`_replay_text_prefilters`, `_apply_stealth`, `_CHROME_LAUNCH_ARGS`.

apartment_bot.py must not import this module at top level (this module
imports back from apartment_bot) — import it lazily from the CLI dispatcher.
"""

import storage
from apartment_bot import (
    analyze_post_with_llm, _evaluate_post_data, _build_row,
    process_candidate_listing, _replay_text_prefilters,
    _apply_stealth, _CHROME_LAUNCH_ARGS, _safe_print, _with_retries,
    _sheet_lock, _checkpoint_lock, _resume_event, GmapsQuotaHalted, BIDI_RE,
)


class Yad2ChallengeAbort(Exception):
    """Raised when a Radware "Verifying your browser..." challenge doesn't
    self-clear within the configured wait — mirrors HeadlessCheckpointAbort's
    role for the FB checkpoint flow (apartment_bot.py)."""
    pass
