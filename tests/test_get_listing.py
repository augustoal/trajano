from decimal import Decimal

import pytest

from tests.factories import make_listing, make_seller
from trajano.domain.catalog import get_listing, search
from trajano.domain.errors import DomainError
from trajano.schemas import ErrorCode, SearchSpec


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


def test_search_is_not_implemented_once_the_spec_is_complete(session) -> None:
    from datetime import date

    spec = SearchSpec(length_mm=Decimal("30"), ship_to="US-CA", need_by=date(2026, 12, 1), qty=100)
    with pytest.raises(NotImplementedError, match="catalog.search"):
        search(session, spec)
