"""Static checks the agent must pass before a write: valid schema/template JSON, no hardcoded content."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser

SCHEMA_RE = re.compile(r"{%-?\s*schema\s*-?%}(.*?){%-?\s*endschema\s*-?%}", re.S)
# Blocks whose contents are code, not visible copy.
CODE_BLOCKS = re.compile(
    r"{%-?\s*(style|stylesheet|javascript|comment)\s*-?%}.*?{%-?\s*end\1\s*-?%}"
    r"|<(style|script)\b.*?</\2\s*>"
    r"|<svg\b.*?</svg\s*>",
    re.S | re.I,
)
LIQUID = re.compile(r"{{.*?}}|{%.*?%}", re.S)
CONTENT_ATTRS = {"src", "srcset", "href", "alt", "title", "placeholder", "aria-label", "content", "poster"}
LITERAL_URL = re.compile(r"https?://|//[\w.-]+\.\w+/|cdn/shop/", re.I)


MARK = "\u2063"  # stands in for a Liquid tag/output, so `alt="{{ x }}"` isn't a literal


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.problems: list[str] = []

    def handle_data(self, data):
        text = data.strip()
        if re.search(r"[A-Za-z0-9]", text.replace(MARK, "")):
            self.problems.append(f"hardcoded text: {text[:60]!r}")

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            literal = (v or "").replace(MARK, "").strip()
            if k in CONTENT_ATTRS and literal:
                if k in ("alt", "title", "placeholder", "aria-label", "content") or LITERAL_URL.search(literal):
                    self.problems.append(f"hardcoded {k}= on <{tag}>: {v[:60]!r}")


def split_schema(liquid: str) -> tuple[str, dict]:
    m = SCHEMA_RE.search(liquid)
    if not m:
        raise ValueError("no {% schema %} block")
    return liquid[: m.start()] + liquid[m.end():], json.loads(m.group(1))


def check_section(liquid: str) -> list[str]:
    """Problems that would make content non-editable. Empty list = clean."""
    try:
        markup, schema = split_schema(liquid)
    except (ValueError, json.JSONDecodeError) as e:
        return [f"schema: {e}"]
    problems = _check_schema(schema)
    # Liquid tags become a neutral placeholder so `alt="{{ x }}"` doesn't count as a literal.
    stripped = LIQUID.sub("⁣", CODE_BLOCKS.sub("", markup))
    c = _Collector()
    c.feed(stripped)
    problems += [p for p in c.problems if "⁣" not in p]
    if LITERAL_URL.search(LIQUID.sub("", CODE_BLOCKS.sub("", markup))):
        problems.append("literal URL in markup (put it in a setting)")
    return problems


def _check_schema(schema: dict) -> list[str]:
    problems, seen = [], set()
    for s in schema.get("settings", []):
        if "id" in s and s["id"] in seen:
            problems.append(f"duplicate setting id {s['id']}")
        seen.add(s.get("id"))
    for b in schema.get("blocks", []):
        ids = [s.get("id") for s in b.get("settings", []) if "id" in s]
        if len(ids) != len(set(ids)):
            problems.append(f"duplicate setting id in block {b.get('type')}")
    return problems


def check_template(template_json: str, section_type: str) -> list[str]:
    try:
        t = json.loads(template_json)
    except json.JSONDecodeError as e:
        return [f"template JSON: {e}"]
    sections = t.get("sections", {})
    types = [s.get("type") for s in sections.values()]
    if types != [section_type]:
        return [f"template must contain exactly one section of type {section_type!r}, got {types}"]
    if t.get("order") != list(sections):
        return ["template 'order' must list the section key"]
    return []


def coverage(values: str, wanted: list[str]) -> list[str]:
    """Which wanted strings (source texts / image URLs) never appear in the written values."""
    return [w for w in wanted if w not in values]
