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
# A quoted literal inside a Liquid tag/output that reads like copy: has a space or starts uppercase
# ('Best Seller', ' OFF', "Don't miss out"). Keys like 'two-pack', 'swap', 'icon-check' don't. `| t` keys are fine.
LIQUID_LITERAL = re.compile(r"'([^']*)'|\"([^\"]*)\"")
CSS_REMOTE_URL = re.compile(r"url\(\s*['\"]?\s*(?:https?:)?//", re.I)
TEXT_ATTRS = {"alt", "title", "placeholder", "aria-label", "content", "value"}
URL_ATTRS = {"src", "srcset", "href", "poster"}
LITERAL_URL = re.compile(r"https?://|//[\w.-]+\.\w+/|cdn/shop/", re.I)
MARK = "⁣"  # stands in for a Liquid tag/output, so `alt="{{ x }}"` isn't a literal
VISIBLE_VALUE_TYPES = {"submit", "button", "reset", "text", "email", "search", "tel", "url"}
# Literal arguments that are markup, not copy: `image_tag: sizes: '(min-width: 750px) 50vw'`, `date: '%b %d'`, …
NON_COPY_ARG = re.compile(r"\b(sizes|date|class|id|format|font_display|loading|widths|media|rel|target)\s*:\s*$")
CSS_ESCAPE = re.compile(r"\\[0-9a-fA-F]{1,6}\s?")
def decode_css_escapes(s: str) -> str:
    """'\\42\\75\\79' -> 'Buy', so copy spelled as escapes is still copy; '\\2713' -> '✓' stays a symbol."""
    def char(m):
        code = int(m.group(0).strip()[1:], 16)
        return chr(code) if code <= 0x10FFFF else ""
    return CSS_ESCAPE.sub(char, s)


CSS_CONTENT = re.compile(r"(?<![\w-])content\s*:\s*([^;}]*)")  # the whole value: "a" "b", attr(x), …
CSS_STRING = re.compile(r"(['\"])(.*?)\1", re.S)


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
            # value= is visible only on buttons; on radios/hidden inputs it is a key ("two-pack", "2").
            itype = (dict(attrs).get("type") or "text").lower()
            visible_value = tag == "button" or (tag == "input" and itype in VISIBLE_VALUE_TYPES)
            if (k in TEXT_ATTRS and (k != "value" or (visible_value and re.search(r"[A-Za-z]", literal)))) or (
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
    if not isinstance(schema, dict):
        return ["schema: must be a JSON object"]
    problems = _check_schema(schema)
    for tag in LIQUID.finditer(CODE_BLOCKS.sub("", markup)):
        for m in LIQUID_LITERAL.finditer(tag.group(0)):
            lit = m.group(1) if m.group(1) is not None else m.group(2)
            translated = re.match(r"\s*\|\s*t\b", tag.group(0)[m.end():])
            markup_arg = NON_COPY_ARG.search(tag.group(0)[:m.start()]) or re.match(r"{%-?\s*form\b", tag.group(0))
            if LITERAL_URL.search(lit):  # {{ 'https://…/x.jpg' }} is a hardcoded image/link, not a key
                problems.append(f"literal URL in a Liquid string: {lit[:60]!r}")
            elif (re.search(r"[A-Za-z]", lit) and (" " in lit or lit.lstrip()[:1].isupper())
                    and not translated and not markup_arg):
                problems.append(f"hardcoded text in a Liquid string: {lit[:60]!r}")
    for m in STYLE_BLOCKS.finditer(markup):
        css = LIQUID.sub("", m.group(2) or m.group(3) or "")
        if CSS_REMOTE_URL.search(css):
            problems.append("remote url(...) in CSS (put the image in a setting)")
        for decl in CSS_CONTENT.finditer(css):
            value = decl.group(1)
            if "attr(" in value:  # the copy lives in a markup attribute the editor can't reach
                problems.append(f"CSS content uses attr(): {value.strip()[:60]!r}")
            problems += [f"hardcoded text in CSS content: {m.group(2)[:60]!r}" for m in CSS_STRING.finditer(value)
                         if re.search(r"[A-Za-z0-9]", decode_css_escapes(m.group(2)))]  # "\2713" ✓ is an icon
    c = _Collector()
    c.feed(LIQUID.sub(MARK, CODE_BLOCKS.sub("", markup)))
    problems += c.problems
    if LITERAL_URL.search(LIQUID.sub("", CODE_BLOCKS.sub("", markup))):
        problems.append("literal URL in markup (put it in a setting)")
    return problems


def _check_schema(schema: dict) -> list[str]:
    settings, blocks = schema.get("settings", []), schema.get("blocks", [])
    if not (isinstance(settings, list) and all(isinstance(x, dict) for x in settings)
            and isinstance(blocks, list) and all(isinstance(b, dict) and isinstance(b.get("settings", []), list)
                                                 and all(isinstance(x, dict) for x in b.get("settings", []))
                                                 for b in blocks)):
        return ["schema: settings/blocks must be lists of objects"]
    problems, seen = [], set()
    ids = [s["id"] for s in settings if "id" in s] + [
        x["id"] for b in blocks for x in b.get("settings", []) if "id" in x]
    if not all(isinstance(i, str) for i in ids):
        return ["schema: every setting id must be a string"]
    for s in settings:
        if "id" in s and s["id"] in seen:
            problems.append(f"duplicate setting id {s['id']}")
        seen.add(s.get("id"))
    for b in blocks:
        ids = [s.get("id") for s in b.get("settings", []) if "id" in s]
        if len(ids) != len(set(ids)):
            problems.append(f"duplicate setting id in block {b.get('type')}")
    return problems


def check_template(template_json: str, section_type: str, key: str) -> list[str]:
    try:
        t = json.loads(template_json)
    except json.JSONDecodeError as e:
        return [f"template JSON: {e}"]
    sections = t.get("sections") if isinstance(t, dict) else None
    if not isinstance(sections, dict) or not all(isinstance(v, dict) for v in sections.values()):
        return ["template must be an object with a 'sections' object of section objects"]
    if list(sections) != [key] or sections[key].get("type") != section_type:
        found = {k: v.get("type") for k, v in sections.items()}
        return [f"template must contain exactly one section, key {key!r}, type {section_type!r}; got {found}"]
    if t.get("order") != [key]:
        return [f"template 'order' must be [{key!r}]"]
    return []
