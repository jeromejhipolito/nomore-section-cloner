"""Static checks the agent must pass before a write: valid schema/template JSON, no hardcoded content."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser

SCHEMA_RE = re.compile(r"{%-?\s*schema\s*-?%}(.*?){%-?\s*endschema\s*-?%}", re.S)
STYLE_BLOCKS = re.compile(r"{%-?\s*(style|stylesheet)\s*-?%}(.*?){%-?\s*end\1\s*-?%}|<style\b[^>]*>(.*?)</style\s*>",
                          re.S | re.I)
# Blocks whose contents are code, not visible copy.
CODE_BLOCKS = re.compile(
    r"{%-?\s*(style|stylesheet|javascript|comment)\s*-?%}.*?{%-?\s*end\1\s*-?%}"
    r"|<(style|script)\b.*?</\2\s*>"
    r"|<svg\b.*?</svg\s*>",
    re.S | re.I,
)
LIQUID = re.compile(r"{{.*?}}|{%.*?%}", re.S)
# `{{ 'Buy now' }}` / `{{ "Buy" | upcase }}` is copy typed into the markup; `{{ 'x' | t }}` is a translation key.
QUOTED_OUTPUT = re.compile(r"{{-?\s*(['\"])([^'\"]*[A-Za-z][^'\"]*)\1(?![^}]*\|\s*(?:t|asset_url)\b)[^}]*}}")
CSS_REMOTE_URL = re.compile(r"url\(\s*['\"]?\s*(?:https?:)?//", re.I)
TEXT_ATTRS = {"alt", "title", "placeholder", "aria-label", "content", "value"}
URL_ATTRS = {"src", "srcset", "href", "poster"}
LITERAL_URL = re.compile(r"https?://|//[\w.-]+\.\w+/|cdn/shop/", re.I)
MARK = "⁣"  # stands in for a Liquid tag/output, so `alt="{{ x }}"` isn't a literal


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.problems: list[str] = []

    def handle_data(self, data):
        text = data.strip()
        if re.search(r"[A-Za-z0-9]", text.replace(MARK, "")):
            self.problems.append(f"hardcoded text: {text.replace(MARK, '{{…}}')[:60]!r}")

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            literal = (v or "").replace(MARK, "").strip()
            if not literal:
                continue
            # value="1" (a quantity) is fine; value="Add to cart" is copy.
            if (k in TEXT_ATTRS and (k != "value" or re.search(r"[A-Za-z]", literal))) or (
                    k in URL_ATTRS and LITERAL_URL.search(literal)):
                self.problems.append(f"hardcoded {k}= on <{tag}>: {literal[:60]!r}")


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
    problems += [f"hardcoded text in a Liquid string: {m.group(2)[:60]!r}" for m in QUOTED_OUTPUT.finditer(markup)]
    for m in STYLE_BLOCKS.finditer(markup):
        if CSS_REMOTE_URL.search(LIQUID.sub("", m.group(2) or m.group(3) or "")):
            problems.append("remote url(...) in CSS (put the image in a setting)")
    c = _Collector()
    c.feed(LIQUID.sub(MARK, CODE_BLOCKS.sub("", markup)))
    problems += c.problems
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


def check_template(template_json: str, section_type: str, key: str) -> list[str]:
    try:
        t = json.loads(template_json)
    except json.JSONDecodeError as e:
        return [f"template JSON: {e}"]
    sections = t.get("sections", {})
    if list(sections) != [key] or sections[key].get("type") != section_type:
        found = {k: v.get("type") for k, v in sections.items()}
        return [f"template must contain exactly one section, key {key!r}, type {section_type!r}; got {found}"]
    if t.get("order") != [key]:
        return [f"template 'order' must be [{key!r}]"]
    return []
