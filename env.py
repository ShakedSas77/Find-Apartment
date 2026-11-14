"""
=== Environment / secret access ===

Thin accessors over .env, with NO validation at import time — any module can
import this (and be imported itself) without a populated .env, which is what
makes the rest of the codebase unit-testable. Validation is explicit: call
require_env() from a CLI entrypoint, never at module scope. Only what the enabled
features (config.py / config_local.py) need is required.
"""
import os
import sys

from dotenv import load_dotenv

import config

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


def missing_env() -> list[str]:
    """What the enabled features still lack, one line each naming the setting that needs it and the fix."""
    missing = []
    if config.LLM_PROVIDER == "gemini" and not os.getenv("GEMINI_API_KEY"):
        missing.append('GEMINI_API_KEY in .env (LLM_PROVIDER = "gemini"): a free key from '
                       'https://aistudio.google.com/app/apikey, or LLM_PROVIDER = "ollama"')
    if config.OUTPUT_BACKEND == "sheets":
        if not os.getenv("SHEET_ID"):
            missing.append('SHEET_ID in .env (OUTPUT_BACKEND = "sheets"), or OUTPUT_BACKEND = "excel"')
        if not os.path.exists(config.CREDENTIALS_FILE):
            missing.append(f'{config.CREDENTIALS_FILE} (OUTPUT_BACKEND = "sheets"): the Google service-account key')
    if config.GMAPS_ENABLED and not os.getenv("GMAPS_API_KEY"):
        missing.append("GMAPS_API_KEY in .env (GMAPS_ENABLED = True), or GMAPS_ENABLED = False for the free estimate")
    if config.TELEGRAM_ENABLED and not (os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")):
        missing.append("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env (TELEGRAM_ENABLED = True), "
                       "or TELEGRAM_ENABLED = False")
    return missing


def require_env() -> None:
    """Exits the process, listing what's missing, when an enabled feature lacks its key or file."""
    missing = missing_env()
    if missing:
        print("ERROR: setup incomplete. Run `python setup_wizard.py`, or add:")
        for line in missing:
            print(f"  - {line}")
        sys.exit(1)
