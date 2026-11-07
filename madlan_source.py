"""
=== Madlan listing source ===

A second listing source next to the Facebook groups: Madlan's internal GraphQL
API (POST https://www.madlan.co.il/api2). Madlan's HTML pages sit behind
PerimeterX bot protection, but the JSON API is gated only by a visitor token —
the same "protection guards the pages, not the data API" split that blocked our
Playwright Yad2 attempt (reverted 2026-07-26). Query shape from
cxt9/find-apartments (scripts/madlan-api.mjs).

Listings arrive structured, so they skip the LLM: each one is mapped to the
LLM's ApartmentData shape and fed to process_candidate_listing(data=...), which
runs the same filters, dedupe, distance, sheet row and Telegram alert as a
Facebook post. Never raises into the caller — a Madlan outage, a rotated token
or a schema change costs this source's listings for the run, nothing else.
"""
import threading
from datetime import datetime, timedelta, timezone

import requests

import env
import storage
from config import MADLAN_LOCATION_DOC_IDS, MADLAN_PAGE_LIMIT, MAX_POST_AGE_DAYS, MIN_ROOMS, MAX_ROOMS
from core.errors import GmapsQuotaHalted
from core.evaluate import process_candidate_listing, flush_pending_listings
from core.util import _safe_print

_API_URL = "https://www.madlan.co.il/api2"
_LISTING_URL = "https://www.madlan.co.il/listings/{id}"
_SEARCH_PAGE_URL = "https://www.madlan.co.il/for-rent/{doc_id}"
_REQUEST_TIMEOUT_S = 30

_QUERY = """
query searchPoi($dealType: String, $roomsRange: [Float], $locationDocId: String,
                $poiTypes: [PoiType], $offset: Int, $limit: Int) {
  searchPoiV2(dealType: $dealType, roomsRange: $roomsRange, locationDocId: $locationDocId,
              poiTypes: $poiTypes, offset: $offset, limit: $limit) {
    total
    poi {
      id
      ... on Bulletin {
        address price beds area floor firstTimeSeen rentalBrokerFee
        addressDetails { city streetName streetNumber neighbourhood }
      }
    }
  }
}
"""


def run_madlan(sheet, seen_urls: set, live: bool) -> dict:
    """Fetches every configured location and queues matches; returns the same stats shape as a FB group scan."""
    stats = {"added": 0, "checkpoint_hit": False, "posts_seen": 0, "prefiltered": 0, "llm_parsed": 0}
    for doc_id in MADLAN_LOCATION_DOC_IDS:
        label = f"Madlan {doc_id}"
        try:
            items = fetch_madlan_listings(doc_id)
        except Exception as e:
            _safe_print(f"WARNING: [{label}] Madlan API request failed, skipping this location: {e}")
            continue
        _safe_print(f"[{label}] {len(items)} listing(s) returned.")
        try:
            _process_location(sheet, seen_urls, live, doc_id, label, items, stats)
        except GmapsQuotaHalted:
            _safe_print(f"    [{label}] Stopping: Google Maps monthly cap reached (GMAPS_ON_CAP='halt').")
            break
    return stats


def fetch_madlan_listings(doc_id: str) -> list[dict]:
    """One searchPoiV2 call for rentals in the configured room range. Raises on an HTTP or GraphQL error."""
    headers = {
        "content-type": "application/json",
        "origin": "https://www.madlan.co.il",
        "referer": _SEARCH_PAGE_URL.format(doc_id=doc_id),
        "x-requested-with": "XMLHttpRequest",
        "x-source": "web",
    }
    token = env.get_madlan_token()
    if token:
        headers["authorization"] = f"Bearer {token}"
    payload = {
        "operationName": "searchPoi",
        "query": _QUERY,
        "variables": {
            "dealType": "unitRent",
            "roomsRange": [MIN_ROOMS, MAX_ROOMS],
            "locationDocId": doc_id,
            "poiTypes": ["bulletin"],
            "offset": 0,
            "limit": MADLAN_PAGE_LIMIT,
        },
    }
    response = requests.post(_API_URL, json=payload, headers=headers, timeout=_REQUEST_TIMEOUT_S)
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"GraphQL errors: {str(body['errors'])[:300]}")
    result = (body.get("data") or {}).get("searchPoiV2")
    if result is None:
        raise RuntimeError("response has no searchPoiV2 data")
    return result.get("poi") or []


def _process_location(sheet, seen_urls: set, live: bool, doc_id: str, label: str, items: list[dict], stats: dict):
    pending_rows, pending_seen_urls, pending_records, pending_telegram = [], set(), [], []
    local_lock = threading.Lock()
    group_url = _SEARCH_PAGE_URL.format(doc_id=doc_id)
    for item in items:
        if not item.get("id"):
            continue
        url = _LISTING_URL.format(id=item["id"])
        stats["posts_seen"] += 1
        if url in seen_urls or storage.should_skip(url) or not _is_recent(item.get("firstTimeSeen")):
            stats["prefiltered"] += 1
            continue
        data = to_apartment_data(item)
        process_candidate_listing(url, listing_text(item, data), _post_date(item.get("firstTimeSeen")), group_url,
                                  label, live, seen_urls, pending_rows, pending_seen_urls, pending_records, stats,
                                  local_lock, pending_telegram, data=data)
    flush_pending_listings(sheet, seen_urls, pending_rows, pending_seen_urls, pending_records,
                           pending_telegram, stats, live, label)


def to_apartment_data(item: dict) -> dict:
    """A Madlan bulletin in the LLM's ApartmentData shape. Fields Madlan doesn't provide stay None (blank in the sheet)."""
    details = item.get("addressDetails") or {}
    street = " ".join(str(part) for part in (details.get("streetName"), details.get("streetNumber")) if part)
    city = details.get("city") or ""
    address = ", ".join(part for part in (street, city) if part) if street else (item.get("address") or "")
    broker_fee = item.get("rentalBrokerFee")
    return {
        "rooms": item.get("beds"),
        "price": item.get("price"),
        "arnona": None,
        "vaad": None,
        "shelter": None,
        "parking": None,
        "entry_date": None,
        "floor": str(item["floor"]) if item.get("floor") is not None else None,
        "elevator": None,
        "is_agent": bool(broker_fee) if broker_fee is not None else None,
        "address": address,
    }


def listing_text(item: dict, data: dict) -> str:
    """
    Hebrew stand-in for a post's text: stored as the listing's raw_text and read by the
    text-based checks (_detect_agent's תיווך signal, the focus-area check). States the broker
    status explicitly so _detect_agent agrees with Madlan's own rentalBrokerFee flag.
    """
    lines = ["דירה להשכרה (מדלן)"]
    if data["address"]:
        lines.append(data["address"])
    if data["rooms"] is not None:
        lines.append(f"{data['rooms']} חדרים")
    if data["price"] is not None:
        lines.append(f"{data['price']} ₪ לחודש")
    if data["floor"] is not None:
        lines.append(f"קומה {data['floor']}")
    if item.get("area"):
        lines.append(f"{item['area']} מ\"ר")
    neighbourhood = (item.get("addressDetails") or {}).get("neighbourhood")
    if neighbourhood:
        lines.append(f"שכונת {neighbourhood}")
    if data["is_agent"] is True:
        lines.append("תיווך")
    elif data["is_agent"] is False:
        lines.append("ללא תיווך")
    return "\n".join(lines)


def _parse_first_seen(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_recent(first_seen: str | None) -> bool:
    """Within MAX_POST_AGE_DAYS — same cutoff as Facebook posts. An unparseable/missing date is kept."""
    parsed = _parse_first_seen(first_seen)
    if parsed is None:
        return True
    return parsed >= datetime.now(timezone.utc) - timedelta(days=MAX_POST_AGE_DAYS)


def _post_date(first_seen: str | None) -> str:
    """DD/MM, the sheet's post-date format; blank when Madlan gave no date."""
    parsed = _parse_first_seen(first_seen)
    return parsed.astimezone().strftime("%d/%m") if parsed else ""
