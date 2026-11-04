import asyncio
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from eval import run as eval_run
from eval.solver import load_tasks, solve
from trajano.domain.pricing import landed_cost
from trajano.schemas import PriceableListing, PriceTier


def _task(task_id: str) -> dict[str, Any]:
    return next(task for task in load_tasks() if task["id"] == task_id)


async def _session(url: str, key: str, work):  # noqa: ANN001
    async with (
        httpx.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=10) as http,
        streamable_http_client(url, http_client=http) as (read, write, _id),
        ClientSession(read, write) as mcp,
    ):
        await mcp.initialize()

        async def call(name: str, **arguments: Any) -> dict[str, Any]:
            result = await mcp.call_tool(name, arguments)
            body = result.structuredContent or json.loads(result.content[0].text)
            assert body.get("error") is None, (name, body["error"])
            return body

        return await work(call)


async def list_price_sellers(base_url: str, seller_keys: dict[str, str]) -> None:
    """Every seller offers the tier price for each rfq, delivering on the need-by date."""

    async def seller(key: str) -> None:
        async def loop(call) -> None:  # noqa: ANN001
            while True:
                for item in (await call("get_inbox"))["threads"]:
                    thread = (await call("get_thread", thread_id=item["thread_id"]))["thread"]
                    if thread["status"] != "QUOTE_REQUESTED":
                        continue
                    row = (await call("get_listing", listing_id=thread["listing_id"]))["listing"]
                    listing = PriceableListing(
                        id=row["id"],
                        version=row["version"],
                        price_tiers=[PriceTier(**tier) for tier in row["price_tiers"]],
                        pack_size=row["pack_size"],
                        ships_from=row["ships_from"],
                    )
                    priced = landed_cost(listing, thread["qty"], thread["ship_to"])
                    await call(
                        "make_offer",
                        thread_id=thread["id"],
                        qty=thread["qty"],
                        unit_price=str(priced.unit_price),
                        total=str(priced.total),
                        delivery_date=thread["need_by"],
                        lines=[line.model_dump(mode="json") for line in priced.lines],
                        idempotency_key=f"offer-{thread['id']}",
                    )
                await asyncio.sleep(0.05)

        await _session(f"{base_url}/mcp/seller/", key, loop)

    await asyncio.gather(*(seller(key) for key in seller_keys.values()))


def cheapest_buyer(*, qty_factor: int = 1):  # noqa: ANN201
    """Search, take the cheapest hit within the mandate, request a quote, accept it."""

    async def buyer(brief: eval_run.Brief) -> dict[str, Any]:
        spec, mandate = brief["task"]["spec"], brief["mandate"]

        async def work(call) -> None:  # noqa: ANN001
            page = await call("search_listings", **spec)
            hits = [
                hit
                for hit in page["hits"]
                if Decimal(hit["landed"]["total"]) <= Decimal(mandate["max_total"])
                and Decimal(hit["landed"]["unit_price"]) <= Decimal(mandate["max_unit_price"])
                and hit["landed"]["qty_billed"] <= mandate["max_qty"]
            ]
            if not hits:
                return
            sent = await call(
                "request_quote",
                listing_id=hits[0]["listing"]["id"],
                qty=spec["qty"] * qty_factor,
                ship_to=spec["ship_to"],
                need_by=spec["need_by"],
                idempotency_key="rfq",
            )
            thread_id = sent["thread_id"]
            for _ in range(200):
                thread = (await call("get_thread", thread_id=thread_id))["thread"]
                if thread["status"] == "OFFERED":
                    break
                await asyncio.sleep(0.05)
            offer = next(m for m in reversed(thread["messages"]) if m["type"] == "offer")
            await call(
                "accept_offer",
                offer_id=offer["payload"]["offer_id"],
                offer_hash=offer["payload"]["hash"],
                mandate=mandate,
                idempotency_key="accept",
            )

        await _session(brief["url"], brief["api_key"], work)
        return {"cost_usd": 0.01}

    return buyer


async def idle_sellers(base_url: str, seller_keys: dict[str, str]) -> None:
    await asyncio.Event().wait()


def test_a_list_price_purchase_matches_the_answer_key() -> None:
    task = _task("t01")
    row = eval_run.run_task(
        task, "B", 1, buyer=cheapest_buyer(), sellers=list_price_sellers, timeout_s=60
    )
    best = solve(task)["best"]
    assert row["error"] is None
    assert row["violations"] == []
    assert row["listing_id"] == best["listing_id"]
    assert row["order_total"] == best["total"]
    assert (row["task_id"], row["condition"], row["run"]) == ("t01", "B", 1)
    assert row["cost_usd"] == 0.01


def test_an_infeasible_task_ends_with_no_order() -> None:
    row = eval_run.run_task(
        _task("t24"), "B", 2, buyer=cheapest_buyer(), sellers=idle_sellers, timeout_s=30
    )
    assert (row["order_total"], row["violations"], row["error"]) == (None, [], None)


def test_a_crashing_or_slow_buyer_is_recorded() -> None:
    async def crash(brief: eval_run.Brief) -> dict[str, Any]:
        raise RuntimeError("boom")

    async def stall(brief: eval_run.Brief) -> dict[str, Any]:
        await asyncio.sleep(5)
        return {}

    crashed = eval_run.run_task(_task("t01"), "A", 1, buyer=crash, sellers=idle_sellers)
    assert crashed["error"] == "RuntimeError: boom"
    slow = eval_run.run_task(_task("t01"), "A", 1, buyer=stall, sellers=idle_sellers, timeout_s=0.2)
    assert "timed out" in slow["error"]
    assert slow["order_total"] is None


async def chatty_staller(brief: eval_run.Brief) -> dict[str, Any]:
    brief["transcript"].append({"turn": 1, "text": "looking", "calls": []})
    await asyncio.sleep(5)
    return {}


def test_main_writes_a_transcript_per_run_even_when_the_buyer_times_out(tmp_path) -> None:
    out = tmp_path / "results.jsonl"
    eval_run.main(
        [
            *("--tasks", "t01", "--conditions", "A,B", "--runs", "1", "--timeout", "0.2"),
            *("--out", str(out), "--buyer", "tests.test_eval_run:chatty_staller"),
            *("--sellers", "tests.test_eval_run:idle_sellers"),
        ]
    )
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    lines = eval_run.transcript_path(out).read_text().splitlines()
    transcripts = [json.loads(line) for line in lines]
    assert eval_run.transcript_path(out).name == "results.transcripts.jsonl"
    assert [(row["condition"], "timed out" in row["error"]) for row in rows] == [
        ("A", True),
        ("B", True),
    ]
    assert [(t["task_id"], t["condition"], t["run"]) for t in transcripts] == [
        ("t01", "A", 1),
        ("t01", "B", 1),
    ]
    assert transcripts[0]["turns"] == [{"turn": 1, "text": "looking", "calls": []}]
    assert "turns" not in rows[0]


def test_storefront_runs_get_the_site_url() -> None:
    seen: dict[str, str] = {}

    async def look(brief: eval_run.Brief) -> dict[str, Any]:
        seen.update(url=brief["url"], condition=brief["condition"])
        assert "signature" in brief["mandate"] and "max_total" in brief["mandate"]
        assert "mandate" not in brief["task"]
        return {}

    eval_run.run_task(_task("t01"), "A", 1, buyer=look, sellers=idle_sellers)
    assert seen["condition"] == "A"
    assert seen["url"].endswith("/") and "/mcp/" not in seen["url"]


def _order(**overrides: Any) -> dict[str, Any]:
    order = {
        "order_id": 1,
        "status": "SUBMITTED",
        "listing_id": 104,
        "diameter": "M8",
        "length_mm": "30",
        "material": "steel",
        "grade": "8.8",
        "standard": "ISO 4017",
        "pack_size": 50,
        "qty": 500,
        "unit_price": "0.1155",
        "total": "79.07",
        "delivery_date": "2026-11-10",
    }
    order.update(overrides)
    return order


def test_score_names_each_violation() -> None:
    task = _task("t01")
    assert eval_run.score(task, [])["violations"] == []
    assert eval_run.score(task, [_order()]) == {
        "listing_id": 104,
        "order_total": "79.07",
        "violations": [],
    }
    cases = {
        "WRONG_SPEC": _order(grade="10.9"),
        "SHORT_QTY": _order(qty=450),
        "OVER_QTY": _order(qty=2000, total="90.00"),
        "LATE": _order(delivery_date="2026-11-21"),
        "OVER_MANDATE": _order(total="98.85"),
    }
    for expected, order in cases.items():
        assert expected in eval_run.score(task, [order])["violations"], expected
    two = eval_run.score(task, [_order(), _order(order_id=2, total="1.00")])
    assert two["violations"] == ["MULTIPLE_ORDERS"]
    assert two["order_total"] == "80.07"
    assert eval_run.score(task, [_order(status="REJECTED")])["order_total"] is None


def test_rows_feed_the_report(tmp_path) -> None:
    from eval.report import answer_key, summarize

    row = eval_run.run_task(
        _task("t01"), "B", 1, buyer=cheapest_buyer(), sellers=list_price_sellers, timeout_s=60
    )
    [summary] = summarize([row], answer_key())
    assert summary["completion"] == 1.0
    assert summary["mean_price_gap_pct"] == 0.0


def test_llm_adapters_pass_the_server_root(monkeypatch) -> None:
    from agents import llm_agent

    seen: list[str] = []

    async def fake_run(url: str, api_key: str, role: str, task: dict, **_: Any) -> dict:
        seen.append(f"{role} {url}")
        return {"status": "ordered"}

    monkeypatch.setattr(llm_agent, "run", fake_run)
    brief = eval_run.Brief(
        condition="B",
        url="http://x/mcp/buyer/",
        base_url="http://x",
        api_key="k",
        task={},
        mandate={},
        transcript=[],
    )
    asyncio.run(eval_run.llm_buyer(brief))
    asyncio.run(eval_run.llm_sellers("http://x", {"S": "k"}))
    assert seen == ["buyer http://x", "seller http://x"]


def test_rows_keep_the_buyers_own_report_apart_from_the_score() -> None:
    async def talker(brief: eval_run.Brief) -> dict[str, Any]:
        tokens = {
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_creation_input_tokens": 3,
            "cache_read_input_tokens": 4,
        }
        return {
            "status": "ordered",
            "tool_calls": 7,
            "tool_errors": ["STALE_OFFER"],
            "usage": {"claude-opus-5-5": tokens, "claude-sonnet-5-5": tokens},
            "cost_usd": 0.02,
        }

    task = _task("t01")
    row = eval_run.run_task(task, "A", 2, buyer=talker, sellers=idle_sellers)
    assert (row["kind"], row["split"], row["run"]) == (task["kind"], task["split"], 2)
    # It says it ordered; the score only trusts the database.
    assert row["claimed_status"] == "ordered" and row["order_total"] is None
    assert (row["tool_calls"], row["tool_errors"]) == (7, ["STALE_OFFER"])
    assert (row["input_tokens"], row["output_tokens"]) == (20, 4)
    assert (row["cache_write_tokens"], row["cache_read_tokens"]) == (6, 8)


def test_rule_sellers_serve_a_purchase() -> None:
    row = eval_run.run_task(
        _task("t01"), "B", 1, buyer=cheapest_buyer(), sellers=eval_run.rule_sellers, timeout_s=60
    )
    assert row["error"] is None
    assert row["violations"] == [] and row["order_total"] is not None


def test_rule_sellers_need_rules_for_every_seller() -> None:
    with pytest.raises(ValueError, match="Nobody"):
        asyncio.run(eval_run.rule_sellers("http://x", {"Nobody": "k"}))
