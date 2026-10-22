"""Minimal Shopify Admin GraphQL client: main theme, theme files, placeholder product."""

from __future__ import annotations

import json
import time

import httpx

API_VERSION = "2026-01"
JOB_POLLS = 120  # seconds; Shopify's theme job queue can be slow
SCOPES = {"read_products", "write_products", "read_themes", "write_themes", "read_publications", "write_publications"}


def normalize_domain(domain: str) -> str:
    return domain.removeprefix("https://").removeprefix("http://").strip("/")


class ShopifyError(RuntimeError):
    pass


class ShopifyTransientError(ShopifyError):
    """Infra trouble worth a free retry: unreachable, 5xx, still throttled. Never the caller's own mistake."""


class Admin:
    def __init__(self, domain: str, token: str, client: httpx.Client | None = None, sleep=time.sleep):
        self.domain = normalize_domain(domain)
        self.url = f"https://{self.domain}/admin/api/{API_VERSION}/graphql.json"
        self.http = client or httpx.Client(timeout=60)
        self.headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
        self.sleep = sleep

    def gql(self, query: str, variables: dict | None = None) -> dict:
        for attempt in range(5):
            try:
                r = self.http.post(self.url, json={"query": query, "variables": variables or {}},
                                   headers=self.headers)
                body = r.json() if r.status_code == 200 else None
            except httpx.TransportError as e:  # DNS / reset / timeout: retry, then a clean error
                if attempt < 4:
                    self.sleep(min(2**attempt, 20))
                    continue
                raise ShopifyTransientError(f"cannot reach {self.domain}: {e}") from e
            except ValueError as e:  # a 200 that isn't JSON (an HTML interstitial)
                raise ShopifyTransientError(f"Admin API returned non-JSON: {r.text[:200]}") from e
            throttled = body is not None and any(
                (e.get("extensions") or {}).get("code") == "THROTTLED" for e in body.get("errors") or [])
            if r.status_code == 429 or throttled:
                if attempt < 4:  # no pointless sleep before giving up
                    self.sleep(min(2**attempt, 20))
                continue
            if r.status_code != 200:
                err = ShopifyTransientError if r.status_code >= 500 else ShopifyError
                raise err(f"Admin API HTTP {r.status_code}: {r.text[:300]}")
            if body.get("errors"):
                codes = {(e.get("extensions") or {}).get("code") for e in body["errors"]}
                err = ShopifyTransientError if codes & {"INTERNAL_SERVER_ERROR", "TIMEOUT"} else ShopifyError
                raise err(f"GraphQL errors: {body['errors']}")
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


def upsert_files(admin: Admin, theme_id: str, files: dict[str, str]) -> None:
    """Write text files to the theme. Shopify validates Liquid/JSON here and returns userErrors."""
    data = admin.gql(
        """mutation($themeId: ID!, $files: [OnlineStoreThemeFilesUpsertFileInput!]!) {
          themeFilesUpsert(themeId: $themeId, files: $files) {
            upsertedThemeFiles { filename } job { id done } userErrors { field message } } }""",
        {"themeId": theme_id,
         "files": [{"filename": n, "body": {"type": "TEXT", "value": v}} for n, v in files.items()]},
    )["themeFilesUpsert"]
    _raise_user_errors(data["userErrors"])
    job = data.get("job")
    for _ in range(JOB_POLLS):  # the write can finish in the background; a read-back before then sees old content
        if not job or job.get("done"):
            return
        admin.sleep(1)
        job = admin.gql("query($id: ID!) { job(id: $id) { id done } }", {"id": job["id"]})["job"]
    raise ShopifyTransientError(f"theme write job {job['id']} still not done after {JOB_POLLS}s")


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
