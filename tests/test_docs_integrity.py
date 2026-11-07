"""
CLAUDE.md's "Domain notes (skills)" table is the only thing that points a session
at the deep per-module notes in .claude/skills/ — a skill missing from the table is
a note nobody loads, and a table row with no skill is a dead pointer.
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SKILLS_DIR = _ROOT / ".claude" / "skills"


def _listed_skills() -> set[str]:
    text = (_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    section = text.split("## Domain notes (skills)", 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"^\| `([a-z0-9-]+)` \|", section, re.MULTILINE))


def _skill_dirs() -> set[str]:
    return {p.parent.name for p in _SKILLS_DIR.glob("*/SKILL.md")}


def test_every_skill_is_listed_in_claude_md():
    assert _skill_dirs() - _listed_skills() == set()


def test_every_listed_skill_exists():
    assert _listed_skills() - _skill_dirs() == set()


def test_every_skill_names_itself_in_its_frontmatter():
    mismatched = {
        p.parent.name for p in _SKILLS_DIR.glob("*/SKILL.md")
        if f"\nname: {p.parent.name}\n" not in p.read_text(encoding="utf-8").replace("\r\n", "\n")
    }
    assert mismatched == set()
