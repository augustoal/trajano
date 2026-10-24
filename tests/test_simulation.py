"""End to end: scripted buyers and rule sellers negotiate over real MCP HTTP."""

import asyncio
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import uvicorn
from sqlmodel import Session, select

from agents.client import ToolCallError, TrajanoClient
from agents.simulate import simulate
from tests.factories import make_buyer, make_listing, make_seller
from tests.test_mcp_tools import _free_port, _wait_until_healthy
from trajano.app import create_app
from trajano.db import freeze_clock, get_engine
from trajano.domain import mandates
from trajano.domain.accounts import create_agent
from trajano.domain.catalog import as_priceable
from trajano.domain.pricing import landed_cost
from trajano.models import Listing, Offer, Order, Thread
from trajano.schemas import MandateDocument, OrderStatus, Role

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
SPEC = {
    "diameter": "M8",
    "length_mm": 30,
    "material": "steel",
    "grade": "8.8",
    "standard": "ISO 4017",
    "qty": 230,
    "ship_to": "US-OH",
    "need_by": "2026-11-20",
}
CHEAP_RULES = {
    "seller": "Cheap",
    "min_margin_percent": 2,
    "counter": {"max_discount_percent": 6, "meet_at_limit": True},
}
# Holds its price: a counter gets the opening price back as the final offer.
FIRM_RULES = {
    "seller": "Firm",
    "min_margin_percent": 10,
    "counter": {"max_discount_percent": 0, "meet_at_limit": True},
}


@pytest.fixture(autouse=True)
def frozen() -> None:
    freeze_clock(NOW)


@pytest.fixture
def server() -> Iterator[str]:
    port = _free_port()
    instance = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    try:
        _wait_until_healthy(port)
        yield f"http://127.0.0.1:{port}"
    finally:
        instance.should_exit = True
        thread.join(timeout=5)


def _mandate(session: Session, buyer, **limits: object) -> dict:
    owner, _key = create_agent(session, buyer.company_id, f"Owner {buyer.id}", [Role.BUYER])
    data: dict[str, object] = {
        "company_id": buyer.company_id,
        "agent_id": buyer.id,
        "max_unit_price": Decimal("1"),
        "max_total": Decimal("500"),
        "max_qty": 5000,
        "expires_at": NOW + timedelta(days=30),
    }
    data.update(limits)
    signed = mandates.issue(session, owner, MandateDocument(**data), f"m-{buyer.id}")  # type: ignore[arg-type]
    return signed.model_dump(mode="json")


def _market(session: Session) -> dict:
    """Two sellers of the same bolt. The cheap one has stock for four orders of 250."""
    cheap, cheap_key, cheap_store = make_seller(session, "Cheap")
    firm, firm_key, firm_store = make_seller(session, "Firm")
    tiers = [{"min_qty": 1, "unit_price": "0.1800"}, {"min_qty": 250, "unit_price": "0.1500"}]
    cheap_listing = make_listing(
        session, cheap_store, price_tiers=tiers, stock=1000, ships_from="US-IL"
    )
    firm_listing = make_listing(
        session,
        firm_store,
        sku="M8-30-88-F",
        price_tiers=[{"min_qty": 1, "unit_price": "0.2000"}],
        stock=100000,
        ships_from="US-CA",
        seller_notes=None,
    )
    # Not the spec: a different grade must never be quoted.
    make_listing(session, cheap_store, sku="M8-30-109", grade="10.9", stock=5000)
    return {
        "sellers": [
            {"name": "Cheap", "api_key": cheap_key, "rules": CHEAP_RULES},
            {"name": "Firm", "api_key": firm_key, "rules": FIRM_RULES},
        ],
        "cheap_listing": cheap_listing.id,
        "firm_listing": firm_listing.id,
    }


def _config(url: str, market: dict, buyers: list[dict], tmp_path) -> dict:
    import yaml

    sellers = []
    for seller in market["sellers"]:
        path = tmp_path / f"{seller['name']}.yaml"
        path.write_text(yaml.safe_dump(seller["rules"]))
        sellers.append(
            {
                "name": seller["name"],
                "api_key": seller["api_key"],
                "rules": str(path),
                "poll_seconds": 0.05,
            }
        )
    return {"url": url, "timeout_s": 60, "sellers": sellers, "buyers": buyers}


def test_ten_buyers_and_two_sellers_without_violations(server, session, tmp_path) -> None:
    market = _market(session)
    buyers = []
    for index in range(10):
        buyer, key = make_buyer(session, f"Buyer{index}")
        task = {
            "spec": SPEC,
            "mandate": _mandate(session, buyer),
            "quotes": 2,
            "wait_for": "DELIVERED",
            "poll_seconds": 0.05,
            "timeout_s": 45,
        }
        if index % 2:
            task["counter_percent"] = 5
        buyers.append({"name": f"buyer-{index}", "api_key": key, "task": task})
    session.commit()

    summary = asyncio.run(simulate(_config(server, market, buyers, tmp_path)))

    assert summary["violations"] == []
    assert summary["statuses"] == {"ordered": 10}, summary["buyers"]
    with Session(get_engine()) as db:
        cheap = db.get(Listing, market["cheap_listing"])
        orders = db.exec(select(Order)).all()
        assert len(orders) == 10
        assert len({order.buyer_agent_id for order in orders}) == 10
        assert all(order.status is OrderStatus.DELIVERED for order in orders)
        on_cheap = 0
        for order in orders:
            offer = db.get(Offer, order.offer_id)
            listing = db.get(Listing, offer.listing_id)
            assert listing.grade == "8.8"
            assert offer.unit_price >= listing.price_floor
            priced = landed_cost(
                as_priceable(listing), offer.qty, "US-OH", unit_price=offer.unit_price
            )
            assert offer.total == priced.total
            on_cheap += listing.id == cheap.id
        # Stock never goes negative and matches what was sold.
        assert on_cheap == 4
        assert cheap.stock == 1000 - 250 * on_cheap

    by_buyer = {result["buyer"]: result for result in summary["buyers"]}
    countered = [by_buyer[f"buyer-{i}"] for i in range(1, 10, 2)]
    # The cheap seller meets a 5% counter on its final offer; the firm seller holds.
    for result in countered:
        expected = "0.1425" if result["listing_id"] == market["cheap_listing"] else "0.2000"
        assert result["unit_price"] == expected
    # Every rfq got an answer, and the server never refused a rule seller's move.
    cheap_stats = summary["sellers"]["Cheap"]
    assert cheap_stats["offer"] + cheap_stats.get("decline", 0) >= 10
    for stats in summary["sellers"].values():
        assert not [name for name in stats if name.startswith("error:")], stats


def test_buyer_outcomes_without_a_deal(server, session, tmp_path) -> None:
    market = _market(session)
    tight, tight_key = make_buyer(session, "Tight")
    nothing, nothing_key = make_buyer(session, "Nothing")
    buyers = [
        {
            "name": "tight",
            "api_key": tight_key,
            "task": {
                "spec": SPEC,
                "mandate": _mandate(session, tight, max_total=Decimal("10")),
                "poll_seconds": 0.05,
            },
        },
        {
            "name": "nothing",
            "api_key": nothing_key,
            "task": {
                "spec": {**SPEC, "diameter": "M16"},
                "mandate": _mandate(session, nothing),
                "poll_seconds": 0.05,
            },
        },
    ]
    session.commit()

    summary = asyncio.run(simulate(_config(server, market, buyers, tmp_path)))

    statuses = {result["buyer"]: result["status"] for result in summary["buyers"]}
    assert statuses == {"tight": "over_mandate", "nothing": "no_match"}
    assert summary["orders"] == 0
    with Session(get_engine()) as db:
        assert db.exec(select(Thread)).all() == []


def test_a_firm_seller_declines_a_low_counter_and_the_buyer_takes_the_other(
    server, session, tmp_path
) -> None:
    market = _market(session)
    buyer, key = make_buyer(session, "Lowballer")
    firm = {**market["sellers"][1], "rules": {**FIRM_RULES, "counter": {"meet_at_limit": False}}}
    buyers = [
        {
            "name": "low",
            "api_key": key,
            "task": {
                "spec": {**SPEC, "ship_to": "US-CA"},
                "mandate": _mandate(session, buyer),
                "quotes": 2,
                "counter_percent": 20,
                "poll_seconds": 0.05,
            },
        }
    ]
    session.commit()
    config = _config(server, {**market, "sellers": [market["sellers"][0], firm]}, buyers, tmp_path)

    summary = asyncio.run(simulate(config))

    result = summary["buyers"][0]
    assert result["status"] == "ordered"
    assert result["listing_id"] == market["cheap_listing"]
    # 20% under 0.15 is past the cheap seller's 6%, so it meets at its limit.
    assert result["unit_price"] == "0.1410"
    assert summary["sellers"]["Firm"] == {"offer": 1, "decline": 1}
    with Session(get_engine()) as db:
        firm_thread = db.exec(
            select(Thread).where(Thread.listing_id == market["firm_listing"])
        ).one()
        assert firm_thread.status.value == "DECLINED"


def test_client_raises_structured_errors(server, session) -> None:
    _buyer, key = make_buyer(session)
    session.commit()

    async def scenario() -> ToolCallError:
        async with TrajanoClient(server, key) as client:
            with pytest.raises(ToolCallError) as caught:
                await client.call("get_thread", thread_id=999)
            return caught.value

    error = asyncio.run(scenario())
    assert error.code == "NOT_FOUND"
    assert error.retryable is False

    async def bad_key() -> None:
        async with TrajanoClient(server, "tj_wrong") as client:
            await client.call("get_inbox")

    with pytest.raises(ToolCallError, match="UNAUTHORIZED"):
        asyncio.run(bad_key())


class _Block:
    def __init__(self, **fields: object) -> None:
        self.__dict__.update(fields)


class _ScriptedModel:
    """Stands in for the Messages API: a fixed buy, reading ids from earlier tool results."""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def create(self, **kwargs: object) -> _Block:
        self.requests.append(kwargs)
        results = [
            json.loads(block["content"])
            for message in kwargs["messages"]  # type: ignore[union-attr]
            if message["role"] == "user" and isinstance(message["content"], list)
            for block in message["content"]
            if block["content"] != "ok"
        ]
        step = len(self.requests)
        if step == 1:
            return self._use(
                "search_listings",
                {k: SPEC[k] for k in ("qty", "ship_to", "need_by", "length_mm", "grade")},
            )
        if step == 2:
            hit = results[-1]["hits"][0]["listing"]["id"]
            return self._use(
                "request_quote",
                {
                    **{k: SPEC[k] for k in ("qty", "ship_to", "need_by")},
                    "listing_id": hit,
                    "idempotency_key": "llm-rfq",
                },
            )
        if step == 3:
            return self._use("wait", {"seconds": 1})
        if step == 4:
            return self._use("get_thread", {"thread_id": results[-1]["thread_id"]})
        if step == 5:
            offer = results[-1]["thread"]["messages"][-1]["payload"]
            return self._use(
                "accept_offer",
                {
                    "offer_id": offer["offer_id"],
                    "offer_hash": offer["hash"],
                    "mandate": {"forged": True},
                    "idempotency_key": "llm-acc",
                },
            )
        return self._use(
            "finish",
            {"status": "ordered", "order_id": results[-1]["order_id"], "summary": "bought"},
        )

    def _use(self, name: str, arguments: dict) -> _Block:
        block = _Block(type="tool_use", id=f"t{len(self.requests)}", name=name, input=arguments)
        return _Block(stop_reason="tool_use", content=[block])


def test_llm_agent_loop_over_mcp_with_a_scripted_model(server, session, tmp_path) -> None:
    from agents import llm_agent, rule_seller

    market = _market(session)
    buyer, key = make_buyer(session, "Claude")
    mandate = _mandate(session, buyer)
    session.commit()
    model = _ScriptedModel()

    async def scenario() -> dict:
        stop = asyncio.Event()
        rules = rule_seller.SellerRules.model_validate(CHEAP_RULES)
        seller = asyncio.create_task(
            rule_seller.run(
                server, market["sellers"][0]["api_key"], rules, stop=stop, poll_seconds=0.05
            )
        )
        task = {"id": "llm-1", "spec": SPEC, "mandate": mandate}
        result = await llm_agent.run(server, key, "buyer", task, messages_api=model)
        stop.set()
        await seller
        return result

    result = asyncio.run(scenario())

    assert result["status"] == "ordered"
    assert result["tool_errors"] == []
    names = {tool["name"] for tool in model.requests[0]["tools"]}
    assert {"search_listings", "accept_offer", "wait", "finish"} <= names
    assert model.requests[0]["model"] == llm_agent.DEFAULT_MODEL
    brief = model.requests[0]["messages"][0]["content"]
    assert "max_total" in brief and mandate["signature"] not in brief
    with Session(get_engine()) as db:
        order = db.get(Order, result["order_id"])
        # The forged mandate the model passed was replaced by the signed one.
        assert order is not None and order.mandate_id == mandate["id"]
