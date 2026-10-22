"""Offline tests for the finish gate, budget refunds and the Admin client (fake HTTP, no store, no LLM)."""

import asyncio
import json
from pathlib import Path

import httpx

from sectioncloner import shopify
from sectioncloner.agent import BUDGET, Run, build_tools
from sectioncloner.browser import Shot, Source


class FakeAdmin:
    domain = "x.myshopify.com"


def _tools(tmp_path: Path):
    run = Run(url="https://shop.example/products/a", admin=FakeAdmin(), password="", out=tmp_path)
    return run, {t.name: t.handler for t in build_tools(run)}


def _call(handler, args=None):
    return asyncio.run(handler(args or {}))


def test_compare_and_finish_refused_before_any_write_without_spending_budget(tmp_path):
    run, tools = _tools(tmp_path)
    for _ in range(BUDGET["compare"] + 2):
        assert _call(tools["compare"]).get("is_error")
    assert run.calls["compare"] == 0  # refusals are not charged
    assert _call(tools["finish"], {"report": "x"}).get("is_error")
    assert not run.done()


def test_finish_needs_a_compare_after_the_last_write(tmp_path):
    run, tools = _tools(tmp_path)
    run.mutations = run.written_at = 2
    run.product_url = "https://x.myshopify.com/products/p"
    run.compared_at = 1  # compared an older version
    assert _call(tools["finish"], {"report": "x"}).get("is_error")
    run.compared_at = 2
    assert not _call(tools["finish"], {"report": "x"}).get("is_error")
    assert run.done()
    assert _call(tools["compare"]).get("is_error")  # nothing is accepted after finish


def _admin(responses: list, sent: list):
    def handler(request: httpx.Request):
        sent.append(json.loads(request.content))
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)
    return shopify.Admin("x.myshopify.com", "t", client=httpx.Client(transport=httpx.MockTransport(handler)),
                         sleep=lambda s: None)


def test_gql_retries_throttling():
    sent = []
    a = _admin([(429, {}), (200, {"errors": [{"extensions": {"code": "THROTTLED"}}]}), (200, {"data": {"ok": 1}})],
               sent)
    assert a.gql("{ ok }") == {"ok": 1} and len(sent) == 3


def test_upsert_waits_for_the_background_job():
    sent = []
    a = _admin([(200, {"data": {"themeFilesUpsert": {"upsertedThemeFiles": [], "userErrors": [],
                                                     "job": {"id": "gid://shopify/Job/1", "done": False}}}}),
                (200, {"data": {"job": {"id": "gid://shopify/Job/1", "done": False}}}),
                (200, {"data": {"job": {"id": "gid://shopify/Job/1", "done": True}}})], sent)
    shopify.upsert_files(a, "gid://shopify/OnlineStoreTheme/1", {"sections/x.liquid": "{{ x }}"})
    assert len(sent) == 3


def test_placeholder_product_is_idempotent_and_sellable():
    sent = []
    product = {"data": {"productSet": {"product": {"id": "gid://shopify/Product/9", "onlineStoreUrl": None},
                                       "userErrors": []}}}
    pubs = {"data": {"publications": {"nodes": [{"id": "gid://shopify/Publication/1", "name": "Online Store"}]}}}
    publish = {"data": {"publishablePublish": {"userErrors": []}}}
    a = _admin([(200, product), (200, pubs), (200, publish)] * 2, sent)
    ids = [shopify.ensure_product(a, "h", "T", "1.00", "nomore") for _ in range(2)]
    assert ids[0] == ids[1]
    first, second = sent[0]["variables"], sent[3]["variables"]
    assert first == second and first["id"] == {"handle": "h"}
    assert first["input"]["variants"][0]["inventoryPolicy"] == "CONTINUE"


def test_finish_refused_while_height_is_off_and_compares_remain(tmp_path):
    run, tools = _tools(tmp_path)
    run.source = Source(url="u", digest="", images=[], texts=[], shot=Shot(height=2714))
    run.mutations = run.written_at = run.compared_at = 1
    run.product_url = "https://x.myshopify.com/products/p"
    run.rebuild_height, run.calls["compare"] = 2627, 2  # 3.2% short, 2 compares left
    assert _call(tools["finish"], {"report": "x"}).get("is_error")
    run.calls["compare"] = BUDGET["compare"]  # out of compares: finishing is allowed, the gap is reported
    assert not _call(tools["finish"], {"report": "x"}).get("is_error")


def test_finish_allowed_when_height_is_close(tmp_path):
    run, tools = _tools(tmp_path)
    run.source = Source(url="u", digest="", images=[], texts=[], shot=Shot(height=2714))
    run.mutations = run.written_at = run.compared_at = 1
    run.product_url = "https://x.myshopify.com/products/p"
    run.rebuild_height, run.calls["compare"] = 2715, 1
    assert not _call(tools["finish"], {"report": "x"}).get("is_error")


def test_finish_allowed_when_nothing_can_change_anymore(tmp_path):
    run, tools = _tools(tmp_path)
    run.source = Source(url="u", digest="", images=[], texts=[], shot=Shot(height=2714))
    run.mutations = run.written_at = run.compared_at = 1
    run.product_url = "https://x.myshopify.com/products/p"
    run.rebuild_height, run.calls["compare"] = 2627, 2
    run.calls["edit_file"], run.calls["write_files"] = BUDGET["edit_file"], BUDGET["write_files"]
    assert not _call(tools["finish"], {"report": "x"}).get("is_error")  # no deadlock: the gap goes in the report


def test_lint_rejection_is_not_charged_to_the_write_budget(tmp_path):
    run, tools = _tools(tmp_path)
    run.source = Source(url="u", digest="", images=[], texts=[], shot=Shot(height=1))
    for _ in range(BUDGET["write_files"] + 2):
        res = _call(tools["write_files"], {"section_liquid": "<p>hardcoded</p>", "template_json": "{}"})
        assert res.get("is_error")
    assert run.calls["write_files"] == 0


def test_write_scope_implies_read_scope():
    sent = []
    a = _admin([(200, {"data": {"currentAppInstallation": {"accessScopes": [
        {"handle": h} for h in ("write_products", "write_themes", "write_publications")]}}})], sent)
    assert shopify.missing_scopes(a, shopify.SCOPES) == []


def test_cost_is_unknown_not_zero_when_the_sdk_never_reported_it(tmp_path):
    from sectioncloner.agent import fmt_cost
    run, _ = _tools(tmp_path)
    assert run.cost is None and fmt_cost(run.cost).startswith("unknown")
    assert fmt_cost(2.6608) == "$2.6608"
