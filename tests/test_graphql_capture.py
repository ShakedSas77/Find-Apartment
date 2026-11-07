"""
Tests for parsing Facebook's /api/graphql/ feed responses. The payloads are
synthetic — shaped like the nested story nodes FB returns (post_id at the
story, message/creation_time somewhere below it, comments under feedback) —
since the real format is undocumented; the live run's per-group report is
what confirms it.
"""
import json
from datetime import datetime

from sources.graphql_capture import compare_with_dom, parse_graphql_body

_CREATED = 1762500000


def _story(post_id="111", text="להשכרה 3 חדרים", created=_CREATED, extra=None):
    node = {
        "__typename": "Story",
        "post_id": post_id,
        "comet_sections": {
            "content": {"story": {"message": {"text": text}}},
            "context_layout": {"story": {"metadata": [{"story": {"creation_time": created}}]}},
        },
    }
    node.update(extra or {})
    return node


def _body(*nodes):
    return "\n".join(json.dumps({"data": {"node": n}}) for n in nodes)


def test_post_text_and_exact_creation_time_are_extracted():
    posts = parse_graphql_body(_body(_story()))

    assert posts["111"] == {"text": "להשכרה 3 חדרים", "created": datetime.fromtimestamp(_CREATED)}


def test_comment_text_under_feedback_is_never_taken_as_the_post_message():
    story = _story(text=None, extra={"feedback": {"comments": [{"message": {"text": "מחיר?"}}]}})
    story["comet_sections"]["content"]["story"].pop("message")

    assert parse_graphql_body(_body(story))["111"]["text"] is None


def _shared_post_parse():
    shared = _story(post_id="111", text="שימו לב", extra={"attached_story": _story(post_id="222", text="הדירה המקורית")})
    return parse_graphql_body(_body(shared))


def test_sharers_note_is_the_sharing_posts_text():
    assert _shared_post_parse()["111"]["text"] == "שימו לב"


def test_shared_original_post_keeps_its_own_text():
    assert _shared_post_parse()["222"]["text"] == "הדירה המקורית"


def test_non_json_lines_are_skipped():
    assert parse_graphql_body("for (;;);\n" + _body(_story()))["111"]["text"] == "להשכרה 3 חדרים"


def _dom_comparison():
    captured = parse_graphql_body(_body(_story()))
    day = datetime.fromtimestamp(_CREATED)
    harvested = {"https://www.facebook.com/groups/1/posts/111/": {"date": "dom"}}
    return compare_with_dom(harvested, captured, lambda _: day)


def test_dom_comparison_matches_by_post_id():
    assert _dom_comparison()["matched"] == 1


def test_dom_comparison_checks_the_day():
    assert _dom_comparison()["dates_agree"] == 1
