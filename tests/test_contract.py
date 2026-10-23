import ast
import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel

from tests.factories import make_buyer, make_listing, make_seller
from trajano.db import freeze_clock, utc_now
from trajano.domain.idempotency import begin, complete, request_fingerprint
from trajano.models import Listing, Message, Store, Thread
from trajano.schemas import (
    CostLine,
    MandateDocument,
    MessageType,
    OfferFingerprint,
    ThreadStatus,
    mandate_signing_bytes,
    offer_canonical_json,
    offer_hash,
)

ROOT = Path(__file__).resolve().parents[1]
_OFFER_JSON = (
    '{"currency":"USD","delivery_date":"2026-11-01","expires_at":"2026-11-02T15:04:05Z",'
    '"final":false,"lines":[{"amount":"12.50","description":"M8x30","kind":"goods"},'
    '{"amount":"3.00","description":"zone US","kind":"freight"}],"listing_id":10,'
    '"listing_version":2,"qty":100,"ship_to":"US-CA","total":"15.50","unit_price":"0.1250"}'
)
_MANDATE_JSON = (
    '{"agent_id":2,"category":"fasteners","company_id":1,"currency":"USD",'
    '"expires_at":"2026-12-01T00:00:00Z","max_qty":1000,"max_total":"500.00",'
    '"max_unit_price":"0.2000"}'
)


def test_offer_hash_is_the_canonical_json() -> None:
    offer = OfferFingerprint(
        listing_id=10,
        listing_version=2,
        lines=[
            CostLine(kind="goods", description="M8x30", amount=Decimal("12.5")),
            CostLine(kind="freight", description="zone US", amount=Decimal("3")),
        ],
        unit_price=Decimal("0.125"),
        qty=100,
        total=Decimal("15.5"),
        currency="USD",
        ship_to="US-CA",
        delivery_date=date(2026, 11, 1),
        expires_at=datetime(2026, 11, 2, 15, 4, 5, 999, tzinfo=UTC),
        final=False,
    )
    assert offer_canonical_json(offer) == _OFFER_JSON
    assert offer_hash(offer) == hashlib.sha256(_OFFER_JSON.encode()).hexdigest()


def test_mandate_signing_bytes_exclude_the_signature() -> None:
    document = MandateDocument(
        id=99,
        company_id=1,
        agent_id=2,
        category="fasteners",
        max_unit_price=Decimal("0.2"),
        max_total=Decimal("500"),
        max_qty=1000,
        currency="USD",
        expires_at=datetime(2026, 12, 1, tzinfo=UTC),
        signature="ab" * 32,
    )
    assert mandate_signing_bytes(document) == _MANDATE_JSON.encode()


def test_tables() -> None:
    assert set(SQLModel.metadata.tables) == {
        "companies",
        "agents",
        "stores",
        "listings",
        "threads",
        "messages",
        "offers",
        "orders",
        "mandates",
        "approval_requests",
        "audit_events",
        "idempotency_keys",
    }


def test_clock_and_money_round_trip(session) -> None:
    moment = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    freeze_clock(moment)
    _agent, _key, store = make_seller(session)
    listing = make_listing(session, store)
    session.commit()
    stored = session.get(Listing, listing.id)
    assert stored is not None
    assert stored.created_at == moment
    assert stored.price_floor == Decimal("0.0500")
    assert utc_now() == moment


def test_foreign_keys_and_one_current_version(session) -> None:
    with pytest.raises(IntegrityError):
        session.add(Store(company_id=999999, name="ghost"))
        session.flush()
    session.rollback()
    _agent, _key, store = make_seller(session)
    make_listing(session, store, sku="M8")
    with pytest.raises(IntegrityError):
        make_listing(session, store, sku="M8", version=2, is_current=True)
    session.rollback()


def test_message_idempotency_key_is_unique_per_sender(session) -> None:
    buyer, _key = make_buyer(session)
    _seller, _seller_key, store = make_seller(session)
    listing = make_listing(session, store)
    thread = Thread(
        listing_id=listing.id,
        buyer_agent_id=buyer.id,
        seller_store_id=store.id,
        seller_company_id=store.company_id,
        status=ThreadStatus.OPEN,
    )
    session.add(thread)
    session.flush()
    session.add(_message(thread.id or 0, buyer.id, "same"))
    session.flush()
    session.add(_message(thread.id or 0, buyer.id, "same"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_idempotency_replays_the_original_body(session) -> None:
    agent, _key = make_buyer(session)
    fingerprint = request_fingerprint({"qty": 2})
    assert begin(session, agent.id or 0, "key-1", fingerprint) is None
    complete(session, agent.id or 0, "key-1", {"order_id": 5})
    assert begin(session, agent.id or 0, "key-1", fingerprint) == {"order_id": 5}
    with pytest.raises(Exception) as exc:
        begin(session, agent.id or 0, "key-1", request_fingerprint({"qty": 3}))
    from trajano.domain.errors import DomainError

    assert isinstance(exc.value, DomainError)
    assert exc.value.code.value == "CONFLICT"
    assert exc.value.retryable is False


def test_clients_do_not_import_the_server() -> None:
    allowed = {
        ROOT / "agents" / "rule_seller.py": {("trajano.domain.pricing",)},
        ROOT / "eval" / "solver.py": {("trajano.domain.pricing",)},
    }
    for package in ("agents", "eval"):
        for path in (ROOT / package).rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                modules: list[str] = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    modules.append(node.module)
                elif isinstance(node, ast.Import):
                    modules.extend(alias.name for alias in node.names)
                for module in modules:
                    pricing = module == "trajano.domain.pricing" or module.startswith(
                        "trajano.domain.pricing."
                    )
                    if pricing:
                        assert path in allowed
                        assert (module,) in allowed[path]
                        continue
                    assert not module.startswith("trajano.domain")
                    assert not module.startswith("trajano.models")


def test_domain_does_not_call_datetime_now() -> None:
    for path in (ROOT / "trajano").rglob("*.py"):
        if path.name == "db.py":
            continue
        text = path.read_text()
        assert "datetime.now" not in text
        assert "datetime.utcnow" not in text


def _message(thread_id: int, sender_id: int | None, key: str) -> Message:
    return Message(
        thread_id=thread_id,
        sender_agent_id=sender_id,
        type=MessageType.QUESTION,
        payload={"listing_id": 1},
        text="hi",
        idempotency_key=key,
    )
