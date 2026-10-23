import csv
import io
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlmodel import Session, select

from tests.factories import make_buyer, make_listing, make_priced_offer, make_seller
from trajano.db import freeze_clock, get_engine
from trajano.domain import mandates, orders
from trajano.domain.accounts import create_agent
from trajano.domain.errors import DomainError
from trajano.models import Agent, Listing, Offer, Order
from trajano.schemas import (
    DeclineReason,
    ErrorCode,
    MandateDocument,
    OrderStatus,
    Role,
    quantize_money,
)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


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


def _deal(session, **listing: object):
    seller, _key, store = make_seller(session)
    buyer, _key = make_buyer(session)
    item = make_listing(session, store, **listing)
    offer = make_priced_offer(session, buyer, item)
    return seller, buyer, item, offer, _mandate(session, buyer)


def _stock(session, listing: Listing) -> int:
    session.expire_all()
    current = session.exec(
        select(Listing).where(
            Listing.store_id == listing.store_id,
            Listing.sku == listing.sku,
            Listing.is_current.is_(True),
        )
    ).one()
    return current.stock


def _code(call) -> DomainError:
    with pytest.raises(DomainError) as exc:
        call()
    return exc.value


def test_accept_creates_a_submitted_order_and_takes_the_stock(session) -> None:
    seller, buyer, item, offer, mandate = _deal(session)
    order = orders.accept(session, buyer, offer, mandate, "buy-1")
    assert order.status is OrderStatus.SUBMITTED
    assert (order.buyer_agent_id, order.buyer_company_id) == (buyer.id, buyer.company_id)
    assert order.seller_company_id == seller.company_id
    assert order.mandate_id == mandate.id
    assert _stock(session, item) == 1000 - offer.qty


def test_accept_replays_by_key(session) -> None:
    _seller, buyer, item, offer, mandate = _deal(session)
    first = orders.accept(session, buyer, offer, mandate, "buy-1")
    for _ in range(3):
        assert orders.accept(session, buyer, offer, mandate, "buy-1").id == first.id
    assert _stock(session, item) == 1000 - offer.qty
    assert len(session.exec(select(Order)).all()) == 1

    other = make_priced_offer(session, buyer, item)
    assert _code(lambda: orders.accept(session, buyer, other, mandate, "buy-1")).code is (
        ErrorCode.CONFLICT
    )


@pytest.mark.parametrize("problem", ["expired", "tampered", "stock", "mandate"])
def test_refusals_leave_stock_and_orders_alone(session, problem: str) -> None:
    _seller, buyer, item, offer, mandate = _deal(session)
    expected = ErrorCode.STALE_OFFER
    if problem == "expired":
        freeze_clock(offer.expires_at + timedelta(seconds=1))
    elif problem == "tampered":
        offer.total = Decimal(offer.total) - Decimal("1")
        session.add(offer)
        session.flush()
    elif problem == "stock":
        item.stock = offer.qty - 1
        session.add(item)
        session.flush()
    else:
        mandate = _mandate(session, buyer, max_total=Decimal(offer.total) - Decimal("0.01"))
        expected = ErrorCode.MANDATE_INVALID
    before = _stock(session, item)
    error = _code(lambda: orders.accept(session, buyer, offer, mandate, "buy-1"))
    assert error.code is expected
    assert error.action is not None
    assert _stock(session, item) == before
    assert session.exec(select(Order)).all() == []


def test_only_the_threads_buyer_can_accept(session) -> None:
    _seller, _buyer, _item, offer, _mandate_doc = _deal(session)
    stranger, _key = make_buyer(session, "Eve")
    mandate = _mandate(session, stranger)
    error = _code(lambda: orders.accept(session, stranger, offer, mandate, "x"))
    assert error.code is ErrorCode.NOT_FOUND


def test_stock_comes_off_the_current_version(session) -> None:
    _seller, buyer, old, offer, mandate = _deal(session)
    old.is_current = False
    session.add(old)
    session.flush()
    store = old.store_id
    newer = Listing(
        **old.model_dump(exclude={"id", "version", "is_current", "stock", "created_at"}),
        version=2,
        is_current=True,
        stock=300,
    )
    session.add(newer)
    session.flush()
    orders.accept(session, buyer, offer, mandate, "buy-1")
    assert newer.store_id == store
    assert _stock(session, newer) == 300 - offer.qty


def test_seller_lifecycle(session) -> None:
    seller, buyer, item, offer, mandate = _deal(session)
    order = orders.accept(session, buyer, offer, mandate, "buy-1")
    order_id = order.id or 0

    assert _code(lambda: orders.ship(session, seller, order_id, "T1", "s")).code is (
        ErrorCode.INVALID_TRANSITION
    )
    assert _code(lambda: orders.confirm(session, buyer, order_id, "c")).code is ErrorCode.NOT_FOUND

    assert orders.confirm(session, seller, order_id, "c").confirmed_at == NOW
    assert orders.confirm(session, seller, order_id, "c2").status is OrderStatus.CONFIRMED
    assert (
        _code(lambda: orders.reject(session, seller, order_id, DeclineReason.OTHER, "r")).code
        is ErrorCode.INVALID_TRANSITION
    )
    shipped = orders.ship(session, seller, order_id, "1Z999", "s")
    assert (shipped.status, shipped.tracking) == (OrderStatus.SHIPPED, "1Z999")
    assert orders.ship(session, seller, order_id, "1Z999", "s2").shipped_at == NOW
    assert orders.deliver(session, seller, order_id, "d").status is OrderStatus.DELIVERED
    assert orders.deliver(session, seller, order_id, "d2").delivered_at == NOW
    assert _stock(session, item) == 1000 - offer.qty


def test_reject_restores_the_stock(session) -> None:
    seller, buyer, item, offer, mandate = _deal(session)
    order = orders.accept(session, buyer, offer, mandate, "buy-1")
    rejected = orders.reject(session, seller, order.id or 0, DeclineReason.OUT_OF_STOCK, "r")
    assert (rejected.status, rejected.reason_code) == (OrderStatus.REJECTED, "OUT_OF_STOCK")
    orders.reject(session, seller, order.id or 0, DeclineReason.OUT_OF_STOCK, "r2")
    assert _stock(session, item) == 1000


def test_order_visibility(session) -> None:
    seller, buyer, _item, offer, mandate = _deal(session)
    order_id = orders.accept(session, buyer, offer, mandate, "buy-1").id or 0
    assert orders.get_order(session, buyer, order_id).status is OrderStatus.SUBMITTED
    assert orders.get_order(session, seller, order_id).buyer_agent_id == buyer.id
    colleague, _key = create_agent(session, buyer.company_id, "Bob", [Role.BUYER])
    stranger, _key, _store = make_seller(session, "Rival")
    for outsider in (colleague, stranger):
        error = _code(lambda who=outsider: orders.get_order(session, who, order_id))
        assert error.code is ErrorCode.NOT_FOUND
    assert _code(lambda: orders.export_order_csv(session, stranger, order_id)).code is (
        ErrorCode.NOT_FOUND
    )
    assert orders.export_order_csv(session, colleague, order_id)


def test_csv_has_one_row_per_cost_line_and_a_total(session) -> None:
    seller, buyer, _item, offer, mandate = _deal(session, sku="=HYPERLINK(1)")
    order_id = orders.accept(session, buyer, offer, mandate, "buy-1").id or 0
    rows = list(csv.DictReader(io.StringIO(orders.export_order_csv(session, seller, order_id))))
    assert [row["line"] for row in rows] == [line["kind"] for line in offer.lines] + ["total"]
    assert rows[-1]["amount"] == quantize_money(Decimal(offer.total))
    assert sum(Decimal(row["amount"]) for row in rows[:-1]) == Decimal(rows[-1]["amount"])
    assert rows[0]["sku"] == "'=HYPERLINK(1)"
    assert rows[0]["created_at"] == "2026-11-01T12:00:00Z"
    assert rows[0]["qty"] == str(offer.qty)


def _race(work: list) -> list[object]:
    results: list[object] = [None] * len(work)
    barrier = threading.Barrier(len(work))

    def run(index: int) -> None:
        with Session(get_engine(), expire_on_commit=False) as db:
            barrier.wait()
            try:
                results[index] = work[index](db)
                db.commit()
            except DomainError as exc:
                db.rollback()
                results[index] = exc.code

    threads = [threading.Thread(target=run, args=(index,)) for index in range(len(work))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def test_two_buyers_racing_for_the_last_stock_get_one_order(session) -> None:
    _seller, _key, store = make_seller(session)
    item = make_listing(session, store, stock=100)
    deals = []
    for name in ("Ada", "Bea"):
        buyer, _key = make_buyer(session, name)
        deals.append((buyer, make_priced_offer(session, buyer, item), _mandate(session, buyer)))
    session.commit()

    def accept(buyer: Agent, offer: Offer, mandate: MandateDocument):
        return lambda db: orders.accept(db, buyer, db.get(Offer, offer.id), mandate, "go").id

    results = _race([accept(*deal) for deal in deals])
    assert sorted(results, key=str).count(ErrorCode.STALE_OFFER) == 1
    assert len([result for result in results if isinstance(result, int)]) == 1
    assert _stock(session, item) == 0


def test_the_same_accept_retried_concurrently_makes_one_order(session) -> None:
    _seller, buyer, item, offer, mandate = _deal(session)
    session.commit()
    work = [lambda db: orders.accept(db, buyer, db.get(Offer, offer.id), mandate, "go").id] * 5
    results = _race(work)
    assert len(set(results)) == 1
    assert isinstance(results[0], int)
    assert _stock(session, item) == 1000 - offer.qty
