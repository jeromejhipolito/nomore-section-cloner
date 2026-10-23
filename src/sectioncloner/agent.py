"""The Claude Agent SDK agent: scrape -> write one Liquid section -> compare at phone size -> fix -> finish.

Loop guards and the finish gate live here in Python, not in the prompt.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import re
import traceback
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from claude_agent_sdk import (
    ClaudeSDKError,
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

from playwright.sync_api import Error as PlaywrightError

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
SETTLE_TRIES = 3
CONFIRM_GRACE_S = 20  # wait before re-reading when an earlier write was never confirmed
READ_RETRY_S = 2
EXIT_PARTIAL = 3  # 0 done · 1 never finished · 2 config/usage error · 3 finished partial · 4 Shopify down (CLI)
FAILURE_CAP = 3  # crashed tool calls that are forgiven before crashes start costing budget
MAX_BUDGET_USD = 4.5  # hard stop below the $5 a run is allowed to cost

TOOL_NAMES = ["get_source", "write_files", "edit_file", "compare", "finish"]
ALLOWED = [f"mcp__{SERVER}__{n}" for n in TOOL_NAMES]
MAX_TURNS = 60  # above the charged budgets (38) plus room for free refusals
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
    good: dict[str, str] | None = None  # files of the last write that was read back AND compared
    finished: bool = False
    failures: int = 0  # tool calls that raised; the first FAILURE_CAP are not charged to a tool's budget
    cost: float | None = None  # set by the SDK's ResultMessage; None = the run ended before reporting it
    calls: Counter = field(default_factory=Counter)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def current(self) -> bool:
        """Live files == the last written, read-back AND compared version."""
        return self.good is not None and self.written_at == self.mutations == self.compared_at

    def done(self) -> bool:
        return self.finished and self.current()


def _ok(data, images: list[Path] = ()) -> dict:
    content = [{"type": "text", "text": data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)}]
    for p in images:
        content.append({"type": "image", "data": base64.b64encode(p.read_bytes()).decode(), "mimeType": "image/jpeg"})
    return {"content": content}


def _err(msg: str) -> dict:
    return {"content": [{"type": "text", "text": f"ERROR: {msg}"}], "is_error": True}


LAST_COMPARE_USED = ("all compares are used: a change now could never be checked, so the files stay as last "
                     "compared; call finish and list what still differs")


# The ONE place that says what the agent can still do. finish, the height gate and the write/edit refusals all
# read these, so the budget rules cannot drift apart (iterations 2-5 of review kept finding such drift).
def no_compares_left(run: "Run") -> bool:
    return run.calls["compare"] >= BUDGET["compare"]


def can_still_write(run: "Run") -> bool:
    """Could a write that gets compared afterwards still happen?"""
    has_source = run.source is not None or run.calls["get_source"] < BUDGET["get_source"]
    writes_left = run.calls["write_files"] < BUDGET["write_files"] or (
        bool(run.files) and run.calls["edit_file"] < BUDGET["edit_file"])
    return has_source and writes_left and not no_compares_left(run)


def can_reach_current(run: "Run") -> bool:
    """Is there still a budget path to a written AND compared current state?"""
    if run.written_at != run.mutations:
        return can_still_write(run)
    return not no_compares_left(run)


def is_transient(e: Exception) -> bool:
    """Infra trouble (network, Shopify 5xx/throttling, a browser that died) — not the agent's mistake."""
    return isinstance(e, (ConnectionError, TimeoutError, PlaywrightError, shopify.ShopifyTransientError))


async def stored_matches(run: "Run", files: dict[str, str], tries: int = 3) -> bool:
    """Does the store hold exactly `files`? Re-reads a few times: a just-finished write can take a moment to show."""
    for attempt in range(tries):
        back = await asyncio.to_thread(shopify.read_files, run.admin, run.theme_id, [SECTION_FILE, TEMPLATE_FILE])
        try:  # Shopify reformats template JSON and adds a comment header, so compare parsed values
            same_template = (shopify.parse_template(back.get(TEMPLATE_FILE, "null"))
                             == json.loads(files[TEMPLATE_FILE]))
        except (json.JSONDecodeError, ValueError):
            same_template = False
        if back.get(SECTION_FILE) == files[SECTION_FILE] and same_template:
            return True
        if attempt < tries - 1:
            await asyncio.sleep(READ_RETRY_S)
    return False


async def upload_verified(run: "Run", files: dict[str, str], final: bool = False) -> bool:
    """Upload section + template in ONE call, then read both back. True only if the store holds exactly `files`."""
    await asyncio.to_thread(shopify.upsert_files, run.admin, run.theme_id, files, final)  # serialised in shopify.py
    return await stored_matches(run, files)


async def settle(run: "Run") -> None:
    """However the session ended, leave the store on the last compared version (`good`), checked by reading it.

    1. Close the store to ordinary writes and wait out any write still running in a thread (a cancelled tool call).
    2. If no write is unconfirmed and the store already holds `good`, nothing is written.
    3. Otherwise upload `good` as the final write and read it back; if some earlier write was never confirmed,
       wait CONFIRM_GRACE_S and read again, re-uploading if it landed meanwhile.
    Limit: a write Shopify accepted but had not applied by then can still land after this process exits."""
    await asyncio.to_thread(shopify.close_writes)  # always: even with nothing to restore, nothing may land later
    if run.good is None or run.theme_id is None:
        return
    risky = shopify.unconfirmed_writes > 0
    if risky:
        run.log(f"{shopify.unconfirmed_writes} earlier write(s) were never confirmed by Shopify: re-uploading and "
                f"re-checking after {CONFIRM_GRACE_S}s")
    for attempt in range(SETTLE_TRIES):
        try:
            if not risky and await stored_matches(run, run.good, tries=1):
                return _mark_settled(run)
            if await upload_verified(run, run.good, final=True):
                if risky:
                    await asyncio.sleep(CONFIRM_GRACE_S)
                    if not await stored_matches(run, run.good, tries=1):
                        run.log("an earlier write landed after the restore; restoring again")
                        if attempt == SETTLE_TRIES - 1 and await upload_verified(run, run.good, final=True):
                            return _mark_settled(run)  # last chance: put it back once more, no further grace
                        continue
                return _mark_settled(run)
        except Exception as e:  # noqa: BLE001 - never let the clean-up crash the run; the exit code tells the truth
            run.log(f"settle attempt {attempt + 1}/{SETTLE_TRIES} failed: {e}")
    if run.current():  # the store could not be confirmed to hold `good`, so the run can't claim it does
        run.mutations += 1
    run.log("could not confirm the store holds the last compared version")


def _mark_settled(run: "Run") -> None:
    if not run.current():
        run.mutations += 1
        run.files, run.written_at, run.compared_at = run.good, run.mutations, run.mutations
        run.log("the store is back on the last compared version (read back and verified)")


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
                    if is_transient(e) and run.failures < FAILURE_CAP:  # infra trouble is not a used attempt
                        run.failures += 1
                        run.calls[name] -= 1
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
    if not await upload_verified(run, files):
        return _err("read-back differs from what was written")
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
        if no_compares_left(run):
            return _pre(LAST_COMPARE_USED)
        return await _write(run, {SECTION_FILE: args["section_liquid"], TEMPLATE_FILE: args["template_json"]})

    @tool("edit_file", "Replace one exact, unique snippet in the last written file and re-write it. "
          "file is 'section' or 'template'.", {"file": str, "old": str, "new": str})
    @guarded(run, "edit_file")
    async def edit_file(args):
        if not run.files:
            return _pre("nothing written yet; use write_files")
        if no_compares_left(run):
            return _pre(LAST_COMPARE_USED)
        name = {"section": SECTION_FILE, "template": TEMPLATE_FILE}.get(args["file"])
        if name is None:
            return _pre("file must be 'section' or 'template'")
        n = run.files[name].count(args["old"])
        if n != 1:
            return _err(f"'old' must match exactly once in {name}; it matched {n} times")
        return await _write(run, run.files | {name: run.files[name].replace(args["old"], args["new"])})

    @tool("compare", "Screenshot the rebuilt section on the live store; returns source|rebuild pairs per band.", {})
    @guarded(run, "compare")
    async def compare(args):
        if run.written_at is None or run.written_at != run.mutations or run.product_url is None:
            return _pre("nothing successfully written since the last change")
        if shopify.unconfirmed_writes and not await stored_matches(run, run.files, tries=1):
            # an earlier unconfirmed write landed after this write's read-back: don't screenshot the wrong version
            run.mutations += 1
            return _pre("the store no longer holds your last write (an earlier write landed late); "
                        "write it again with write_files, then compare")
        i = run.calls["compare"]
        # ?_v=<mutations> skips any storefront page cache that still holds the previous render
        url = f"{run.product_url}?_v={run.mutations}"
        shot = await asyncio.to_thread(browser.shoot_rebuild, url, run.password, run.out, f"rebuild{i}")
        pairs = await asyncio.to_thread(browser.side_by_side, run.source.shot, shot, run.out, f"cmp{i}")
        run.compared_at = run.mutations
        run.good = run.files  # the version settle() falls back to, however the session ends
        run.rebuild_height = shot.height
        left = BUDGET["compare"] - i
        run.log(f"compare #{i}: source {run.source.shot.height}px vs rebuild {shot.height}px, {left} left")
        return _ok(f"Original {run.source.shot.height}px tall, yours {shot.height}px. Left=original, right=yours. "
                   f"{left} compares left.", pairs)

    @tool("finish", "End the run with a short report.", {"report": str})
    @guarded(run, "finish")
    async def finish(args):
        if not run.current():
            if can_reach_current(run):
                return _pre("nothing written yet: start with get_source, then write_files" if run.written_at is None
                            else "the last change was not written successfully" if run.written_at != run.mutations
                            else "compare after your last change first")
            # No budget path back to a current state: accept; settle() then puts the last compared version live.
            run.finished = True
            run.log(f"finish accepted; the last compared version will be put back live: {args['report']}")
            return _ok({"accepted": True, "restoring_last_compared_version": run.good is not None})
        off = height_off(run)
        if off is not None and abs(off) > HEIGHT_TOLERANCE and can_still_write(run):
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
    try:
        await _converse(run, prompt, model)
    except ClaudeSDKError as e:  # expected: the SDK raises after a budget/turn-cap result; state decides the exit
        run.log(f"session ended with {type(e).__name__}: {str(e)[:300]}")
    except Exception:  # noqa: BLE001 - a bug: keep the traceback, still settle the store, still exit from state
        run.log("session crashed:\n" + traceback.format_exc())
    finally:  # budget cap, turn cap, SDK error or a normal finish: the store ends on the last compared version
        await settle(run)
    return exit_code(run)


async def _converse(run: Run, prompt: str, model: str) -> None:
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


def exit_code(run: Run) -> int:
    """0 done · 1 nothing built or never finished · 3 finished but the store is not on the last compared version."""
    if run.done():
        return 0
    if run.finished and run.good is not None:
        run.log(f"finished, but the live files are not the last compared version -> exit {EXIT_PARTIAL}")
        return EXIT_PARTIAL
    run.log("run ended without an accepted finish on a compared version -> exit 1")
    return 1
