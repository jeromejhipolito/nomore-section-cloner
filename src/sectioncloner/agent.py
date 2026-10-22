"""The Claude Agent SDK agent: scrape -> write one Liquid section -> compare at phone size -> fix -> finish.

Loop guards and the finish gate live here in Python, not in the prompt.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    query,
    tool,
)

from . import browser, lint, shopify

SERVER = "builder"
SECTION_TYPE = "nomore-landing"
SECTION_FILE = f"sections/{SECTION_TYPE}.liquid"
TEMPLATE_SUFFIX = "nomore"
TEMPLATE_FILE = f"templates/product.{TEMPLATE_SUFFIX}.json"
TEMPLATE_KEY = browser.TEMPLATE_KEY  # one constant: the screenshot selector is built from it
PRODUCT_HANDLE = "nomore-landing-placeholder"
PRODUCT_TITLE, PRODUCT_PRICE = "NoMore Colds (placeholder)", "49.00"  # AC: a placeholder product is enough
HEIGHT_TOLERANCE = 0.015  # finish needs the rebuild within 1.5% of the original's height (while compares remain)
MAX_BUDGET_USD = 4.5  # hard stop below the $5 a run is allowed to cost

TOOL_NAMES = ["get_source", "write_files", "edit_file", "compare", "finish"]
ALLOWED = [f"mcp__{SERVER}__{n}" for n in TOOL_NAMES]
MAX_TURNS = 40
# Hard per-tool ceilings. `compare` is the visual iteration cap.
BUDGET = {"get_source": 2, "write_files": 4, "edit_file": 25, "compare": 4, "finish": 3}

SYSTEM_PROMPT = f"""You rebuild the first sections of a Shopify landing page as ONE Liquid section, matching the
original as closely as possible on a 390px-wide phone.

Tools, in order: get_source -> write_files -> compare -> (edit_file -> compare)* -> finish.

get_source gives a DOM digest (tags, sizes, computed styles, text, image URLs) and screenshots of the original.

write_files takes two files:
1. `{SECTION_FILE}`: markup + a `{{% style %}}` block (scope every selector under `#shopify-section-{{{{ section.id }}}}`)
   + a `{{% schema %}}` block. Mobile-first CSS; sections render inside the theme's own header/footer.
2. `{TEMPLATE_FILE}`: {{"sections": {{"{TEMPLATE_KEY}": {{"type": "{SECTION_TYPE}", "settings": {{...}},
   "blocks": {{...}}, "block_order": [...]}}}}, "order": ["{TEMPLATE_KEY}"]}}. The real copy and image URLs from
   the source go HERE as setting values, so every one is editable in the theme editor.

Rules (write_files rejects violations):
- NO hardcoded content in the markup. Every visible text, every image, every link, every alt text comes from
  `section.settings.*` or `block.settings.*`. Repeating things (gallery slides, icon tiles, bundle offers, gifts,
  review, bullet points, labels around the hero image) are blocks.
- Images: a `url` setting holding the source CDN URL (keep it exactly as given) plus an optional `image_picker`
  override: `{{% if block.settings.image != blank %}}{{{{ block.settings.image | image_url: width: 800 }}}}{{% else %}}
  {{{{ block.settings.image_url }}}}{{% endif %}}`. Shopify image_picker can't hold an external URL.
- Text: `text`, `inline_richtext` or `richtext` settings (richtext values must be wrapped in <p>/<ul>). Colours
  that matter visually are `color` settings.
- Schema JSON must be valid; setting ids unique per section and per block; text/inline_richtext defaults are
  optional - put real values in the template.
- Add to cart must work: a form posting to /cart/add with
  `{{{{ product.selected_or_first_available_variant.id }}}}`; the chosen bundle sets the quantity. The product is a
  placeholder: show prices/labels from settings, not from the product.
- Fonts: the digest gives each element's computed font-family. When that family is in Shopify's font library
  (e.g. Jost, Poppins, Montserrat, Inter), add a `font_picker` setting (default like "jost_n6"), emit
  `{{{{ section.settings.heading_font | font_face: font_display: 'swap' }}}}` in the style block and use
  `font-family: {{{{ section.settings.heading_font.family }}}}, {{{{ section.settings.heading_font.fallback_families }}}}`.
  Use separate pickers for headings and body text if they differ. Never link font files from the source site.
- Interactivity in plain CSS/vanilla JS only (scroll-snap gallery with dots, radio bundle picker). No libraries.
- Inline SVG icons (stars, check marks, arrows) are fine as markup.

compare renders your section on the live store and returns side-by-side images (left = original, right = yours)
per band. Fix the biggest visual differences first (layout, sizes, spacing, font size/weight, colours, order),
using edit_file for targeted changes. You have {BUDGET['compare']} compares in total. finish is refused while
your section's height is more than {HEIGHT_TOLERANCE:.1%} off the original and you still have compares left.

finish is accepted only when the latest files were written AND compared after the last change. Call it with a
one-paragraph report of what matches and what still differs."""


@dataclass
class Run:
    url: str
    admin: shopify.Admin
    password: str
    out: Path
    sections: int = 3
    log: callable = print
    source: browser.Source | None = None
    files: dict[str, str] = field(default_factory=dict)
    theme_id: str | None = None
    product_url: str | None = None
    mutations: int = 0
    written_at: int | None = None  # mutations value when the last write was read back and matched
    compared_at: int | None = None
    rebuild_height: int | None = None  # from the latest compare
    finished: bool = False
    cost: float | None = None  # set by the SDK's ResultMessage; None = the run ended before reporting it
    calls: Counter = field(default_factory=Counter)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def done(self) -> bool:
        return self.finished and self.written_at == self.mutations == self.compared_at


def _ok(data, images: list[Path] = ()) -> dict:
    content = [{"type": "text", "text": data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)}]
    for p in images:
        content.append({"type": "image", "data": base64.b64encode(p.read_bytes()).decode(), "mimeType": "image/jpeg"})
    return {"content": content}


def _err(msg: str) -> dict:
    return {"content": [{"type": "text", "text": f"ERROR: {msg}"}], "is_error": True}


def fmt_cost(cost: float | None) -> str:
    return "unknown (no result from the SDK)" if cost is None else f"${cost:.4f}"


def _pre(msg: str) -> dict:
    """A precondition refusal: reported to the agent like an error, but not charged to the tool's budget."""
    return _err(msg) | {"_precondition": True}


def guarded(run: Run, name: str):
    def wrap(fn):
        async def inner(args):
            async with run.lock:  # counters too: parallel tool calls must not share a compare index
                run.calls[name] += 1
                if run.finished:
                    return _err("run already finished")
                if run.calls[name] > BUDGET[name]:
                    return _err(f"budget for {name} exhausted ({BUDGET[name]}); call finish")
                try:
                    res = await fn(args)
                except Exception as e:  # noqa: BLE001 - the agent must see every failure as a tool error
                    run.log(f"{name} failed: {e}")
                    return _err(f"{type(e).__name__}: {e}")
                if res.pop("_precondition", False):
                    run.calls[name] -= 1
                return res
        return inner
    return wrap


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip().lower()


def height_off(run: "Run") -> float | None:
    """Rebuild height relative to the source (+ = taller), from the latest compare."""
    if run.rebuild_height is None or run.source is None or not run.source.shot.height:
        return None
    return run.rebuild_height / run.source.shot.height - 1


def _strings(v) -> list[str]:
    if isinstance(v, dict):
        return [s for x in v.values() for s in _strings(x)]
    if isinstance(v, list):
        return [s for x in v for s in _strings(x)]
    return [v] if isinstance(v, str) else []


def content_gaps(source: browser.Source, files: dict[str, str]) -> dict:
    """Source texts / image paths that never made it into the written files (fidelity hint for the agent)."""
    try:  # the template's real values, not its JSON text (which escapes quotes)
        values = _strings(json.loads(files[TEMPLATE_FILE]))
    except (json.JSONDecodeError, KeyError):
        values = []
    blob = _norm(" ".join(values + [files.get(SECTION_FILE, "")]))
    texts = [t for t in dict.fromkeys(source.texts) if len(t) > 2]
    missing_text = [t for t in texts if _norm(t) not in blob]
    paths = {urlparse(u).path for u in source.images if u.startswith("http")}
    missing_img = sorted(p for p in paths if p.lower() not in blob)  # blob is lowercased by _norm
    return {"missing_texts": missing_text[:25], "missing_text_count": len(missing_text),
            "missing_image_paths": missing_img[:15], "missing_image_count": len(missing_img)}


async def _write(run: Run, files: dict[str, str]) -> dict:
    problems = (lint.check_section(files[SECTION_FILE])
                + lint.check_template(files[TEMPLATE_FILE], SECTION_TYPE, TEMPLATE_KEY))
    if problems:
        return _pre("not written; fix these first:\n- " + "\n- ".join(problems[:30]))  # not charged
    if run.theme_id is None:
        run.theme_id = await asyncio.to_thread(shopify.main_theme_id, run.admin)
    run.mutations += 1  # before the write: a partial failure must invalidate the last verify
    # Section first: the template references it, and Shopify validates the reference.
    await asyncio.to_thread(shopify.upsert_files, run.admin, run.theme_id, {SECTION_FILE: files[SECTION_FILE]})
    await asyncio.to_thread(shopify.upsert_files, run.admin, run.theme_id, {TEMPLATE_FILE: files[TEMPLATE_FILE]})
    back = await asyncio.to_thread(shopify.read_files, run.admin, run.theme_id, [SECTION_FILE, TEMPLATE_FILE])
    if back.get(SECTION_FILE) != files[SECTION_FILE]:
        return _err("read-back of the section differs from what was written")
    try:  # Shopify reformats template JSON and adds a comment header, so compare parsed values
        same_template = shopify.parse_template(back.get(TEMPLATE_FILE, "null")) == json.loads(files[TEMPLATE_FILE])
    except (json.JSONDecodeError, ValueError):
        same_template = False
    if not same_template:
        return _err("read-back of the template differs from what was written")
    if run.product_url is None:  # before marking the write done: compare needs a product page to open
        await asyncio.to_thread(shopify.ensure_product, run.admin, PRODUCT_HANDLE, PRODUCT_TITLE, PRODUCT_PRICE,
                                TEMPLATE_SUFFIX)
        run.product_url = f"https://{run.admin.domain}/products/{PRODUCT_HANDLE}"
    run.files = files
    run.written_at = run.mutations
    gaps = content_gaps(run.source, files) if run.source else {}
    run.log(f"wrote + read back {SECTION_FILE} ({len(files[SECTION_FILE])} chars), {TEMPLATE_FILE}; gaps={gaps}")
    return _ok({"written": True, "product_url": run.product_url, "content_gaps": gaps})


def build_tools(run: Run):
    @tool("get_source", "Digest + screenshots of the source page's first sections at 390px.", {})
    @guarded(run, "get_source")
    async def get_source(args):
        if run.source is None:
            run.source = await asyncio.to_thread(browser.scrape, run.url, run.sections, run.out)
            run.log(f"scraped {len(run.source.digest)} chars of digest, {len(run.source.images)} images, "
                    f"{run.source.shot.height}px tall")
        s = run.source
        return _ok(f"Source sections are {s.shot.height}px tall at 390px wide. Images found:\n"
                   + "\n".join(s.images) + "\n\nDOM digest:\n" + s.digest, s.shot.tiles)

    @tool("write_files", f"Write both files ({SECTION_FILE}, {TEMPLATE_FILE}) to the live theme.",
          {"section_liquid": str, "template_json": str})
    @guarded(run, "write_files")
    async def write_files(args):
        if run.source is None:
            return _pre("call get_source first")
        return await _write(run, {SECTION_FILE: args["section_liquid"], TEMPLATE_FILE: args["template_json"]})

    @tool("edit_file", "Replace one exact, unique snippet in the last written file and re-write it. "
          "file is 'section' or 'template'.", {"file": str, "old": str, "new": str})
    @guarded(run, "edit_file")
    async def edit_file(args):
        if not run.files:
            return _pre("nothing written yet; use write_files")
        name = {"section": SECTION_FILE, "template": TEMPLATE_FILE}.get(args["file"])
        if name is None:
            return _err("file must be 'section' or 'template'")
        n = run.files[name].count(args["old"])
        if n != 1:
            return _err(f"'old' must match exactly once in {name}; it matched {n} times")
        return await _write(run, run.files | {name: run.files[name].replace(args["old"], args["new"])})

    @tool("compare", "Screenshot the rebuilt section on the live store; returns source|rebuild pairs per band.", {})
    @guarded(run, "compare")
    async def compare(args):
        if run.written_at is None or run.written_at != run.mutations or run.product_url is None:
            return _pre("nothing successfully written since the last change")
        i = run.calls["compare"]
        shot = await asyncio.to_thread(browser.shoot_rebuild, run.product_url, run.password, run.out, f"rebuild{i}")
        pairs = await asyncio.to_thread(browser.side_by_side, run.source.shot, shot, run.out, f"cmp{i}")
        run.compared_at = run.mutations
        run.rebuild_height = shot.height
        left = BUDGET["compare"] - i
        run.log(f"compare #{i}: source {run.source.shot.height}px vs rebuild {shot.height}px, {left} left")
        return _ok(f"Original {run.source.shot.height}px tall, yours {shot.height}px. Left=original, right=yours. "
                   f"{left} compares left.", pairs)

    @tool("finish", "End the run with a short report.", {"report": str})
    @guarded(run, "finish")
    async def finish(args):
        if run.written_at != run.mutations:
            return _pre("the last change was not written successfully")
        if run.compared_at != run.mutations:
            return _pre("compare after your last change first")
        off = height_off(run)
        can_change = run.calls["edit_file"] < BUDGET["edit_file"] or run.calls["write_files"] < BUDGET["write_files"]
        if (off is not None and abs(off) > HEIGHT_TOLERANCE and can_change
                and run.calls["compare"] < BUDGET["compare"]):
            return _pre(f"your section is {off:+.1%} the height of the original ({run.rebuild_height}px vs "
                        f"{run.source.shot.height}px); use a remaining compare to close the gap first")
        run.finished = True
        run.log(f"finish accepted: {args['report']}")
        return _ok({"accepted": True})

    return [get_source, write_files, edit_file, compare, finish]


def options_for(run: Run, model: str) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={SERVER: create_sdk_mcp_server(SERVER, "1.0.0", build_tools(run))},
        tools=[],  # no built-in Bash/Read/Write: only the 5 tools above exist
        allowed_tools=ALLOWED,
        setting_sources=[],  # don't inherit the developer's ~/.claude settings or hooks
        max_turns=MAX_TURNS,
        model=model,
        max_buffer_size=16 * 1024 * 1024,  # tool results carry screenshots; the 1MB default overflows
        max_budget_usd=MAX_BUDGET_USD,
    )


async def run_agent(run: Run, model: str) -> int:
    prompt = f"Rebuild the first {run.sections} sections of this page as one Liquid section: {run.url}"
    async for msg in query(prompt=prompt, options=options_for(run, model)):
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock) and b.text.strip():
                    run.log(f"agent: {b.text.strip()[:500]}")
                elif isinstance(b, ToolUseBlock):
                    args = {k: (v if len(str(v)) < 80 else f"<{len(str(v))} chars>") for k, v in b.input.items()}
                    run.log(f"-> {b.name.removeprefix(f'mcp__{SERVER}__')}({json.dumps(args)})")
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            for b in msg.content:
                if isinstance(b, ToolResultBlock):
                    parts = b.content if isinstance(b.content, list) else [{"type": "text", "text": b.content}]
                    text = " ".join(c.get("text", "") for c in parts if isinstance(c, dict))
                    imgs = sum(isinstance(c, dict) and c.get("type") == "image" for c in parts)
                    run.log(f"<- {'ERROR ' if b.is_error else ''}{text[:300]}" + (f" [+{imgs} images]" if imgs else ""))
        elif isinstance(msg, ResultMessage):
            run.cost = msg.total_cost_usd
            run.log(f"session end: {msg.subtype}, turns={msg.num_turns}, API cost={fmt_cost(run.cost)}")
    if not run.done():
        run.log("run ended WITHOUT an accepted finish backed by a current write + compare -> exit 1")
    return 0 if run.done() else 1
