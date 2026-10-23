"""CLI: python -m sectioncloner <product-url> [--sections 3] [--model ...]"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from . import shopify
from .agent import EXIT_PARTIAL, Run, fmt_cost, run_agent

DEFAULT_MODEL = "claude-sonnet-5-5"


def main(argv: list[str] | None = None) -> int:
    load_dotenv()  # before argparse, so CLONER_MODEL in .env is the --model default
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url")
    ap.add_argument("--sections", type=int, default=3, choices=range(1, 11), metavar="1-10")
    ap.add_argument("--model", default=os.getenv("CLONER_MODEL", DEFAULT_MODEL))
    args = ap.parse_args(argv)
    if not re.match(r"https?://[^/]+/.*products/", args.url):
        print(f"not a Shopify product URL: {args.url}", file=sys.stderr)
        return 2

    missing = [k for k in ("SHOPIFY_STORE_DOMAIN", "SHOPIFY_ADMIN_TOKEN") if not os.getenv(k)]
    if missing:
        print(f"missing in .env: {', '.join(missing)}", file=sys.stderr)
        return 2
    admin = shopify.Admin(os.environ["SHOPIFY_STORE_DOMAIN"], os.environ["SHOPIFY_ADMIN_TOKEN"])
    # Fail here, for free, instead of after the agent has spent money on a write it isn't allowed to make.
    try:
        lacking = shopify.missing_scopes(admin, shopify.SCOPES)
    except shopify.ShopifyTransientError as e:
        print(f"Shopify is unreachable or erroring right now (not a config problem), retry: {e}", file=sys.stderr)
        return 2
    except shopify.ShopifyError as e:
        print(f"cannot use the Admin API token: {e}", file=sys.stderr)
        return 2
    if lacking:
        print(f"the Admin API token is missing scopes: {', '.join(lacking)}", file=sys.stderr)
        return 2
    out = Path("runs") / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    log_file = (out / "run.log").open("w")

    def log(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        log_file.write(line + "\n")
        log_file.flush()

    run = Run(url=args.url, admin=admin, password=os.getenv("STOREFRONT_PASSWORD", ""), out=out,
              sections=args.sections, log=log)
    log(f"rebuilding the first {args.sections} sections of {args.url} with {args.model}")
    code = 1
    try:
        code = asyncio.run(run_agent(run, args.model))
    finally:  # the cost line is printed even if the run crashes
        status = {0: "DONE", EXIT_PARTIAL: "PARTIAL"}.get(code, "FAILED")
        log(f"{status}: {run.product_url or '-'}  |  API cost {fmt_cost(run.cost)}"
            f"  |  artifacts {out}")
    return code


if __name__ == "__main__":
    sys.exit(main())
