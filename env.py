"""
=== Environment / secret access ===

Thin accessors over .env, with NO validation at import time — any module can
import this (and be imported itself) without a populated .env, which is what
makes the rest of the codebase unit-testable. Validation is explicit: call
require_env() from a CLI entrypoint, never at module scope.
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def get_gemini_api_key() -> str | None:
    return os.getenv("GEMINI_API_KEY")


def get_gmaps_api_key() -> str | None:
    return os.getenv("GMAPS_API_KEY")


def get_sheet_id() -> str | None:
    return os.getenv("SHEET_ID")


def get_alerts_imap_credentials() -> tuple[str | None, str | None, str]:
    """(user, app password, folder) for the read-only alert-email mailbox; folder defaults to INBOX."""
    return os.getenv("ALERTS_IMAP_USER"), os.getenv("ALERTS_IMAP_PASSWORD"), os.getenv("ALERTS_IMAP_FOLDER") or "INBOX"


_REQUIRED = ("GEMINI_API_KEY", "GMAPS_API_KEY", "SHEET_ID")


def require_env() -> None:
    """Exits the process if any required key is missing — the same check that
    used to run at apartment_bot.py import time, now explicit and CLI-only."""
    if any(not os.getenv(k) for k in _REQUIRED):
        print("ERROR: Missing GEMINI_API_KEY, GMAPS_API_KEY, or SHEET_ID in .env file")
        sys.exit(1)
