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
    run.compared_at, run.good = 1, {"sections/nomore-landing.liquid": "a", "templates/product.nomore.json": "{}"}  # compared an older version
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
    run.product_url, run.good = "https://x.myshopify.com/products/p", {"sections/nomore-landing.liquid": "a", "templates/product.nomore.json": "{}"}
    run.rebuild_height, run.calls["compare"] = 2715, 1
    assert not _call(tools["finish"], {"report": "x"}).get("is_error")


def test_finish_allowed_when_nothing_can_change_anymore(tmp_path):
    run, tools = _tools(tmp_path)
    run.source = Source(url="u", digest="", images=[], texts=[], shot=Shot(height=2714))
    run.mutations = run.written_at = run.compared_at = 1
    run.product_url, run.good = "https://x.myshopify.com/products/p", {"sections/nomore-landing.liquid": "a", "templates/product.nomore.json": "{}"}
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


def test_no_writes_after_the_last_compare(tmp_path):
    run, tools = _tools(tmp_path)
    run.source = Source(url="u", digest="", images=[], texts=[], shot=Shot(height=1))
    run.files = {"sections/nomore-landing.liquid": "a", "templates/product.nomore.json": "{}"}
    run.calls["compare"] = BUDGET["compare"]
    assert _call(tools["edit_file"], {"file": "section", "old": "a", "new": "b"}).get("is_error")
    assert _call(tools["write_files"], {"section_liquid": "a", "template_json": "{}"}).get("is_error")
    assert run.mutations == 0 and run.calls["edit_file"] == 0 and run.calls["write_files"] == 0


def test_network_errors_become_a_clean_shopify_error():
    def boom(request):
        raise httpx.ConnectError("dns")
    a = shopify.Admin("nope.myshopify.com", "t", client=httpx.Client(transport=httpx.MockTransport(boom)),
                      sleep=lambda s: None)
    try:
        a.gql("{ shop { name } }")
    except shopify.ShopifyError as e:
        assert "cannot reach nope.myshopify.com" in str(e)
    else:
        raise AssertionError("expected ShopifyError")


def test_a_crashing_compare_is_not_charged_until_the_failure_cap(tmp_path, monkeypatch):
    from sectioncloner import agent, browser
    run, tools = _tools(tmp_path)
    run.source = Source(url="u", digest="", images=[], texts=[], shot=Shot(height=10))
    run.mutations = run.written_at = 1
    run.product_url = "https://x.myshopify.com/products/p"

    def boom(*a, **k):
        raise TimeoutError("storefront timed out")
    monkeypatch.setattr(browser, "shoot_rebuild", boom)
    for _ in range(agent.FAILURE_CAP):
        assert _call(tools["compare"]).get("is_error")
    assert run.calls["compare"] == 0 and run.failures == agent.FAILURE_CAP
    _call(tools["compare"])  # past the cap, crashes cost budget again (no infinite retry loop)
    assert run.calls["compare"] == 1


def test_finish_accepted_when_no_budget_path_remains_then_settle_decides_the_exit(tmp_path, monkeypatch):
    from sectioncloner import agent
    run, tools = _tools(tmp_path)
    run.mutations, run.written_at, run.compared_at = 3, 2, 2  # last write failed after the last compare
    run.files = run.good = {"sections/nomore-landing.liquid": "a", "templates/product.nomore.json": "{}"}
    run.theme_id = "gid://shopify/OnlineStoreTheme/1"
    run.calls["compare"] = BUDGET["compare"]  # and every compare is used
    assert not _call(tools["finish"], {"report": "x"}).get("is_error") and run.finished
    monkeypatch.setattr(shopify, "upsert_files", lambda a, t, f: (_ for _ in ()).throw(shopify.ShopifyError("no")))
    asyncio.run(agent.settle(run))
    assert agent.exit_code(run) == agent.EXIT_PARTIAL and run.partial  # could not put it back: partial


def test_finish_still_refused_while_a_path_to_current_exists(tmp_path):
    run, tools = _tools(tmp_path)
    run.mutations, run.written_at, run.compared_at = 3, 3, 2
    run.calls["compare"] = 2  # a compare is still available
    assert _call(tools["finish"], {"report": "x"}).get("is_error") and not run.finished


def test_domain_normalisation_handles_http_and_trailing_slash():
    assert shopify.normalize_domain("http://s.myshopify.com/") == "s.myshopify.com"
    assert shopify.Admin("https://s.myshopify.com/", "t").domain == "s.myshopify.com"


def test_finish_reachable_when_the_scrape_never_succeeds(tmp_path):
    from sectioncloner import agent
    run, tools = _tools(tmp_path)
    run.calls["get_source"] = BUDGET["get_source"]  # every scrape failed, source is None
    assert not _call(tools["finish"], {"report": "could not read the source page"}).get("is_error")
    assert agent.exit_code(run) == 1  # nothing was built: not a "partial", a failure


def test_only_infra_crashes_are_forgiven():
    from sectioncloner.agent import is_transient
    assert is_transient(TimeoutError()) and is_transient(httpx.ConnectError("x"))
    assert is_transient(shopify.ShopifyTransientError("Admin API HTTP 502: bad gateway"))
    assert not is_transient(shopify.ShopifyError("files.0: Liquid syntax error"))  # the agent's own mistake
    assert not is_transient(shopify.ShopifyError("throttled"))  # typed, not matched on message text
    assert not is_transient(RuntimeError("the rebuilt section did not render on the product page"))


def test_settle_puts_the_last_compared_version_back_however_the_session_ended(tmp_path, monkeypatch):
    from sectioncloner import agent
    run, _ = _tools(tmp_path)
    good = {agent.SECTION_FILE: "{{ x }}", agent.TEMPLATE_FILE: '{"order": []}'}
    run.good, run.theme_id = good, "gid://shopify/OnlineStoreTheme/1"
    run.files = {agent.SECTION_FILE: "new", agent.TEMPLATE_FILE: '{"order": [1]}'}
    run.mutations, run.written_at, run.compared_at = 3, 2, 2  # half-failed write, session then hit its budget
    store = {}
    calls = []
    monkeypatch.setattr(shopify, "upsert_files", lambda a, t, files: (calls.append(sorted(files)), store.update(files)))
    monkeypatch.setattr(shopify, "read_files", lambda a, t, names: {n: store[n] for n in names})
    asyncio.run(agent.settle(run))
    assert store == good and calls == [sorted(good)]  # both files in ONE upload
    assert run.current() and agent.exit_code(run) == 1  # store is safe, but the session never finished
    run.finished = True
    assert agent.exit_code(run) == 0


def test_exit_codes(tmp_path):
    from sectioncloner.agent import EXIT_PARTIAL
    assert EXIT_PARTIAL == 3  # distinct from the CLI's 2 (usage/config) and 1 (never finished)
