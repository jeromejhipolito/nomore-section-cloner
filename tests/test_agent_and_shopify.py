"""Offline tests for the finish gate, budget refunds and the Admin client (fake HTTP, no store, no LLM)."""

import asyncio
import json
from pathlib import Path

import threading
import time

import httpx
import pytest

from sectioncloner import shopify
from sectioncloner.agent import BUDGET, Run, build_tools
from sectioncloner.browser import Shot, Source


@pytest.fixture(autouse=True)
def fresh_write_state(monkeypatch):
    """shopify keeps process-wide write state (lock queue, closed flag); every test starts clean."""
    from sectioncloner import agent
    monkeypatch.setattr(shopify, "_closed", threading.Event())
    monkeypatch.setattr(agent, "READ_RETRY_S", 0)
    monkeypatch.setattr(agent, "CONFIRM_GRACE_S", 0)
    monkeypatch.setattr(shopify, "unconfirmed_writes", 0)


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
    monkeypatch.setattr(shopify, "upsert_files",
                        lambda a, t, f, final=False: (_ for _ in ()).throw(shopify.ShopifyError("no")))
    asyncio.run(agent.settle(run))
    assert agent.exit_code(run) == agent.EXIT_PARTIAL  # could not put it back: partial


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
    assert is_transient(TimeoutError()) and is_transient(ConnectionError())
    assert not is_transient(PermissionError())  # a local disk error is not infra trouble
    # raw httpx errors never reach the agent: Admin.gql wraps them (see test_network_errors_become_...)
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
    monkeypatch.setattr(shopify, "upsert_files",
                        lambda a, t, files, final=False: (calls.append((sorted(files), final)), store.update(files)))
    monkeypatch.setattr(shopify, "read_files", lambda a, t, names: {n: store[n] for n in names if n in store})
    asyncio.run(agent.settle(run))
    assert store == good and calls == [(sorted(good), True)]  # both files in ONE upload, as the final restore
    assert run.current() and agent.exit_code(run) == 1  # store is safe, but the session never finished
    run.finished = True
    assert agent.exit_code(run) == 0


def test_exit_codes(tmp_path):
    from sectioncloner.agent import EXIT_PARTIAL
    assert EXIT_PARTIAL == 3  # distinct from the CLI's 2 (usage/config) and 1 (never finished)


def test_sdk_error_after_a_cap_still_settles_and_exits_from_state(tmp_path, monkeypatch):
    from sectioncloner import agent

    async def fake_query(prompt, options):  # what the SDK does on error_max_budget_usd: result, then raise
        run.finished = True
        yield agent.ResultMessage(subtype="error_max_budget_usd", duration_ms=1, duration_api_ms=1, is_error=True,
                                  num_turns=9, session_id="s", total_cost_usd=4.51)
        raise RuntimeError("Claude Code exited with code 1")
    run, _ = _tools(tmp_path)
    run.good = run.files = {"sections/nomore-landing.liquid": "a", "templates/product.nomore.json": "{}"}
    run.mutations = run.written_at = run.compared_at = 1
    monkeypatch.setattr(agent, "query", fake_query)
    assert asyncio.run(agent.run_agent(run, "m")) == 0  # finished on a compared version: done, no traceback
    assert run.cost == 4.51


def test_graphql_internal_errors_are_transient():
    sent = []
    err = (200, {"errors": [{"message": "x", "extensions": {"code": "INTERNAL_SERVER_ERROR"}}]})
    a = _admin([err] * 5, sent)  # a read is idempotent: retried on every attempt before giving up
    try:
        a.gql("{ shop { name } }")
    except shopify.ShopifyTransientError:
        assert len(sent) == 5
    else:
        raise AssertionError("expected ShopifyTransientError")
OK_UPSERT = {"data": {"themeFilesUpsert": {"upsertedThemeFiles": [], "userErrors": [], "job": None}}}


def test_no_ordinary_write_lands_after_the_final_restore():
    sent = []
    a = _admin([(200, OK_UPSERT)], sent)
    result = {}

    def orphan():
        try:
            shopify.upsert_files(a, "t", {"sections/x.liquid": "stale"})
        except shopify.ShopifyError as e:
            result["queued"] = str(e)
    with shopify._UPLOAD_LOCK:  # an orphan write is queued behind the lock when the final restore starts
        t = threading.Thread(target=orphan)
        t.start()
        time.sleep(0.2)
        shopify._closed.set()
    t.join(5)
    assert "superseded" in result.get("queued", "")
    try:  # and an orphan whose thread only starts AFTER the restore began is refused too
        shopify.upsert_files(a, "t", {"sections/x.liquid": "late"})
    except shopify.ShopifyError as e:
        assert "superseded" in str(e)
    else:
        raise AssertionError("a late ordinary write was accepted after the final restore")
    shopify.upsert_files(a, "t", {"sections/x.liquid": "good"}, final=True)  # the restore itself goes through
    assert len(sent) == 1


def test_resend_policy():
    def client(responses):
        calls = []
        def handler(request):
            calls.append(1)
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return httpx.Response(r[0], json=r[1])
        return calls, shopify.Admin("s.myshopify.com", "t", client=httpx.Client(transport=httpx.MockTransport(handler)),
                                    sleep=lambda s: None)
    calls, a = client([httpx.ReadTimeout("t")])  # may have reached Shopify: an ordinary write is not re-sent
    try:
        shopify.upsert_files(a, "t", {"sections/x.liquid": "1"})
    except shopify.ShopifyTransientError:
        pass
    assert len(calls) == 1
    calls, a = client([httpx.ConnectError("refused"), (200, OK_UPSERT)])  # never left: safe to retry
    shopify.upsert_files(a, "t", {"sections/x.liquid": "1"})
    assert len(calls) == 2
    calls, a = client([httpx.ReadTimeout("t"), (502, {}), (200, OK_UPSERT)])  # the final restore retries both
    shopify.upsert_files(a, "t", {"sections/x.liquid": "1"}, final=True)
    assert len(calls) == 3


def test_a_job_record_shopify_dropped_does_not_stall_the_write():
    sent = []
    gone = (200, {"data": {"job": None}})
    a = _admin([(200, {"data": {"themeFilesUpsert": {"upsertedThemeFiles": [], "userErrors": [],
                                                     "job": {"id": "gid://shopify/Job/1", "done": False}}}})]
               + [gone] * 5, sent)
    shopify.upsert_files(a, "t", {"sections/x.liquid": "1"})
    assert len(sent) == 6  # 1 write + 5 empty polls, then the read-back is left to decide
    assert shopify.unconfirmed_writes == 1  # never seen finishing: settle() will treat the store as at risk


def _settle_run(tmp_path, monkeypatch, store, read_error=None):
    from sectioncloner import agent
    run, _ = _tools(tmp_path)
    good = {agent.SECTION_FILE: "{{ x }}", agent.TEMPLATE_FILE: '{"order": []}'}
    run.good, run.files, run.theme_id = good, good, "gid://shopify/OnlineStoreTheme/1"
    run.mutations = run.written_at = run.compared_at = 1
    uploads = []
    monkeypatch.setattr(shopify, "upsert_files",
                        lambda a, t, files, final=False: (uploads.append(final), store.update(files)))
    def read(a, t, names):
        if read_error:
            raise read_error
        return {n: store[n] for n in names if n in store}
    monkeypatch.setattr(shopify, "read_files", read)
    return run, good, uploads


def test_settle_leaves_a_store_that_already_holds_the_compared_version_alone(tmp_path, monkeypatch):
    from sectioncloner import agent
    store = {agent.SECTION_FILE: "{{ x }}", agent.TEMPLATE_FILE: '/* auto */\n{"order": []}'}
    run, good, uploads = _settle_run(tmp_path, monkeypatch, store)
    asyncio.run(agent.settle(run))
    assert uploads == [] and run.current()


def test_settle_overwrites_a_late_landing_earlier_write(tmp_path, monkeypatch):
    from sectioncloner import agent
    store = {agent.SECTION_FILE: "stale", agent.TEMPLATE_FILE: '{"order": [1]}'}  # an old write landed late
    run, good, uploads = _settle_run(tmp_path, monkeypatch, store)
    asyncio.run(agent.settle(run))
    assert uploads == [True] and store == good and run.current()


def test_settle_never_crashes_and_never_claims_an_unverified_store(tmp_path, monkeypatch):
    from sectioncloner import agent
    run, good, uploads = _settle_run(tmp_path, monkeypatch, {}, read_error=shopify.ShopifyTransientError("502"))
    asyncio.run(agent.settle(run))  # every read fails: logged, not raised
    run.finished = True
    assert not run.current() and agent.exit_code(run) == agent.EXIT_PARTIAL



def test_settle_closes_the_store_even_when_nothing_needs_writing(tmp_path, monkeypatch):
    from sectioncloner import agent
    store = {agent.SECTION_FILE: "{{ x }}", agent.TEMPLATE_FILE: '{"order": []}'}
    run, good, uploads = _settle_run(tmp_path, monkeypatch, store)
    asyncio.run(agent.settle(run))
    assert uploads == [] and shopify._closed.is_set()  # a cancelled tool's thread can't write after this


def test_an_unconfirmed_earlier_write_forces_the_restore_and_a_recheck(tmp_path, monkeypatch):
    from sectioncloner import agent
    store = {agent.SECTION_FILE: "{{ x }}", agent.TEMPLATE_FILE: '{"order": []}'}  # looks right right now...
    run, good, uploads = _settle_run(tmp_path, monkeypatch, store)
    monkeypatch.setattr(shopify, "unconfirmed_writes", 1)  # ...but an earlier write was never confirmed
    asyncio.run(agent.settle(run))
    assert uploads == [True] and store == good and run.current()



def test_an_accepted_write_whose_job_poll_fails_counts_as_unconfirmed():
    def handler(request):
        if b"themeFilesUpsert" in request.content:
            return httpx.Response(200, json={"data": {"themeFilesUpsert": {
                "upsertedThemeFiles": [], "userErrors": [], "job": {"id": "gid://shopify/Job/1", "done": False}}}})
        raise httpx.ReadTimeout("poll lost")
    a = shopify.Admin("s.myshopify.com", "t", client=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleep=lambda s: None)
    try:
        shopify.upsert_files(a, "t", {"sections/x.liquid": "1"})
    except shopify.ShopifyTransientError:
        pass
    else:
        raise AssertionError("a failed job poll must surface as a transient error")
    assert shopify.unconfirmed_writes == 1  # counted once, not twice



def test_a_rejected_request_that_never_ran_is_not_counted():
    sent = []
    a = _admin([(400, {"errors": "bad request"})], sent)
    try:
        shopify.upsert_files(a, "t", {"sections/x.liquid": "1"})
    except shopify.ShopifyError:
        pass
    assert shopify.unconfirmed_writes == 0


def test_settle_last_attempt_restores_once_more_if_a_late_write_keeps_landing(tmp_path, monkeypatch):
    from sectioncloner import agent
    store = {}
    run, good, uploads = _settle_run(tmp_path, monkeypatch, store)
    monkeypatch.setattr(shopify, "unconfirmed_writes", 1)
    real_sleep = asyncio.sleep
    async def late_write_lands(_):  # every grace period, an earlier stale write lands on top of the restore
        store.update({agent.SECTION_FILE: "stale"})
        await real_sleep(0)
    monkeypatch.setattr(agent.asyncio, "sleep", late_write_lands)
    asyncio.run(agent.settle(run))
    assert store == good and run.current()  # the final extra upload put the compared version back
    assert uploads.count(True) == agent.SETTLE_TRIES + 1


def test_compare_refuses_when_a_late_write_replaced_the_last_write(tmp_path, monkeypatch):
    from sectioncloner import agent
    run, tools = _tools(tmp_path)
    run.source = Source(url="u", digest="", images=[], texts=[], shot=Shot(height=1))
    run.files = {agent.SECTION_FILE: "mine", agent.TEMPLATE_FILE: '{"order": []}'}
    run.theme_id, run.product_url = "gid://shopify/OnlineStoreTheme/1", "https://x.myshopify.com/products/p"
    run.mutations = run.written_at = 1
    monkeypatch.setattr(shopify, "unconfirmed_writes", 1)
    monkeypatch.setattr(shopify, "read_files", lambda a, t, names: {agent.SECTION_FILE: "an older write",
                                                                    agent.TEMPLATE_FILE: '{"order": []}'})
    res = _call(tools["compare"])
    assert res.get("is_error") and "landed late" in res["content"][0]["text"]
    assert run.calls["compare"] == 0 and run.good is None  # free refusal, and nothing recorded as compared
    assert run.mutations == 2 and run.written_at == 1  # the live files are no longer the last written ones



def test_compare_refuses_when_a_late_write_lands_during_the_screenshot(tmp_path, monkeypatch):
    from sectioncloner import agent, browser
    run, tools = _tools(tmp_path)
    run.source = Source(url="u", digest="", images=[], texts=[], shot=Shot(height=1))
    mine = {agent.SECTION_FILE: "mine", agent.TEMPLATE_FILE: '{"order": []}'}
    store = dict(mine)
    run.files, run.theme_id = mine, "gid://shopify/OnlineStoreTheme/1"
    run.product_url = "https://x.myshopify.com/products/p"
    run.mutations = run.written_at = 1
    monkeypatch.setattr(shopify, "unconfirmed_writes", 1)
    monkeypatch.setattr(shopify, "read_files", lambda a, t, names: {n: store[n] for n in names})

    def screenshot_while_an_old_write_lands(*a, **k):
        store[agent.SECTION_FILE] = "an older write"
        return Shot(height=1)
    monkeypatch.setattr(browser, "shoot_rebuild", screenshot_while_an_old_write_lands)
    res = _call(tools["compare"])
    assert res.get("is_error") and run.good is None and run.compared_at is None


def test_a_non_http_error_after_sending_counts_as_unconfirmed():
    def handler(request):
        return httpx.Response(200, json={"no_data_key": True})  # a 200 with no `data`: KeyError after sending
    a = shopify.Admin("s.myshopify.com", "t", client=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleep=lambda s: None)
    try:
        shopify.upsert_files(a, "t", {"sections/x.liquid": "1"})
    except Exception:
        pass
    else:
        raise AssertionError("expected an error")
    assert shopify.unconfirmed_writes == 1
