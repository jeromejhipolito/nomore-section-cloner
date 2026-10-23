"""Minimal Shopify Admin GraphQL client: main theme, theme files, placeholder product."""

from __future__ import annotations

import json
import threading
import time

import httpx

API_VERSION = "2026-01"
JOB_POLLS = 120  # seconds; Shopify's theme job queue can be slow
UNCONFIRMED_GRACE_S = 20
# One theme write at a time for the whole process. Before writing, every earlier write that may still be
# landing (a job we stopped polling, or a request that failed after it may have reached Shopify) is drained.
_UPLOAD_LOCK = threading.Lock()
_pending_jobs: list[str | None] = []  # None = a write request that failed in flight (may or may not have landed)
# Set by the end-of-session restore: from then on no ordinary write is accepted, however late its thread runs.
_closed = threading.Event()
lost_track = 0  # earlier writes the drain could not confirm before letting the next write go ahead
SCOPES = {"read_products", "write_products", "read_themes", "write_themes", "read_publications", "write_publications"}


def normalize_domain(domain: str) -> str:
    return domain.removeprefix("https://").removeprefix("http://").strip("/")


class ShopifyError(RuntimeError):
    pass


class ShopifyTransientError(ShopifyError):
    """Infra trouble worth a free retry: unreachable, 5xx, still throttled. Never the caller's own mistake.

    maybe_applied: the request may have reached Shopify (read timeout, 5xx, ...), so a write could still land."""

    def __init__(self, msg: str, maybe_applied: bool = False):
        super().__init__(msg)
        self.maybe_applied = maybe_applied


class Admin:
    def __init__(self, domain: str, token: str, client: httpx.Client | None = None, sleep=time.sleep):
        self.domain = normalize_domain(domain)
        self.url = f"https://{self.domain}/admin/api/{API_VERSION}/graphql.json"
        self.http = client or httpx.Client(timeout=60)
        self.headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
        self.sleep = sleep

    def gql(self, query: str, variables: dict | None = None, retry_transport: bool = True) -> dict:
        for attempt in range(5):
            try:
                r = self.http.post(self.url, json={"query": query, "variables": variables or {}},
                                   headers=self.headers)
                body = r.json() if r.status_code == 200 else None
            except httpx.TransportError as e:  # DNS / reset / timeout: retry, then a clean error
                sent = not isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout))  # did it reach Shopify?
                if attempt < 4 and (retry_transport or not sent):  # never blindly re-send a write that may have landed
                    self.sleep(min(2**attempt, 20))
                    continue
                raise ShopifyTransientError(f"cannot reach {self.domain}: {e}", maybe_applied=sent) from e
            except ValueError as e:  # a 200 that isn't JSON (an HTML interstitial)
                raise ShopifyTransientError(f"Admin API returned non-JSON: {r.text[:200]}", maybe_applied=True) from e
            throttled = body is not None and any(
                (e.get("extensions") or {}).get("code") == "THROTTLED" for e in body.get("errors") or [])
            if r.status_code == 429 or throttled:
                if attempt < 4:  # no pointless sleep before giving up
                    self.sleep(min(2**attempt, 20))
                continue
            if r.status_code != 200:
                if r.status_code >= 500:
                    raise ShopifyTransientError(f"Admin API HTTP {r.status_code}: {r.text[:300]}", maybe_applied=True)
                raise ShopifyError(f"Admin API HTTP {r.status_code}: {r.text[:300]}")
            if body.get("errors"):
                codes = {(e.get("extensions") or {}).get("code") for e in body["errors"]}
                if codes & {"INTERNAL_SERVER_ERROR", "TIMEOUT"}:
                    raise ShopifyTransientError(f"GraphQL errors: {body['errors']}", maybe_applied=True)
                raise ShopifyError(f"GraphQL errors: {body['errors']}")
            return body["data"]
        raise ShopifyTransientError("Admin API stayed throttled after 5 attempts")


def _raise_user_errors(errs: list[dict]) -> None:
    if errs:
        raise ShopifyError("; ".join(f"{'.'.join(map(str, e.get('field') or []))}: {e['message']}" for e in errs))


def main_theme_id(admin: Admin) -> str:
    nodes = admin.gql("{ themes(first: 1, roles: [MAIN]) { nodes { id name } } }")["themes"]["nodes"]
    if not nodes:
        raise ShopifyError("store has no published (MAIN) theme")
    return nodes[0]["id"]


def upsert_files(admin: Admin, theme_id: str, files: dict[str, str], final: bool = False) -> None:
    """Write text files to the theme. Shopify validates Liquid/JSON here and returns userErrors.

    final=True is the end-of-session restore: it closes the store to every later ordinary write, and (being
    idempotent) it retries transport errors instead of giving up on the one write that must succeed."""
    if final:
        _closed.set()
    with _UPLOAD_LOCK:
        if not final and _closed.is_set():
            raise ShopifyError("superseded by the final restore: not written")
        _drain_pending(admin)
        _upsert(admin, theme_id, files, final)


def _drain_pending(admin: Admin) -> None:
    """Let every earlier write land before this one. Bounded, and never silent: what can't be confirmed is counted."""
    global lost_track
    while _pending_jobs:
        job_id = _pending_jobs[0]
        try:
            if job_id is None:  # a request that failed in flight: no job id to ask about, only time
                admin.sleep(UNCONFIRMED_GRACE_S)
                lost_track += 1
            else:
                _wait_job(admin, job_id)
        except Exception:  # noqa: BLE001 - can't confirm it: give it the grace period, count it, move on
            admin.sleep(UNCONFIRMED_GRACE_S)
            lost_track += 1
        finally:
            _pending_jobs.pop(0)


def _wait_job(admin: Admin, job_id: str) -> None:
    for _ in range(JOB_POLLS):
        data = admin.gql("query($id: ID!) { job(id: $id) { id done } }", {"id": job_id}) or {}
        job = data.get("job")  # None can mean "not queryable yet", so it is never read as "finished"
        if job and job.get("done"):
            return
        admin.sleep(1)
    raise ShopifyTransientError(f"theme write job {job_id} still not done after {JOB_POLLS}s", maybe_applied=True)


def _upsert(admin: Admin, theme_id: str, files: dict[str, str], final: bool) -> None:
    try:
        data = _upsert_request(admin, theme_id, files, retry_transport=final)
    except ShopifyTransientError as e:
        if e.maybe_applied:
            _pending_jobs.append(None)  # it may have been accepted: the next write waits for it to land
        raise
    _raise_user_errors(data["userErrors"])
    job = data.get("job")
    if not job or job.get("done"):
        return
    _pending_jobs.append(job["id"])  # tracked BEFORE waiting, so no failure path can drop it
    _wait_job(admin, job["id"])
    _pending_jobs.remove(job["id"])


def _upsert_request(admin: Admin, theme_id: str, files: dict[str, str], retry_transport: bool) -> dict:
    return admin.gql(
        """mutation($themeId: ID!, $files: [OnlineStoreThemeFilesUpsertFileInput!]!) {
          themeFilesUpsert(themeId: $themeId, files: $files) {
            upsertedThemeFiles { filename } job { id done } userErrors { field message } } }""",
        {"themeId": theme_id,
         "files": [{"filename": n, "body": {"type": "TEXT", "value": v}} for n, v in files.items()]},
        retry_transport=retry_transport,
    )["themeFilesUpsert"]


def missing_scopes(admin: Admin, needed: set[str]) -> list[str]:
    have = {s["handle"] for s in admin.gql("{ currentAppInstallation { accessScopes { handle } } }")
            ["currentAppInstallation"]["accessScopes"]}
    have |= {"read_" + s.removeprefix("write_") for s in have if s.startswith("write_")}  # write_X implies read_X
    return sorted(needed - have)


def read_files(admin: Admin, theme_id: str, names: list[str]) -> dict[str, str]:
    """Fresh read of theme files; missing files are absent from the result."""
    nodes = admin.gql(
        """query($id: ID!, $names: [String!]!) { theme(id: $id) { files(filenames: $names) {
             nodes { filename body { ... on OnlineStoreThemeFileBodyText { content } } } } } }""",
        {"id": theme_id, "names": names},
    )["theme"]["files"]["nodes"]
    return {n["filename"]: (n["body"] or {}).get("content", "") for n in nodes}


def parse_template(text: str):
    """Shopify prepends a /* auto-generated */ comment to JSON templates it stores; drop it before parsing."""
    text = text.lstrip()
    if text.startswith("/*"):
        text = text[text.index("*/") + 2:]
    return json.loads(text)


def ensure_product(admin: Admin, handle: str, title: str, price: str, template_suffix: str) -> str:
    """Create/update the placeholder product by handle (one default variant), active and on the Online Store."""
    data = admin.gql(
        """mutation($input: ProductSetInput!, $id: ProductSetIdentifiers) {
          productSet(synchronous: true, input: $input, identifier: $id) {
            product { id onlineStoreUrl } userErrors { field message } } }""",
        {"id": {"handle": handle},
         "input": {"title": title, "handle": handle, "status": "ACTIVE", "templateSuffix": template_suffix,
                   "productOptions": [{"name": "Title", "values": [{"name": "Default Title"}]}],
                   # CONTINUE: the placeholder has no stock, and Add to cart must still work.
                   "variants": [{"optionValues": [{"optionName": "Title", "name": "Default Title"}],
                                 "price": price, "inventoryPolicy": "CONTINUE"}]}},
    )["productSet"]
    _raise_user_errors(data["userErrors"])
    product_id = data["product"]["id"]
    pubs = admin.gql("{ publications(first: 20) { nodes { id name } } }")["publications"]["nodes"]
    online = [p["id"] for p in pubs if p["name"] == "Online Store"]
    if not online:
        raise ShopifyError("no 'Online Store' publication found")
    res = admin.gql(
        """mutation($id: ID!, $input: [PublicationInput!]!) {
          publishablePublish(id: $id, input: $input) { userErrors { field message } } }""",
        {"id": product_id, "input": [{"publicationId": online[0]}]},
    )["publishablePublish"]
    _raise_user_errors(res["userErrors"])
    return product_id
