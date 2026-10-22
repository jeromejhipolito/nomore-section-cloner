"""CLI: python -m sectioncloner <product-url> [--sections 3] [--model ...]"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from . import shopify
from .agent import Run, run_agent

DEFAULT_MODEL = "claude-sonnet-5-5"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url")
    ap.add_argument("--sections", type=int, default=3)
    ap.add_argument("--model", default=os.getenv("CLONER_MODEL", DEFAULT_MODEL))
    args = ap.parse_args(argv)

    load_dotenv()
    missing = [k for k in ("SHOPIFY_STORE_DOMAIN", "SHOPIFY_ADMIN_TOKEN") if not os.getenv(k)]
    if missing:
        print(f"missing in .env: {', '.join(missing)}", file=sys.stderr)
        return 2
    out = Path("runs") / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    log_file = (out / "run.log").open("w")

    def log(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        log_file.write(line + "\n")
        log_file.flush()

    admin = shopify.Admin(os.environ["SHOPIFY_STORE_DOMAIN"], os.environ["SHOPIFY_ADMIN_TOKEN"])
    run = Run(url=args.url, admin=admin, password=os.getenv("STOREFRONT_PASSWORD", ""), out=out,
              sections=args.sections, log=log)
    log(f"rebuilding the first {args.sections} sections of {args.url} with {args.model}")
    code = asyncio.run(run_agent(run, args.model))
    log(f"{'DONE' if code == 0 else 'FAILED'}: {run.product_url or '-'}  |  API cost ${run.cost:.4f}  |  artifacts {out}")
    return code


if __name__ == "__main__":
    sys.exit(main())
