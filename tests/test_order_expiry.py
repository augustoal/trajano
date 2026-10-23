from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlmodel import select

from tests.factories import make_buyer, make_listing, make_priced_offer, make_seller
from trajano.db import freeze_clock
from trajano.domain import mandates, negotiation, orders
from trajano.domain.accounts import create_agent
from trajano.domain.errors import DomainError
from trajano.models import Listing, Message, Thread
from trajano.schemas import ErrorCode, MandateDocument, MessageType, OrderStatus, Role, ThreadStatus

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
WINDOW = timedelta(hours=24)


@pytest.fixture(autouse=True)
def frozen() -> None:
    freeze_clock(NOW)


def _accepted(session):
    seller, _key, store = make_seller(session)
    buyer, _key = make_buyer(session)
    item = make_listing(session, store)
    offer = make_priced_offer(session, buyer, item)
    owner, _key = create_agent(session, buyer.company_id, "Owner", [Role.BUYER])
    mandate = mandates.issue(
        session,
        owner,
        MandateDocument(
            company_id=buyer.company_id,
            agent_id=buyer.id or 0,
            max_unit_price=Decimal("1"),
            max_total=Decimal("1000"),
            max_qty=10000,
            expires_at=NOW + timedelta(days=30),
        ),
        "mandate",
    )
    order = orders.accept(session, buyer, offer, mandate, "buy")
    thread = session.get(Thread, offer.thread_id)
    assert thread is not None
    thread.status = ThreadStatus.ACCEPTED
    session.add(thread)
    session.flush()
    return seller, buyer, item, thread, order


def _expired_messages(session, thread: Thread) -> list[Message]:
    return session.exec(
        select(Message).where(Message.thread_id == thread.id, Message.type == MessageType.EXPIRED)
    ).all()


def test_an_unconfirmed_order_expires_after_the_window(session) -> None:
    _seller, _buyer, item, thread, order = _accepted(session)
    assert session.get(Listing, item.id).stock == 900

    freeze_clock(NOW + WINDOW)
    assert orders.expire_unconfirmed(session, thread) is False
    assert order.status is OrderStatus.SUBMITTED

    freeze_clock(NOW + WINDOW + timedelta(seconds=1))
    assert orders.expire_unconfirmed(session, thread) is True
    assert (order.status, order.reason_code) == (OrderStatus.REJECTED, orders.EXPIRED_REASON)
    assert order.rejected_at == NOW + WINDOW + timedelta(seconds=1)
    assert thread.status is ThreadStatus.EXPIRED
    session.expire_all()
    assert session.get(Listing, item.id).stock == 1000
    [message] = _expired_messages(session, thread)
    assert message.sender_agent_id is None
    assert message.idempotency_key == f"expire-order-{order.id}"

    assert orders.expire_unconfirmed(session, thread) is False
    assert len(_expired_messages(session, thread)) == 1


def test_a_confirmed_order_never_expires(session) -> None:
    seller, _buyer, _item, thread, order = _accepted(session)
    orders.confirm(session, seller, order.id or 0, "c")
    freeze_clock(NOW + WINDOW * 3)
    assert orders.expire_unconfirmed(session, thread) is False
    assert order.status is OrderStatus.CONFIRMED


def test_confirming_after_the_window_is_refused(session) -> None:
    seller, _buyer, _item, _thread, order = _accepted(session)
    freeze_clock(NOW + WINDOW + timedelta(minutes=1))
    with pytest.raises(DomainError) as exc:
        orders.confirm(session, seller, order.id or 0, "c")
    assert exc.value.code is ErrorCode.INVALID_TRANSITION
    assert order.status is OrderStatus.SUBMITTED


def test_reads_expire_the_order(session) -> None:
    seller, buyer, item, thread, order = _accepted(session)
    freeze_clock(NOW + WINDOW + timedelta(hours=1))
    view = negotiation.get_thread(session, buyer, thread.id or 0)
    assert view.status is ThreadStatus.EXPIRED
    assert order.status is OrderStatus.REJECTED
    session.expire_all()
    assert session.get(Listing, item.id).stock == 1000


def test_the_inbox_expires_the_order(session) -> None:
    seller, _buyer, _item, thread, order = _accepted(session)
    freeze_clock(NOW + WINDOW + timedelta(hours=1))
    negotiation.get_inbox(session, seller)
    assert thread.status is ThreadStatus.EXPIRED
    assert order.reason_code == orders.EXPIRED_REASON
