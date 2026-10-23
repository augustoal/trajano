import asyncio
from decimal import Decimal

import pytest
from sqlmodel import select

from tests.factories import make_listing, make_seller
from tests.test_mcp_tools import _body, _call
from trajano.domain.catalog import (
    create_listing,
    create_store,
    get_listing,
    get_listing_for_seller,
    update_listing,
)
from trajano.domain.errors import DomainError
from trajano.interfaces.mcp_seller import seller_mcp
from trajano.models import Listing
from trajano.schemas import ErrorCode, ListingWrite, PriceTier


def _write(**overrides: object) -> ListingWrite:
    data: dict[str, object] = {
        "sku": "M10-40-109",
        "diameter": "M10",
        "length_mm": Decimal("40"),
        "material": "steel",
        "grade": "10.9",
        "standard": "ISO 4017",
        "price_tiers": [
            PriceTier(min_qty=1, unit_price=Decimal("0.30")),
            PriceTier(min_qty=500, unit_price=Decimal("0.21")),
        ],
        "price_floor": Decimal("0.15"),
        "moq": 100,
        "pack_size": 100,
        "stock": 5000,
        "lead_time_days": 3,
        "ships_from": "US-TX",
    }
    data.update(overrides)
    return ListingWrite(**data)  # type: ignore[arg-type]


def test_create_store_replays_the_same_key(session) -> None:
    seller, _key, _store = make_seller(session)
    first = create_store(session, seller.id or 0, seller.company_id, " Bolts ", "store-1")
    again = create_store(session, seller.id or 0, seller.company_id, "Bolts", "store-1")
    assert first == again
    assert first.name == "Bolts"
    assert first.company_id == seller.company_id

    with pytest.raises(DomainError) as exc:
        create_store(session, seller.id or 0, seller.company_id, "Other", "store-1")
    assert exc.value.code is ErrorCode.CONFLICT


def test_create_listing_stores_version_one_with_the_floor(session) -> None:
    seller, _key, store = make_seller(session)
    created = create_listing(
        session, seller.id or 0, seller.company_id, store.id or 0, _write(), "list-1"
    )
    assert created.version == 1
    assert created.price_floor == Decimal("0.1500")
    assert [tier.min_qty for tier in created.price_tiers] == [1, 500]

    row = session.get(Listing, created.id)
    assert row is not None
    assert row.price_tiers[0] == {"min_qty": 1, "unit_price": "0.3000"}
    assert "price_floor" not in get_listing(session, created.id).model_dump()

    replay = create_listing(
        session, seller.id or 0, seller.company_id, store.id or 0, _write(), "list-1"
    )
    assert replay.id == created.id

    with pytest.raises(DomainError) as exc:
        create_listing(
            session, seller.id or 0, seller.company_id, store.id or 0, _write(), "list-2"
        )
    assert exc.value.code is ErrorCode.CONFLICT


def test_create_listing_refuses_another_company_store(session) -> None:
    seller, _key, _store = make_seller(session, "Helix")
    _other, _other_key, other_store = make_seller(session, "Rival")
    with pytest.raises(DomainError) as exc:
        create_listing(
            session, seller.id or 0, seller.company_id, other_store.id or 0, _write(), "x"
        )
    assert exc.value.code is ErrorCode.FORBIDDEN
    assert exc.value.action == "create_listing"


def test_update_listing_adds_a_version_and_retires_the_old_one(session) -> None:
    seller, _key, store = make_seller(session)
    first = create_listing(
        session, seller.id or 0, seller.company_id, store.id or 0, _write(), "v1"
    )
    second = update_listing(
        session, seller.id or 0, seller.company_id, first.id, _write(stock=10), "v2"
    )
    assert second.version == 2
    assert second.id != first.id
    assert second.stock == 10

    old = session.get(Listing, first.id)
    assert old is not None and old.is_current is False and old.stock == 5000
    current = session.exec(
        select(Listing).where(Listing.sku == "M10-40-109", Listing.is_current == True)  # noqa: E712
    ).all()
    assert [row.id for row in current] == [second.id]

    with pytest.raises(DomainError) as exc:
        update_listing(session, seller.id or 0, seller.company_id, first.id, _write(), "v3")
    assert exc.value.code is ErrorCode.CONFLICT
    assert str(second.id) in exc.value.message


def test_update_listing_keeps_the_sku_and_the_owner(session) -> None:
    seller, _key, store = make_seller(session, "Helix")
    rival, _rival_key, _rival_store = make_seller(session, "Rival")
    listing = make_listing(session, store)

    with pytest.raises(DomainError) as exc:
        update_listing(session, seller.id or 0, seller.company_id, listing.id or 0, _write(), "sku")
    assert exc.value.code is ErrorCode.VALIDATION

    with pytest.raises(DomainError) as exc:
        update_listing(
            session,
            rival.id or 0,
            rival.company_id,
            listing.id or 0,
            _write(sku=listing.sku),
            "theft",
        )
    assert exc.value.code is ErrorCode.FORBIDDEN


def test_only_the_owner_sees_the_floor(session) -> None:
    seller, _key, store = make_seller(session, "Helix")
    rival, _rival_key, _rival_store = make_seller(session, "Rival")
    listing = make_listing(session, store)
    own = get_listing_for_seller(session, seller.company_id, listing.id or 0)
    assert own.price_floor == Decimal("0.0500")
    with pytest.raises(DomainError) as exc:
        get_listing_for_seller(session, rival.company_id, listing.id or 0)
    assert exc.value.code is ErrorCode.FORBIDDEN
    with pytest.raises(DomainError) as exc:
        get_listing_for_seller(session, seller.company_id, 999)
    assert exc.value.code is ErrorCode.NOT_FOUND


def test_seller_tools_create_a_store_and_a_listing(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trajano.interfaces import support

    _seller, key, _store = make_seller(session)
    session.commit()
    monkeypatch.setattr(support, "current_api_key", lambda: key)

    store = _body(
        asyncio.run(_call(seller_mcp, "create_store", {"name": "Depot", "idempotency_key": "s"}))
    )
    assert store["error"] is None
    listing = _body(
        asyncio.run(
            _call(
                seller_mcp,
                "create_listing",
                {
                    "store_id": store["store"]["id"],
                    "listing": _write().model_dump(mode="json"),
                    "idempotency_key": "l",
                },
            )
        )
    )
    assert listing["error"] is None
    assert listing["listing"]["version"] == 1
    assert Decimal(listing["listing"]["price_floor"]) == Decimal("0.15")


def test_seller_get_listing_tool_shows_the_floor_to_the_owner_only(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trajano.interfaces import support

    _seller, key, store = make_seller(session, "Helix")
    _rival, rival_key, _rival_store = make_seller(session, "Rival")
    listing = make_listing(session, store)
    session.commit()

    monkeypatch.setattr(support, "current_api_key", lambda: key)
    own = _body(asyncio.run(_call(seller_mcp, "get_listing", {"listing_id": listing.id})))
    assert own["error"] is None
    assert Decimal(own["listing"]["price_floor"]) == Decimal("0.05")

    monkeypatch.setattr(support, "current_api_key", lambda: rival_key)
    other = _body(asyncio.run(_call(seller_mcp, "get_listing", {"listing_id": listing.id})))
    assert other["error"]["code"] == "FORBIDDEN"
    assert other["listing"] is None
