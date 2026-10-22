"""Record ONE real run as an mp4: the live terminal output of `python -m sectioncloner <url>` (with each
side-by-side comparison the agent looks at, as it appears), then the rebuilt page at phone size.

Nothing is replayed: the run starts when the recording starts. Everything is filmed by headless Playwright,
so no desktop/screen capture permission is needed and nothing else on the screen ends up in the video.

    uv run python scripts/record_run.py [url] [-o demo.mp4]
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from sectioncloner.show import PHONE, product_url, tour

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_URL = "https://try-nomore.com/products/nomorecolds-red-light-therapy-colds-sinusitis"
SIZE = {"width": 1280, "height": 800}
ANSI = re.compile(r"\x1b\[[0-9;]*m")

PAGE = """<!doctype html><meta charset=utf-8><title>run</title><style>
body{margin:0;background:#0d1117;color:#c9d1d9;font:13px/1.45 ui-monospace,Menlo,monospace;display:flex;height:100vh}
#term{flex:1;padding:14px 18px;overflow:hidden;display:flex;flex-direction:column}
#bar{color:#8b949e;border-bottom:1px solid #30363d;padding-bottom:8px;margin-bottom:8px}
#log{flex:1;overflow:hidden;white-space:pre-wrap;word-break:break-word}
.cmd{color:#7ee787}.tool{color:#79c0ff}.res{color:#8b949e}.err{color:#ff7b72}.ok{color:#7ee787;font-weight:bold}
.agent{color:#d2a8ff}.cost{color:#f2cc60;font-weight:bold}
#side{width:420px;border-left:1px solid #30363d;padding:12px;display:flex;flex-direction:column;gap:6px}
#side h4{margin:0;color:#8b949e;font-weight:normal}#side img{width:100%;border-radius:4px}
</style><div id=term><div id=bar>~/nomore-section-cloner &nbsp;·&nbsp; <span id=clock>00:00</span></div>
<div id=log><span class=cmd>$ uv run python -m sectioncloner {CMD_URL}</span>\n</div></div>
<div id=side><h4>Agent's latest compare: original | rebuild (390px)</h4><img id=cmp></div>
<script>
const log = document.getElementById('log'), t0 = Date.now();
setInterval(() => { const s = Math.floor((Date.now() - t0) / 1000);
  document.getElementById('clock').textContent = String(s / 60 | 0).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0'); }, 500);
const es = new EventSource('/events');
es.onmessage = e => { const d = JSON.parse(e.data);
  if (d.img) { document.getElementById('cmp').src = '/file?p=' + encodeURIComponent(d.img); return; }
  if (d.exit !== undefined) { window.__exit = d.exit; es.close(); return; }
  const div = document.createElement('div'); div.className = d.cls; div.textContent = d.line; log.appendChild(div);
  while (log.scrollHeight > log.clientHeight && log.children.length > 1) log.removeChild(log.children[1]); };
</script>"""


def classify(line: str) -> str:
    if "] -> " in line:
        return "tool"
    if "<- ERROR" in line or "Error" in line or "FAILED" in line:
        return "err"
    if "API cost" in line or "session end" in line:
        return "cost"
    if "DONE:" in line or "finish accepted" in line:
        return "ok"
    if "] agent:" in line:
        return "agent"
    return "res"


class Run:
    def __init__(self, url: str):
        self.url, self.exit, self.lines, self.images, self.proc = url, None, [], [], None
        self.cond = threading.Condition()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _push(self, item: dict) -> None:
        with self.cond:
            self.lines.append(item)
            self.cond.notify_all()

    def _run(self) -> None:
        env = os.environ | {"PYTHONUNBUFFERED": "1"}
        proc = self.proc = subprocess.Popen([sys.executable, "-m", "sectioncloner", self.url], cwd=ROOT, env=env,
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=self._watch_images, args=(proc,), daemon=True).start()
        for raw in proc.stdout:
            line = ANSI.sub("", raw.rstrip("\n"))
            # The digest/tool payloads are long; the terminal shows what a person would read.
            self._push({"line": line if len(line) < 400 else line[:400] + " …", "cls": classify(line)})
        self.exit = proc.wait()
        self._push({"exit": self.exit})

    def _watch_images(self, proc) -> None:
        start, seen = time.time(), set()
        while proc.poll() is None:
            for p in sorted((ROOT / "runs").glob("*/cmp*-pair-0.jpg"), key=lambda p: p.stat().st_mtime):
                if p.stat().st_mtime >= start and p not in seen:
                    seen.add(p)
                    self._push({"img": str(p)})
            time.sleep(1)


def serve(run: Run) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def do_GET(self):
            if self.path == "/":
                body = PAGE.replace("{CMD_URL}", html.escape(run.url)).encode()
                self.send_response(200), self.send_header("Content-Type", "text/html"), self.end_headers()
                self.wfile.write(body)
            elif self.path == "/events":
                self.send_response(200), self.send_header("Content-Type", "text/event-stream"), self.end_headers()
                i = 0
                while True:
                    with run.cond:
                        while i >= len(run.lines):
                            run.cond.wait()
                        batch, i = run.lines[i:], len(run.lines)
                    for item in batch:
                        self.wfile.write(f"data: {json.dumps(item)}\n\n".encode())
                    self.wfile.flush()
                    if any("exit" in b for b in batch):
                        return
            elif self.path.startswith("/file?p="):
                from urllib.parse import unquote
                p = Path(unquote(self.path[8:])).resolve()
                if (ROOT / "runs") not in p.parents:
                    self.send_response(404), self.end_headers()
                    return
                self.send_response(200), self.send_header("Content-Type", "image/jpeg"), self.end_headers()
                self.wfile.write(p.read_bytes())
            else:
                self.send_response(404), self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", default=DEFAULT_URL)
    ap.add_argument("-o", "--out", default="demo.mp4")
    args = ap.parse_args()
    load_dotenv(ROOT / ".env")
    if not shutil.which("ffmpeg"):
        print("ffmpeg is required", file=sys.stderr)
        return 2
    vids = ROOT / "runs" / f"video-{time.strftime('%Y%m%d-%H%M%S')}"
    run = Run(args.url)
    srv = serve(run)
    try:
        return _record(run, srv, vids, args.out)
    finally:  # never leave the agent running (and spending) unwatched if recording fails or times out
        if run.proc and run.proc.poll() is None:
            run.proc.terminate()
        srv.shutdown()


def _record(run: Run, srv, vids: Path, out: str) -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport=SIZE, record_video_dir=str(vids / "term"), record_video_size=SIZE)
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{srv.server_port}/")
        run.start()
        page.wait_for_function("window.__exit !== undefined", timeout=20 * 60 * 1000, polling=1000)
        page.wait_for_timeout(4000)
        ctx.close()
        if run.exit != 0:
            print(f"run failed (exit {run.exit}); terminal video kept in {vids}", file=sys.stderr)
            return run.exit
        phone = browser.new_context(**PHONE, record_video_dir=str(vids / "phone"),
                                    record_video_size=PHONE["viewport"])
        tour(phone.new_page(), product_url(), os.getenv("STOREFRONT_PASSWORD", ""), hold=4)
        phone.close()
        browser.close()
    term, ph = next((vids / "term").glob("*.webm")), next((vids / "phone").glob("*.webm"))
    # Phone clip centred on the same 1280x800 canvas, then both joined into one h264 mp4.
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(term), "-i", str(ph), "-filter_complex",
                    "[0:v]fps=30,format=yuv420p,setsar=1[a];"
                    "[1:v]scale=-2:780,pad=1280:800:(ow-iw)/2:(oh-ih)/2:color=0x0d1117,fps=30,format=yuv420p,"
                    "setsar=1[b];[a][b]concat=n=2:v=1[v]",
                    "-map", "[v]", "-c:v", "libx264", "-crf", "23", "-movflags", "+faststart", out],
                   check=True, cwd=ROOT)
    print(f"video: {ROOT / out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
