# nomore-section-cloner

A [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) script that takes a Shopify product
page URL and rebuilds the page's **first 3 sections** as **one Liquid section** in your Shopify theme, as close to
the original as possible **on mobile**. Every text and every image ends up as a theme-editor setting, so nothing
is hardcoded.

```bash
make install
cp .env.example .env        # fill in the 4 values (see Setup)
make run                    # = uv run python -m sectioncloner <url>
```

## What it does

```
source URL ──► scrape (Playwright, 390px iPhone viewport)
                 compact DOM digest: tags, sizes, computed styles, text, image URLs  (~11k tokens)
                 + screenshot tiles of the first 3 sections
           ──► Claude agent (claude-sonnet-5-5, 5 tools, nothing else)
                 get_source → write_files → compare → (edit_file → compare)* → finish
           ──► your store
                 sections/nomore-landing.liquid          one section: markup + {% style %} + {% schema %}
                 templates/product.nomore.json           the scraped copy + CDN image URLs as setting values
                 product "nomore-landing-placeholder"    one variant, dummy price, uses the template above
```

| Tool | What it does | Guard |
|---|---|---|
| `get_source` | scrapes the source once; returns the digest + screenshots | 2 calls |
| `write_files` | lints, uploads both files to the live theme, reads them back, creates the placeholder product | 4 calls |
| `edit_file` | exact one-match replace in the last written file, then the same lint/upload/read-back | 25 calls |
| `compare` | screenshots the rebuilt section on the storefront; returns original \| rebuild side by side per band | **4 calls = the visual iteration cap** |
| `finish` | accepted only if the latest change was written **and** compared | 3 calls |

The limits live in Python (`agent.py`), not in the prompt. The process exits `0` only after an accepted `finish`,
and always prints the run's **API cost** (`ResultMessage.total_cost_usd`).

### "No hardcoded content" is enforced, not requested

`lint.py` rejects a write when the section's markup contains any literal text, any literal image/link URL, or any
literal `alt`/`title`/`aria-label`. CSS, JS, inline SVG icons and the `{% schema %}` block are excluded. The agent
gets the exact list of violations back and has to fix them before anything reaches the store.

Images use a `url` setting (the source CDN link, used as-is) plus an optional `image_picker` override, because
Shopify's image picker can't hold an external URL. Swap any image in the theme editor by picking a file.

After each write, the agent also gets a **content-gap** list: source texts and image URLs that aren't in the
written files yet.

## Setup

1. **Dev store.** A free Partner development store works: <https://partners.shopify.com> → Stores → Add store.
2. **Admin API token.** In the store admin: Settings → Apps → Develop apps → create a (legacy) custom app →
   Admin API scopes: `read_products write_products read_themes write_themes read_publications write_publications`
   → Install → copy the `shpat_…` token.
3. **Storefront password.** Dev stores are always password-protected. The agent needs the password from
   Online Store → Preferences to screenshot its own work.
4. **Claude.** Set `ANTHROPIC_API_KEY`, or leave it empty to use a logged-in `claude` CLI.
5. Fill `.env`:

| Key | Value |
|---|---|
| `SHOPIFY_STORE_DOMAIN` | `your-store.myshopify.com` |
| `SHOPIFY_ADMIN_TOKEN` | `shpat_…` |
| `STOREFRONT_PASSWORD` | the storefront password |
| `ANTHROPIC_API_KEY` | optional |

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 18+ (the Agent SDK drives the Claude Code CLI).

## Run

```bash
make run
# or another page / section count / model:
uv run python -m sectioncloner https://example.com/products/x --sections 3 --model claude-sonnet-5-5
```

Each run writes `runs/<timestamp>/`: `run.log`, the source tiles, every rebuild screenshot and every side-by-side
comparison the agent saw.

The rebuilt page is at `https://<store>/products/nomore-landing-placeholder`. To edit it, open
Online Store → Themes → Customize → Products → `nomore`.

## Cost

One full run: see **Live run** below. The digest is pruned (hidden nodes skipped, wrapper divs collapsed, only
styles that differ from the parent), the screenshots are phone-width tiles, and small fixes go through `edit_file`
instead of rewriting the file. That keeps a run well under $5.

## Tests

```bash
make test        # offline: the hardcoded-content linter, template shape, content-gap check
```

## Live run

_Filled in from the recorded run._
