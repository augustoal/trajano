from collections import Counter
from decimal import Decimal

from sqlmodel import select

from data.catalog import INJECTIONS, build
from data.seed import load_catalog
from trajano.domain.accounts import authenticate
from trajano.domain.catalog import get_listing
from trajano.models import Listing, Store
from trajano.schemas import ListingWrite, Role


def _rows() -> list[dict]:
    return [row for seller in build() for row in seller.listings]


def test_catalog_is_the_same_on_every_build() -> None:
    assert _rows() == _rows()


def test_catalog_shape() -> None:
    sellers = build()
    rows = _rows()
    assert len(sellers) == 5
    assert len(rows) == 200
    assert [row["listing_id"] for row in rows] == list(range(1, 201))
    assert len({seller.ships_from for seller in sellers}) == 5
    for seller in sellers:
        skus = [row["sku"] for row in seller.listings]
        assert len(skus) == len(set(skus))
    carried_by_all = [
        sku for sku, count in Counter(row["sku"] for row in rows).items() if count == 5
    ]
    assert len(carried_by_all) >= 20
    assert any(row["stock"] == 0 for row in rows)


def test_every_row_is_a_valid_listing_with_a_floor_below_its_tiers() -> None:
    for row in _rows():
        body = ListingWrite(**{key: value for key, value in row.items() if key != "listing_id"})
        lowest = min(tier.unit_price for tier in body.price_tiers)
        assert 0 < body.price_floor < lowest
        prices = [tier.unit_price for tier in body.price_tiers]
        assert prices == sorted(prices, reverse=True)
        assert body.price_tiers[0].min_qty == 1
        assert body.stock % body.pack_size == 0


def test_injections_sit_on_known_listings() -> None:
    notes = {row["listing_id"]: row["seller_notes"] for row in _rows()}
    for listing_id, text in INJECTIONS.items():
        assert notes[listing_id] == text


def test_load_catalog_inserts_sellers_and_listings(session) -> None:
    keys = load_catalog(session)
    session.commit()
    assert len(keys) == 5
    assert len(session.exec(select(Store)).all()) == 5
    listings = session.exec(select(Listing)).all()
    assert len(listings) == 200
    assert all(listing.is_current and listing.version == 1 for listing in listings)

    public = get_listing(session, 93)
    assert public.seller_notes == INJECTIONS[93]
    assert isinstance(public.price_tiers[0].unit_price, Decimal)

    for raw in keys.values():
        agent = authenticate(session, raw)
        assert agent.roles == [Role.SELLER.value]
