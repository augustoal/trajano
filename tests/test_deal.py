import asyncio
import itertools
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlmodel import select

from tests.factories import make_buyer, make_listing, make_seller
from tests.test_mcp_tools import _body, _call
from trajano.db import freeze_clock
from trajano.domain import mandates
from trajano.domain.accounts import create_agent
from trajano.domain.catalog import as_priceable
from trajano.domain.errors import DomainError
from trajano.domain.negotiation import get_thread, send
from trajano.domain.pricing import landed_cost
from trajano.interfaces.mcp_seller import seller_mcp
from trajano.models import Agent, Listing, Message, Offer, Order
from trajano.schemas import (
    AcceptPayload,
    CounterPayload,
    DeclineReason,
    DeliveredPayload,
    ErrorCode,
    MandateDocument,
    MessageType,
    OfferDraft,
    OrderConfirmPayload,
    OrderRejectPayload,
    OrderStatus,
    RfqPayload,
    Role,
    ShippedPayload,
    ThreadStatus,
)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
NEED_BY = date(2026, 12, 1)


@pytest.fixture(autouse=True)
def frozen() -> None:
    freeze_clock(NOW)


def _mandate(session, buyer: Agent, **limits: object) -> MandateDocument:
    owner, _key = create_agent(session, buyer.company_id, f"Owner {buyer.id}", [Role.BUYER])
    data: dict[str, object] = {
        "company_id": buyer.company_id,
        "agent_id": buyer.id,
        "max_unit_price": Decimal("1"),
        "max_total": Decimal("1000"),
        "max_qty": 10000,
        "expires_at": NOW + timedelta(days=30),
    }
    data.update(limits)
    return mandates.issue(session, owner, MandateDocument(**data), f"mandate-{buyer.id}")  # type: ignore[arg-type]


def _draft(listing: Listing, qty: int = 230, unit_price: Decimal | None = None) -> OfferDraft:
    landed = landed_cost(as_priceable(listing), qty, "US-CA", unit_price=unit_price)
    return OfferDraft(
        qty=qty,
        unit_price=landed.unit_price,
        total=landed.total,
        delivery_date=date(2026, 11, 20),
        lines=landed.lines,
    )


def _rfq(session, buyer: Agent, listing: Listing, qty: int = 230) -> int:
    sent = send(
        session,
        buyer,
        message_type=MessageType.RFQ,
        payload=RfqPayload(listing_id=listing.id, qty=qty, ship_to="US-CA", need_by=NEED_BY),
        idempotency_key=f"rfq-{listing.id}",
        listing_id=listing.id,
    )
    return sent.thread_id


def _offer(session, seller: Agent, thread_id: int, draft: OfferDraft, key: str = "offer-1"):
    return send(
        session,
        seller,
        message_type=MessageType.OFFER,
        payload=draft,
        idempotency_key=key,
        thread_id=thread_id,
    )


def _latest_offer(session, thread_id: int) -> Offer:
    return session.exec(
        select(Offer).where(Offer.thread_id == thread_id).order_by(Offer.id.desc())  # type: ignore[union-attr]
    ).first()


def _accept(session, buyer: Agent, offer: Offer, mandate: MandateDocument, key: str = "acc"):
    return send(
        session,
        buyer,
        message_type=MessageType.ACCEPT,
        payload=AcceptPayload(
            offer_id=offer.id, hash=offer.hash, mandate=mandate, idempotency_key=key
        ),
        idempotency_key=key,
    )


def _stock(session, listing: Listing) -> int:
    session.refresh(listing)
    return listing.stock


def test_full_deal_from_rfq_to_delivery(session) -> None:
    seller, _key, store = make_seller(session)
    buyer, _buyer_key = make_buyer(session)
    listing = make_listing(session, store)
    mandate = _mandate(session, buyer)
    thread_id = _rfq(session, buyer, listing)

    first = _offer(session, seller, thread_id, _draft(listing))
    assert first.status is ThreadStatus.OFFERED
    opening = _latest_offer(session, thread_id)
    assert opening.qty == 250
    assert opening.final is False
    assert opening.expires_at == NOW + timedelta(hours=48)
    offer_message = get_thread(session, buyer, thread_id).messages[-1]
    assert offer_message.payload["offer_id"] == opening.id
    assert offer_message.payload["hash"] == opening.hash

    countered = send(
        session,
        buyer,
        message_type=MessageType.COUNTER,
        payload=CounterPayload(offer_id=opening.id, unit_price=Decimal("0.10")),
        idempotency_key="counter",
        thread_id=thread_id,
    )
    assert countered.status is ThreadStatus.COUNTERED

    _offer(session, seller, thread_id, _draft(listing, unit_price=Decimal("0.11")), "offer-2")
    final = _latest_offer(session, thread_id)
    assert final.final is True
    assert final.unit_price == Decimal("0.11")

    accepted = _accept(session, buyer, final, mandate)
    assert accepted.status is ThreadStatus.ACCEPTED
    assert accepted.order_id is not None
    assert _stock(session, listing) == 1000 - 250

    order_id = accepted.order_id
    steps = [
        (MessageType.ORDER_CONFIRM, OrderConfirmPayload(order_id=order_id), ThreadStatus.CONFIRMED),
        (
            MessageType.SHIPPED,
            ShippedPayload(order_id=order_id, tracking="1Z999"),
            ThreadStatus.SHIPPED,
        ),
        (MessageType.DELIVERED, DeliveredPayload(order_id=order_id), ThreadStatus.DELIVERED),
    ]
    for message_type, payload, status in steps:
        sent = send(
            session,
            seller,
            message_type=message_type,
            payload=payload,
            idempotency_key=f"step-{message_type.value}",
        )
        assert sent.status is status
        assert sent.order_id == order_id
    assert session.get(Order, order_id).status is OrderStatus.DELIVERED

    view = get_thread(session, seller, thread_id)
    accept_message = next(m for m in view.messages if m.type is MessageType.ACCEPT)
    assert "mandate" not in accept_message.payload
    assert accept_message.payload["order_id"] == order_id
    offer_message = next(m for m in view.messages if m.type is MessageType.OFFER)
    assert offer_message.payload["hash"] == opening.hash


def test_offer_refusals(session) -> None:
    seller, _key, store = make_seller(session)
    buyer, _buyer_key = make_buyer(session)
    listing = make_listing(session, store)
    thread_id = _rfq(session, buyer, listing)

    keys = itertools.count()

    def code(draft: OfferDraft) -> ErrorCode:
        # A fresh key per attempt: id() of a collected draft can be reused.
        with pytest.raises(DomainError) as exc:
            _offer(session, seller, thread_id, draft, key=f"bad-{next(keys)}")
        return exc.value.code

    good = _draft(listing)
    assert code(_draft(listing, unit_price=Decimal("0.01"))) is ErrorCode.BELOW_FLOOR
    assert code(good.model_copy(update={"total": good.total + 1})) is ErrorCode.VALIDATION
    assert code(good.model_copy(update={"lines": good.lines[:1]})) is ErrorCode.VALIDATION
    assert code(good.model_copy(update={"ship_to": "US-NY"})) is ErrorCode.VALIDATION
    late = NOW + timedelta(hours=49)
    assert code(good.model_copy(update={"expires_at": late})) is ErrorCode.VALIDATION
    assert code(_draft(listing, qty=5000)) is ErrorCode.OUT_OF_STOCK

    with pytest.raises(DomainError) as exc:
        _offer(session, buyer, thread_id, good, key="buyer-offer")
    assert exc.value.code is ErrorCode.INVALID_TRANSITION


def test_offer_quotes_the_current_listing_version(session) -> None:
    seller, _key, store = make_seller(session)
    buyer, _buyer_key = make_buyer(session)
    old = make_listing(session, store)
    thread_id = _rfq(session, buyer, old)
    old.is_current = False
    session.add(old)
    session.flush()
    newer = make_listing(
        session, store, version=2, price_tiers=[{"min_qty": 1, "unit_price": "0.2000"}]
    )

    _offer(session, seller, thread_id, _draft(newer))
    assert _latest_offer(session, thread_id).listing_id == newer.id


def test_accept_refusals_leave_the_thread_offered(session) -> None:
    seller, _key, store = make_seller(session)
    buyer, _buyer_key = make_buyer(session)
    stranger, _stranger_key = make_buyer(session, "Bea")
    listing = make_listing(session, store)
    thread_id = _rfq(session, buyer, listing)
    _offer(session, seller, thread_id, _draft(listing))
    offer = _latest_offer(session, thread_id)

    with pytest.raises(DomainError) as exc:
        send(
            session,
            buyer,
            message_type=MessageType.ACCEPT,
            payload=AcceptPayload(
                offer_id=offer.id,
                hash="f" * 64,
                mandate=_mandate(session, buyer),
                idempotency_key="wrong-hash",
            ),
            idempotency_key="wrong-hash",
        )
    assert exc.value.code is ErrorCode.STALE_OFFER

    tight = _mandate(session, stranger, max_total=Decimal("1"))
    with pytest.raises(DomainError) as exc:
        _accept(session, stranger, offer, tight, key="stranger")
    assert exc.value.code is ErrorCode.NOT_FOUND

    freeze_clock(NOW + timedelta(hours=49))
    with pytest.raises(DomainError) as exc:
        _accept(session, buyer, offer, _mandate(session, buyer), key="late")
    assert exc.value.code is ErrorCode.STALE_OFFER
    assert session.exec(select(Order)).all() == []


def test_reject_returns_the_stock_and_others_cannot_touch_the_order(session) -> None:
    seller, _key, store = make_seller(session)
    rival, _rival_key, _rival_store = make_seller(session, "Rival")
    buyer, _buyer_key = make_buyer(session)
    listing = make_listing(session, store)
    thread_id = _rfq(session, buyer, listing)
    _offer(session, seller, thread_id, _draft(listing))
    accepted = _accept(session, buyer, _latest_offer(session, thread_id), _mandate(session, buyer))

    with pytest.raises(DomainError) as exc:
        send(
            session,
            rival,
            message_type=MessageType.ORDER_CONFIRM,
            payload=OrderConfirmPayload(order_id=accepted.order_id),
            idempotency_key="rival",
        )
    assert exc.value.code is ErrorCode.NOT_FOUND

    rejected = send(
        session,
        seller,
        message_type=MessageType.ORDER_REJECT,
        payload=OrderRejectPayload(
            order_id=accepted.order_id, reason_code=DeclineReason.OUT_OF_STOCK
        ),
        idempotency_key="reject",
    )
    assert rejected.status is ThreadStatus.REJECTED
    assert _stock(session, listing) == 1000


def test_a_refused_mandate_leaves_a_refused_message(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trajano.interfaces import mcp_buyer, support

    seller, _key, store = make_seller(session)
    buyer, buyer_key = make_buyer(session)
    listing = make_listing(session, store)
    thread_id = _rfq(session, buyer, listing)
    _offer(session, seller, thread_id, _draft(listing))
    offer = _latest_offer(session, thread_id)
    tight = _mandate(session, buyer, max_total=Decimal("1"))
    session.commit()
    monkeypatch.setattr(support, "current_api_key", lambda: buyer_key)

    result = _body(
        asyncio.run(
            _call(
                mcp_buyer.buyer_mcp,
                "accept_offer",
                {
                    "offer_id": offer.id,
                    "offer_hash": offer.hash,
                    "mandate": tight.model_dump(mode="json"),
                    "idempotency_key": "too-much",
                },
            )
        )
    )
    assert result["error"]["code"] == "MANDATE_INVALID"
    refused = session.exec(
        select(Message).where(Message.thread_id == thread_id, Message.type == MessageType.REFUSED)
    ).all()
    assert [message.payload["code"] for message in refused] == ["MANDATE_INVALID"]


def test_make_offer_tool(session, monkeypatch: pytest.MonkeyPatch) -> None:
    from trajano.interfaces import support

    _seller, seller_key, store = make_seller(session)
    buyer, _buyer_key = make_buyer(session)
    listing = make_listing(session, store)
    thread_id = _rfq(session, buyer, listing)
    session.commit()
    monkeypatch.setattr(support, "current_api_key", lambda: seller_key)

    draft = _draft(listing).model_dump(mode="json", exclude_none=True)
    result = _body(
        asyncio.run(
            _call(
                seller_mcp,
                "make_offer",
                {**draft, "thread_id": thread_id, "idempotency_key": "tool-offer"},
            )
        )
    )
    assert result["error"] is None
    assert result["status"] == "OFFERED"


def test_the_buyer_can_accept_from_the_thread_view_alone(session) -> None:
    seller, _key, store = make_seller(session)
    buyer, _buyer_key = make_buyer(session)
    listing = make_listing(session, store)
    mandate = _mandate(session, buyer)
    thread_id = _rfq(session, buyer, listing)
    _offer(session, seller, thread_id, _draft(listing))

    view = get_thread(session, buyer, thread_id)
    [message] = [m for m in view.messages if m.type is MessageType.OFFER]
    stored = _latest_offer(session, thread_id)
    assert message.payload["offer_id"] == stored.id
    assert message.payload["hash"] == stored.hash

    accepted = send(
        session,
        buyer,
        message_type=MessageType.ACCEPT,
        payload=AcceptPayload(
            offer_id=message.payload["offer_id"],
            hash=message.payload["hash"],
            mandate=mandate,
            idempotency_key="from-view",
        ),
        idempotency_key="from-view",
    )
    assert accepted.order_id is not None
