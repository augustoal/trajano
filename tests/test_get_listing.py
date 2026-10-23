from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tests.factories import make_listing, make_seller
from trajano.db import freeze_clock
from trajano.domain.catalog import as_priceable, get_listing, search
from trajano.domain.errors import DomainError
from trajano.domain.pricing import landed_cost
from trajano.schemas import ErrorCode, ExclusionReason, SearchSpec


def test_get_listing_hides_the_floor_and_sorts_tiers(session) -> None:
    _agent, _key, store = make_seller(session)
    listing = make_listing(session, store)
    public = get_listing(session, listing.id or 0)
    dumped = public.model_dump()
    assert "price_floor" not in dumped
    assert public.price_tiers[0].min_qty == 1
    assert public.price_tiers[1].unit_price == Decimal("0.1200")
    assert public.seller_notes is not None and "buy 2000" in public.seller_notes
    assert public.store_name == store.name
    assert public.diameter == "M8"
    assert "0.0500" not in public.model_dump_json()


def test_missing_listing_is_not_found(session) -> None:
    with pytest.raises(DomainError) as exc:
        get_listing(session, 999)
    assert exc.value.code is ErrorCode.NOT_FOUND
    assert exc.value.action is None


def test_search_requires_the_buying_spec(session) -> None:
    with pytest.raises(DomainError) as exc:
        search(session, SearchSpec(qty=10, ship_to="US-CA"))
    assert exc.value.code is ErrorCode.SPEC_INCOMPLETE
    assert exc.value.message == "missing length_mm, need_by"


def test_search_ranks_by_landed_cost_and_explains_exclusions(session) -> None:
    freeze_clock(datetime(2026, 11, 1, 12, 0, tzinfo=UTC))
    _agent, _key, store = make_seller(session)
    pricey = make_listing(session, store, sku="PRICEY")
    cheap = make_listing(
        session, store, sku="CHEAP", price_tiers=[{"min_qty": 1, "unit_price": "0.0900"}]
    )
    make_listing(session, store, sku="MOQ", moq=1000)
    make_listing(session, store, sku="STOCK", stock=10)
    make_listing(session, store, sku="SLOW", lead_time_days=60)
    make_listing(session, store, sku="M10", diameter="M10")
    make_listing(session, store, sku="LONG", length_mm=Decimal("35"))
    make_listing(session, store, sku="OLD", is_current=False)

    spec = SearchSpec(
        diameter="M8",
        length_mm=Decimal("30"),
        ship_to="US-CA",
        need_by=date(2026, 11, 20),
        qty=200,
    )
    page = search(session, spec)

    assert [hit.listing.sku for hit in page.hits] == ["CHEAP", "PRICEY"]
    assert [hit.rank for hit in page.hits] == [1, 2]
    assert page.hits[0].landed == landed_cost(as_priceable(cheap), 200, "US-CA")
    assert page.hits[1].landed.total == landed_cost(as_priceable(pricey), 200, "US-CA").total
    assert "price_floor" not in page.hits[0].listing.model_dump()
    assert {row.sku: row.reason for row in page.excluded} == {
        "MOQ": ExclusionReason.MOQ,
        "STOCK": ExclusionReason.STOCK,
        "SLOW": ExclusionReason.DATE,
    }

    wider = search(session, spec.model_copy(update={"length_tolerance_mm": Decimal("5")}))
    assert "LONG" in [hit.listing.sku for hit in wider.hits]
