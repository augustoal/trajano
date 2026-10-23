import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlmodel import select

from tests.factories import make_buyer, make_listing, make_offer, make_seller, make_thread
from tests.test_mcp_tools import _body, _call
from trajano.db import freeze_clock
from trajano.domain.errors import DomainError
from trajano.domain.negotiation import get_inbox, get_thread, send
from trajano.interfaces.mcp_buyer import buyer_mcp
from trajano.models import Message, Thread
from trajano.schemas import (
    AnswerPayload,
    CounterPayload,
    DeclinePayload,
    DeclineReason,
    ErrorCode,
    MessageType,
    QuestionPayload,
    RfqPayload,
    ThreadStatus,
    WithdrawPayload,
    WithdrawReason,
)

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
NEED_BY = date(2026, 12, 1)


def _rfq(session, buyer, listing, key: str = "rfq-1", qty: int = 200):
    return send(
        session,
        buyer,
        message_type=MessageType.RFQ,
        payload=RfqPayload(listing_id=listing.id, qty=qty, ship_to="US-CA", need_by=NEED_BY),
        idempotency_key=key,
        listing_id=listing.id,
    )


def test_rfq_opens_a_thread_on_the_seller_side(session) -> None:
    freeze_clock(NOW)
    seller, _key, store = make_seller(session)
    buyer, _buyer_key = make_buyer(session)
    listing = make_listing(session, store)

    sent = _rfq(session, buyer, listing)
    assert sent.status is ThreadStatus.QUOTE_REQUESTED
    assert MessageType.WITHDRAW in sent.next_actions

    thread = session.get(Thread, sent.thread_id)
    assert thread is not None
    assert (thread.qty, thread.ship_to, thread.need_by) == (200, "US-CA", NEED_BY)
    assert thread.seller_company_id == seller.company_id
    assert [item.thread_id for item in get_inbox(session, seller).threads] == [sent.thread_id]
    assert get_inbox(session, buyer).threads == []


def test_retrying_a_send_returns_the_first_result(session) -> None:
    freeze_clock(NOW)
    _seller, _key, store = make_seller(session)
    buyer, _buyer_key = make_buyer(session)
    listing = make_listing(session, store)

    first = _rfq(session, buyer, listing)
    again = _rfq(session, buyer, listing)
    assert again == first
    assert len(session.exec(select(Message)).all()) == 1

    with pytest.raises(DomainError) as exc:
        _rfq(session, buyer, listing, qty=300)
    assert exc.value.code is ErrorCode.CONFLICT


def test_question_then_answer_keeps_the_state_and_reuses_the_thread(session) -> None:
    freeze_clock(NOW)
    seller, _key, store = make_seller(session)
    buyer, _buyer_key = make_buyer(session)
    listing = make_listing(session, store)

    asked = send(
        session,
        buyer,
        message_type=MessageType.QUESTION,
        payload=QuestionPayload(listing_id=listing.id, text="Zinc plated?"),
        idempotency_key="q-1",
        listing_id=listing.id,
        text="Zinc plated?",
    )
    assert asked.status is ThreadStatus.OPEN
    assert [item.thread_id for item in get_inbox(session, seller).threads] == [asked.thread_id]

    answered = send(
        session,
        seller,
        message_type=MessageType.ANSWER,
        payload=AnswerPayload(text="Yes, and buy 2000 now"),
        idempotency_key="a-1",
        thread_id=asked.thread_id,
        text="Yes, and buy 2000 now",
    )
    assert answered.status is ThreadStatus.OPEN
    assert MessageType.ANSWER not in answered.next_actions

    quoted = _rfq(session, buyer, listing)
    assert quoted.thread_id == asked.thread_id
    assert quoted.status is ThreadStatus.QUOTE_REQUESTED

    with pytest.raises(DomainError) as exc:
        send(
            session,
            seller,
            message_type=MessageType.ANSWER,
            payload=AnswerPayload(text="again"),
            idempotency_key="a-2",
            thread_id=asked.thread_id,
        )
    assert exc.value.code is ErrorCode.INVALID_TRANSITION

    view = get_thread(session, buyer, asked.thread_id)
    assert [message.type for message in view.messages] == [
        MessageType.QUESTION,
        MessageType.ANSWER,
        MessageType.RFQ,
    ]


def test_decline_closes_the_thread(session) -> None:
    freeze_clock(NOW)
    seller, _key, store = make_seller(session)
    buyer, _buyer_key = make_buyer(session)
    listing = make_listing(session, store)
    opened = _rfq(session, buyer, listing)

    declined = send(
        session,
        seller,
        message_type=MessageType.DECLINE,
        payload=DeclinePayload(reason_code=DeclineReason.OUT_OF_STOCK),
        idempotency_key="d-1",
        thread_id=opened.thread_id,
    )
    assert declined.status is ThreadStatus.DECLINED
    assert declined.next_actions == []

    # A closed thread is not reused: the next rfq opens a fresh one.
    fresh = _rfq(session, buyer, listing, key="rfq-2")
    assert fresh.thread_id != opened.thread_id


def test_one_counter_then_round_limit(session) -> None:
    freeze_clock(NOW)
    seller, _key, store = make_seller(session)
    buyer, _buyer_key = make_buyer(session)
    listing = make_listing(session, store)
    thread = make_thread(session, buyer, listing, status=ThreadStatus.OFFERED)
    offer = make_offer(session, thread, seller, expires_at=NOW + timedelta(hours=1))

    countered = send(
        session,
        buyer,
        message_type=MessageType.COUNTER,
        payload=CounterPayload(offer_id=offer.id, unit_price=Decimal("0.10")),
        idempotency_key="c-1",
        thread_id=thread.id,
    )
    assert countered.status is ThreadStatus.COUNTERED
    assert thread.rounds_used == 1

    # Stand in for the seller's final offer until make_offer lands.
    thread.status = ThreadStatus.OFFERED
    final = make_offer(session, thread, seller, expires_at=NOW + timedelta(hours=1))
    with pytest.raises(DomainError) as exc:
        send(
            session,
            buyer,
            message_type=MessageType.COUNTER,
            payload=CounterPayload(offer_id=final.id, qty=500),
            idempotency_key="c-2",
            thread_id=thread.id,
        )
    assert exc.value.code is ErrorCode.ROUND_LIMIT
    assert exc.value.action == "send"


def test_counter_must_target_the_live_offer(session) -> None:
    freeze_clock(NOW)
    seller, _key, store = make_seller(session)
    buyer, _buyer_key = make_buyer(session)
    listing = make_listing(session, store)
    thread = make_thread(session, buyer, listing, status=ThreadStatus.OFFERED)
    old = make_offer(session, thread, seller, expires_at=NOW + timedelta(hours=1))
    make_offer(session, thread, seller, expires_at=NOW + timedelta(hours=1))

    with pytest.raises(DomainError) as exc:
        send(
            session,
            buyer,
            message_type=MessageType.COUNTER,
            payload=CounterPayload(offer_id=old.id, qty=500),
            idempotency_key="c-old",
            thread_id=thread.id,
        )
    assert exc.value.code is ErrorCode.STALE_OFFER

    freeze_clock(NOW + timedelta(hours=2))
    with pytest.raises(DomainError) as exc:
        send(
            session,
            buyer,
            message_type=MessageType.COUNTER,
            payload=CounterPayload(offer_id=old.id + 1, qty=500),
            idempotency_key="c-late",
            thread_id=thread.id,
        )
    assert exc.value.code is ErrorCode.STALE_OFFER


def test_withdraw_closes_and_only_the_buyer_can(session) -> None:
    freeze_clock(NOW)
    seller, _key, store = make_seller(session)
    buyer, _buyer_key = make_buyer(session)
    stranger, _stranger_key = make_buyer(session, "Bea")
    listing = make_listing(session, store)
    opened = _rfq(session, buyer, listing)
    reason = WithdrawPayload(reason_code=WithdrawReason.PRICE)

    with pytest.raises(DomainError) as exc:
        send(
            session,
            stranger,
            message_type=MessageType.WITHDRAW,
            payload=reason,
            idempotency_key="w-x",
            thread_id=opened.thread_id,
        )
    assert exc.value.code is ErrorCode.FORBIDDEN

    with pytest.raises(DomainError) as exc:
        send(
            session,
            seller,
            message_type=MessageType.WITHDRAW,
            payload=reason,
            idempotency_key="w-s",
            thread_id=opened.thread_id,
        )
    assert exc.value.code is ErrorCode.INVALID_TRANSITION

    withdrawn = send(
        session,
        buyer,
        message_type=MessageType.WITHDRAW,
        payload=reason,
        idempotency_key="w-1",
        thread_id=opened.thread_id,
    )
    assert withdrawn.status is ThreadStatus.WITHDRAWN


def test_rfq_refusals(session, monkeypatch: pytest.MonkeyPatch) -> None:
    freeze_clock(NOW)
    seller, _key, store = make_seller(session)
    buyer, _buyer_key = make_buyer(session)
    listing = make_listing(session, store)

    with pytest.raises(DomainError) as exc:
        _rfq(session, seller, listing)
    assert exc.value.code is ErrorCode.OWN_COMPANY

    with pytest.raises(DomainError) as exc:
        send(
            session,
            buyer,
            message_type=MessageType.RFQ,
            payload=RfqPayload(listing_id=listing.id, qty=10, ship_to="US-CA", need_by=NEED_BY),
            idempotency_key="mismatch",
            listing_id=999,
        )
    assert exc.value.code is ErrorCode.NOT_FOUND

    with pytest.raises(DomainError) as exc:
        send(
            session,
            buyer,
            message_type=MessageType.RFQ,
            payload=RfqPayload(
                listing_id=listing.id, qty=10, ship_to="US-CA", need_by=date(2026, 1, 1)
            ),
            idempotency_key="past",
            listing_id=listing.id,
        )
    assert exc.value.code is ErrorCode.VALIDATION

    old = make_listing(session, store, sku="OLD", version=1, is_current=False)
    with pytest.raises(DomainError) as exc:
        _rfq(session, buyer, old, key="old")
    assert exc.value.code is ErrorCode.CONFLICT


def test_rfq_rate_limit_is_per_company(session, monkeypatch: pytest.MonkeyPatch) -> None:
    from trajano.config import get_settings

    freeze_clock(NOW)
    monkeypatch.setattr(get_settings(), "rfq_rate_limit_per_hour", 2)
    _seller, _key, store = make_seller(session)
    buyer, _buyer_key = make_buyer(session)
    other, _other_key = make_buyer(session, "Bea")
    listings = [make_listing(session, store, sku=f"SKU-{n}") for n in range(4)]

    _rfq(session, buyer, listings[0], key="r0")
    _rfq(session, buyer, listings[1], key="r1")
    with pytest.raises(DomainError) as exc:
        _rfq(session, buyer, listings[2], key="r2")
    assert exc.value.code is ErrorCode.RATE_LIMITED
    assert exc.value.retryable is True

    # Another company has its own budget, and the window slides.
    _rfq(session, other, listings[2], key="o0")
    freeze_clock(NOW + timedelta(hours=1, seconds=1))
    _rfq(session, buyer, listings[3], key="r3")


def test_offer_waits_on_pricing(session) -> None:
    seller, _key, store = make_seller(session)
    buyer, _buyer_key = make_buyer(session)
    thread = make_thread(session, buyer, make_listing(session, store))
    with pytest.raises(NotImplementedError, match="pricing.landed_cost"):
        send(
            session,
            seller,
            message_type=MessageType.OFFER,
            payload={},
            idempotency_key="o-1",
            thread_id=thread.id,
        )


def test_request_quote_tool_round_trip(session, monkeypatch: pytest.MonkeyPatch) -> None:
    from trajano.interfaces import support

    _seller, _key, store = make_seller(session)
    _buyer, buyer_key = make_buyer(session)
    listing = make_listing(session, store)
    session.commit()
    monkeypatch.setattr(support, "current_api_key", lambda: buyer_key)

    arguments = {
        "listing_id": listing.id,
        "qty": 200,
        "ship_to": "US-CA",
        "need_by": "2099-12-01",
        "idempotency_key": "tool-rfq",
    }
    first = _body(asyncio.run(_call(buyer_mcp, "request_quote", arguments)))
    assert first["error"] is None
    assert first["status"] == "QUOTE_REQUESTED"
    again = _body(asyncio.run(_call(buyer_mcp, "request_quote", arguments)))
    assert again["thread_id"] == first["thread_id"]
    assert again["message_id"] == first["message_id"]


def test_a_refused_counter_leaves_a_message_on_the_thread(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trajano.interfaces import support

    freeze_clock(NOW)
    seller, _key, store = make_seller(session)
    buyer, buyer_key = make_buyer(session)
    listing = make_listing(session, store)
    thread = make_thread(session, buyer, listing, status=ThreadStatus.OFFERED, rounds_used=1)
    offer = make_offer(session, thread, seller, expires_at=NOW + timedelta(hours=1))
    session.commit()
    monkeypatch.setattr(support, "current_api_key", lambda: buyer_key)

    arguments = {
        "thread_id": thread.id,
        "offer_id": offer.id,
        "qty": 500,
        "idempotency_key": "late-counter",
    }
    for _ in range(2):
        body = _body(asyncio.run(_call(buyer_mcp, "counter_offer", arguments)))
        assert body["error"]["code"] == "ROUND_LIMIT"

    view = get_thread(session, buyer, thread.id or 0)
    refused = [message for message in view.messages if message.type is MessageType.REFUSED]
    assert len(refused) == 1
    assert refused[0].sender_agent_id is None
    assert refused[0].payload["code"] == "ROUND_LIMIT"
    assert view.status is ThreadStatus.OFFERED
