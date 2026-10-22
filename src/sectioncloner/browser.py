"""Playwright (sync API — always call via asyncio.to_thread from the agent loop).

- scrape(): the first N sections of the source page at phone size -> a compact digest + screenshot tiles
- shoot_rebuild(): the rebuilt section on the target storefront -> screenshot tiles
- side_by_side(): source|rebuild tiles composed into one image per band, for the agent to compare
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

WIDTH, HEIGHT = 390, 844
TILE_H = 900
MIN_TAIL = 200  # a last tile shorter than this is merged into the one before it
JPEG_Q = 70  # PNG tiles of a busy page overflow the SDK message buffer; JPEG is ~5x smaller
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
TEMPLATE_KEY = "nomore_landing"  # the template's section key; Shopify renders id="shopify-section-…__<key>"
REBUILD_SELECTOR = f'[id$="__{TEMPLATE_KEY}"]'

# Walks the DOM of the chosen sections and prints one indented line per meaningful element:
# tag.class [w x h] {styles that matter} "own text" img(src). Wrapper divs with nothing of their own
# are collapsed, hidden nodes skipped, so the digest stays a few thousand tokens.
DIGEST_JS = r"""
([n, maxLines]) => {
  const secs = [...document.querySelectorAll('main > .shopify-section, main > section, main > div')]
    .filter(s => s.offsetHeight > 0).slice(0, n);
  const INHERIT = ['color','font-family','font-size','font-weight','font-style','line-height','text-align',
                   'letter-spacing','text-transform','text-decoration-line'];
  const url = u => { if (!u) return ''; u = u.trim().split(/\s+/)[0]; if (u.startsWith('//')) u = 'https:' + u;
    try { const x = new URL(u, location.href); ['width','height','crop'].forEach(k => x.searchParams.delete(k));
      return x.href; } catch (e) { return u; } };
  const images = new Set(), texts = [];
  const out = [];
  function styles(el, ps) {
    const s = getComputedStyle(el), o = [];
    for (const k of INHERIT) if (!ps || s[k] !== ps[k]) o.push(k + ':' + s[k]);
    const bg = s.backgroundColor; if (bg && bg !== 'rgba(0, 0, 0, 0)') o.push('bg:' + bg);
    if (s.backgroundImage !== 'none') { const m = s.backgroundImage.match(/url\(["']?(.*?)["']?\)/);
      if (m) { images.add(url(m[1])); o.push('bg-img:' + url(m[1])); } else o.push('bg-img:' + s.backgroundImage.slice(0, 120)); }
    for (const k of ['padding','margin','border','border-radius','box-shadow','gap'])
      if (s[k] && !/^(0px|none|normal|0px none.*|)$/.test(s[k]) && !s[k].startsWith('0px none')) o.push(k + ':' + s[k]);
    if (['flex','inline-flex','grid'].includes(s.display))
      o.push(`display:${s.display} dir:${s.flexDirection} justify:${s.justifyContent} align:${s.alignItems}` +
             (s.display === 'grid' ? ' cols:' + s.gridTemplateColumns : ''));
    if (s.position === 'absolute' || s.position === 'fixed') o.push('pos:' + s.position);
    if (s.overflowX === 'auto' || s.overflowX === 'scroll') o.push('scroll-x');
    if (s.opacity !== '1') o.push('opacity:' + s.opacity);
    return [o, s];
  }
  function walk(el, depth, ps) {
    if (out.length >= maxLines) return;
    const s0 = getComputedStyle(el);
    if (s0.display === 'none' || s0.visibility === 'hidden') return;
    const r = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    if (['script','style','noscript','template','link','meta'].includes(tag)) return;
    if (r.width === 0 && r.height === 0 && tag !== 'img') return;
    const [st, s] = styles(el, ps);
    const own = [...el.childNodes].filter(c => c.nodeType === 3).map(c => c.textContent.replace(/\s+/g,' ').trim())
      .filter(Boolean).join(' ');
    if (own) texts.push(own);
    let extra = '';
    if (tag === 'img') { const src = url(el.currentSrc || el.src || el.dataset.src); images.add(src);
      extra = ` img(${src}) alt="${(el.alt || '').slice(0, 80)}"`; }
    if (tag === 'input') extra = ` input[type=${el.type}${el.checked ? ' checked' : ''}]`;
    if (tag === 'svg') extra = ` svg fill=${s.fill} stroke=${s.stroke}`;
    const kids = tag === 'svg' ? [] : [...el.children];
    const boring = !own && !extra && st.length === 0 && kids.length === 1;
    if (!boring) {
      const cls = (typeof el.className === 'string' ? el.className : '').trim().split(/\s+/).slice(0, 2).join('.');
      const off = r.left >= innerWidth || r.right <= 0 ? ' (offscreen)' : '';
      out.push('  '.repeat(depth) + `${tag}${cls ? '.' + cls : ''} [${Math.round(r.width)}x${Math.round(r.height)}]` +
        off + (st.length ? ' {' + st.join('; ') + '}' : '') + (own ? ` "${own}"` : '') + extra);
    }
    for (const k of kids) walk(k, boring ? depth : depth + 1, s);
  }
  const bands = secs.map(sec => { const r = sec.getBoundingClientRect();
    return { id: sec.id, top: r.top + scrollY, height: r.height }; });
  secs.forEach((sec, i) => { out.push(`=== SECTION ${i + 1} (${Math.round(bands[i].height)}px tall) ===`);
    walk(sec, 0, null); });
  return { digest: out.join('\n'), truncated: out.length >= maxLines, images: [...images].filter(Boolean),
           texts, bands };
}
"""


BOTTOM_JS = """n => { const s = [...document.querySelectorAll('main > .shopify-section, main > section, main > div')]
  .filter(e => e.offsetHeight > 0).slice(0, n); const l = s[s.length - 1];
  return l ? l.getBoundingClientRect().bottom + scrollY : 0; }"""


@dataclass
class Shot:
    tiles: list[Path] = field(default_factory=list)
    height: int = 0


@dataclass
class Source:
    url: str
    digest: str
    images: list[str]
    texts: list[str]
    shot: Shot


def _page(p):
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT}, user_agent=UA, is_mobile=True,
                              has_touch=True, device_scale_factor=1)
    return browser, ctx.new_page()


def _settle(page: Page, until: int) -> None:
    """Scroll through the region so lazy images load, then back to the top."""
    for y in range(0, until + HEIGHT, 400):
        page.evaluate("y => scrollTo(0, y)", y)
        page.wait_for_timeout(150)
    page.evaluate("scrollTo(0, 0)")
    # Best effort: analytics pixels keep some pages from ever going idle, and lazy carousel slides that are
    # off to the side never load. Neither should block; visible images are what the screenshots need.
    for wait in (lambda: page.wait_for_load_state("networkidle", timeout=8000),
                 lambda: page.wait_for_function(
                     "[...document.images].filter(i => { const r = i.getBoundingClientRect();"
                     " return r.left < innerWidth && r.right > 0 && r.width > 0; }).every(i => i.complete)",
                     timeout=10000)):
        try:
            wait()
        except PlaywrightTimeout:
            pass
    page.wait_for_timeout(800)


def _tiles(page: Page, top: float, height: float, out: Path, prefix: str) -> Shot:
    out.mkdir(parents=True, exist_ok=True)
    shot = Shot(height=round(height))
    y, i = top, 0
    while y < top + height - 1:
        rest = top + height - y
        h = rest if rest <= TILE_H + MIN_TAIL else TILE_H  # fold a thin tail into this tile, not its own image
        path = out / f"{prefix}-{i}.jpg"
        page.screenshot(path=str(path), type="jpeg", quality=JPEG_Q, full_page=True,
                        clip={"x": 0, "y": y, "width": WIDTH, "height": h})
        shot.tiles.append(path)
        y, i = y + h, i + 1
    return shot


def scrape(url: str, n: int, out: Path, max_lines: int = 700) -> Source:
    with sync_playwright() as p:
        browser, page = _page(p)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector("main", timeout=30000)
            # Scroll far enough for lazy images: measure where section N ends, settle to there, then re-measure.
            _settle(page, 4000)
            bottom = page.evaluate(BOTTOM_JS, n)
            if bottom > 4000:
                _settle(page, int(bottom))
            data = page.evaluate(DIGEST_JS, [n, max_lines])
            if len(data["bands"]) < n:
                raise RuntimeError(f"found only {len(data['bands'])} sections under <main>")
            first, last = data["bands"][0], data["bands"][-1]
            # Span to the last section's bottom, so margins between sections are included.
            shot = _tiles(page, first["top"], last["top"] + last["height"] - first["top"], out, "source")
        finally:
            browser.close()
    digest = data["digest"] + ("\n[digest truncated]" if data["truncated"] else "")
    return Source(url=url, digest=digest, images=data["images"], texts=data["texts"], shot=shot)


def open_storefront(page: Page, url: str, password: str) -> None:
    """Go to `url`, getting past a dev store's password page first."""
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    if "/password" not in page.url:
        return
    if not password:
        raise RuntimeError("storefront is password-protected; set STOREFRONT_PASSWORD")
    page.fill('input[type="password"]', password)
    with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):  # the POST must land first
        page.press('input[type="password"]', "Enter")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    if "/password" in page.url:
        raise RuntimeError("storefront password was rejected")


def shoot_rebuild(product_url: str, password: str, out: Path, prefix: str) -> Shot:
    with sync_playwright() as p:
        browser, page = _page(p)
        try:
            open_storefront(page, product_url, password)
            try:
                el = page.wait_for_selector(REBUILD_SELECTOR, timeout=30000)
            except PlaywrightTimeout:  # charged: usually the section's own Liquid broke the render
                raise RuntimeError("the rebuilt section did not render on the product page") from None
            box = el.bounding_box()
            _settle(page, int(box["y"] + box["height"]))
            box = page.eval_on_selector(REBUILD_SELECTOR, "e => { const r = e.getBoundingClientRect(); "
                                        "return {y: r.top + scrollY, h: r.height}; }")
            if box["h"] < 50:
                raise RuntimeError(f"rebuilt section rendered only {box['h']}px tall")
            return _tiles(page, box["y"], box["h"], out, prefix)
        finally:
            browser.close()


def side_by_side(src: Shot, dst: Shot, out: Path, prefix: str) -> list[Path]:
    """One image per band: source on the left, rebuild on the right, same scale."""
    bands = max(len(src.tiles), len(dst.tiles))
    paths = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        # Short viewport: full_page then sizes each pair to its tallest image instead of padding to TILE_H.
        page = browser.new_page(viewport={"width": WIDTH * 2 + 10, "height": 100}, device_scale_factor=1)
        try:
            for i in range(bands):
                cells = []
                for shot in (src, dst):
                    if i < len(shot.tiles):
                        b64 = base64.b64encode(shot.tiles[i].read_bytes()).decode()
                        cells.append(f'<img src="data:image/jpeg;base64,{b64}">')
                    else:
                        cells.append('<div class="end">(ends)</div>')
                page.set_content(
                    "<style>body{margin:0;display:flex;gap:10px;background:#f0f;align-items:flex-start}"
                    f"img,.end{{width:{WIDTH}px;display:block}}.end{{height:80px;background:#fff}}</style>"
                    + "".join(cells))
                path = out / f"{prefix}-pair-{i}.jpg"
                page.screenshot(path=str(path), type="jpeg", quality=JPEG_Q, full_page=True)
                paths.append(path)
        finally:
            browser.close()
    return paths
