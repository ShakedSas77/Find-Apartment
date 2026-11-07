"""
=== Passive capture of Facebook's own GraphQL feed responses ===

The logged-in group page loads its feed from POST /api/graphql/ as it scrolls;
those responses already carry each post's id, message text and exact
creation_time as structured data. Listening to them — no extra requests, only
reading what this session receives — can replace the most fragile DOM-reading
parts (relative-date parsing, comment stripping) once proven. Approach from
FaustRen/facebook-graphql-scraper.

OBSERVATIONAL for now: _scan_group_page still takes posts from the DOM and only
prints how many it could match here, and whether the dates agree. The payload
format is undocumented and changes without notice, so parsing is defensive —
anything unrecognized is skipped, never raised.
"""
import json
from datetime import datetime

_GRAPHQL_PATH = "/api/graphql/"
# Subtrees that hold comments/reactions, not the post itself — a comment's text must never be
# taken as the post's message.
_SKIP_KEY_PARTS = ("feedback", "comment")


class GraphqlPostCapture:
    """Buffers a page's GraphQL responses while attached; collect() parses them into posts."""

    def __init__(self, page):
        self._page = page
        self._responses = []
        self._page.on("response", self._on_response)

    def _on_response(self, response):
        if _GRAPHQL_PATH in response.url:
            self._responses.append(response)

    def collect(self) -> dict[str, dict]:
        """post_id -> {"text": str | None, "created": datetime | None} from every response seen so far."""
        posts = {}
        for response in self._responses:
            try:
                body = response.text()
            except Exception:
                continue
            posts.update(parse_graphql_body(body))
        return posts

    def detach(self):
        try:
            self._page.remove_listener("response", self._on_response)
        except Exception:
            pass


def parse_graphql_body(body: str) -> dict[str, dict]:
    """A /api/graphql/ response body (one JSON object per line) -> post_id -> {text, created}."""
    posts = {}
    for line in body.splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        for node in _post_nodes(payload):
            post_id = str(node["post_id"])
            entry = posts.setdefault(post_id, {"text": None, "created": None})
            entry["text"] = entry["text"] or _find_message_text(node)
            created = _find_creation_time(node)
            if entry["created"] is None and created is not None:
                entry["created"] = datetime.fromtimestamp(created)
    return posts


def _post_nodes(obj):
    if isinstance(obj, dict):
        post_id = obj.get("post_id")
        if isinstance(post_id, (str, int)) and str(post_id).isdigit():
            yield obj
        for value in obj.values():
            yield from _post_nodes(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _post_nodes(item)


def _children(node: dict, root: dict):
    # A shared original is its own post_id, so nested posts are skipped along with comment/feedback
    # subtrees.
    for key, value in node.items():
        if any(part in key.lower() for part in _SKIP_KEY_PARTS):
            continue
        if isinstance(value, dict) and value is not root and "post_id" in value:
            continue
        yield value


def _find_message_text(root: dict) -> str | None:
    stack = [root]
    while stack:
        node = stack.pop(0)
        if isinstance(node, dict):
            message = node.get("message")
            if isinstance(message, dict) and isinstance(message.get("text"), str) and message["text"].strip():
                return message["text"]
            stack.extend(_children(node, root))
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _find_creation_time(root: dict) -> int | None:
    stack = [root]
    while stack:
        node = stack.pop(0)
        if isinstance(node, dict):
            created = node.get("creation_time")
            if isinstance(created, int) and created > 1_000_000_000:
                return created
            stack.extend(_children(node, root))
        elif isinstance(node, list):
            stack.extend(node)
    return None


def compare_with_dom(harvested: dict[str, dict], captured: dict[str, dict], infer_date) -> dict:
    """
    How the GraphQL capture lines up with what the DOM scan harvested: matched = DOM posts whose
    URL's post id was captured, dated = of those, how many have both dates, dates_agree = same
    calendar day. infer_date turns the DOM's DD/MM post date into a datetime (or None).
    """
    stats = {"dom_posts": len(harvested), "captured": len(captured), "matched": 0, "with_text": 0, "dated": 0, "dates_agree": 0}
    for url, post in harvested.items():
        entry = captured.get(url.rstrip("/").rsplit("/", 1)[-1])
        if entry is None:
            continue
        stats["matched"] += 1
        if entry["text"]:
            stats["with_text"] += 1
        dom_date = infer_date(post["date"]) if post.get("date") else None
        if dom_date and entry["created"]:
            stats["dated"] += 1
            if dom_date.date() == entry["created"].date():
                stats["dates_agree"] += 1
    return stats
