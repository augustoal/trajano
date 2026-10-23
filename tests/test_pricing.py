from decimal import Decimal

import pytest

from trajano.domain.errors import DomainError
from trajano.domain.pricing import Zone, landed_cost, tier_price, transit_days, zone
from trajano.schemas import ErrorCode, PriceableListing, PriceTier


def _listing(**overrides: object) -> PriceableListing:
    data: dict[str, object] = {
        "id": 7,
        "version": 2,
        "price_tiers": [
            PriceTier(min_qty=100, unit_price=Decimal("0.12")),
            PriceTier(min_qty=1, unit_price=Decimal("0.18")),
        ],
        "pack_size": 50,
        "ships_from": "US-IL",
    }
    data.update(overrides)
    return PriceableListing(**data)  # type: ignore[arg-type]


def _amounts(breakdown) -> list[tuple[str, str]]:
    return [(line.kind, format(line.amount, "f")) for line in breakdown.lines]


def test_pack_rounding_picks_the_tier_on_the_billed_quantity() -> None:
    priced = landed_cost(_listing(), 120, "US-CA")
    assert priced.qty_requested == 120
    assert priced.qty_billed == 150
    assert priced.unit_price == Decimal("0.1200")
    assert _amounts(priced) == [
        ("goods", "14.40"),
        ("pack_rounding", "3.60"),
        ("freight", "15.20"),
        ("tax", "1.31"),
    ]
    assert (priced.goods, priced.freight, priced.tax) == (
        Decimal("18.00"),
        Decimal("15.20"),
        Decimal("1.31"),
    )
    assert priced.total == Decimal("34.51")
    assert (priced.listing_id, priced.listing_version, priced.currency) == (7, 2, "USD")


def test_rounding_up_into_a_pack_can_stay_on_the_small_tier() -> None:
    priced = landed_cost(_listing(), 40, "US-WI")
    assert priced.qty_billed == 50
    assert priced.unit_price == Decimal("0.1800")
    assert _amounts(priced) == [
        ("goods", "7.20"),
        ("pack_rounding", "1.80"),
        ("freight", "9.25"),
        ("tax", "0.45"),
    ]
    assert priced.total == Decimal("18.70")


def test_exact_packs_have_no_rounding_line() -> None:
    priced = landed_cost(_listing(), 100, "us-il ")
    assert _amounts(priced) == [("goods", "12.00"), ("freight", "6.30"), ("tax", "0.75")]
    assert priced.total == Decimal("19.05")


def test_exports_carry_no_tax_line() -> None:
    priced = landed_cost(_listing(), 100, "MX-NL")
    assert _amounts(priced) == [("goods", "12.00"), ("freight", "47.40")]
    assert priced.tax == Decimal("0")
    assert priced.total == Decimal("59.40")


def test_a_negotiated_unit_price_replaces_the_tier() -> None:
    priced = landed_cost(_listing(), 100, "US-IL", unit_price=Decimal("0.1"))
    assert priced.unit_price == Decimal("0.1000")
    assert _amounts(priced) == [("goods", "10.00"), ("freight", "6.30"), ("tax", "0.63")]
    assert priced.total == Decimal("16.93")


def test_total_is_the_sum_of_the_rounded_lines() -> None:
    listing = _listing(
        price_tiers=[PriceTier(min_qty=1, unit_price=Decimal("0.03335"))], pack_size=7
    )
    for qty in range(1, 300, 13):
        for place in ("US-IL", "US-NY", "US-TX", "CA-ON", "nowhere"):
            priced = landed_cost(listing, qty, place)
            assert priced.total == sum(line.amount for line in priced.lines)
            assert all(
                line.amount == line.amount.quantize(Decimal("0.01")) for line in priced.lines
            )
            assert priced.qty_billed % 7 == 0
            assert 0 <= priced.qty_billed - qty < 7


def test_below_every_tier_the_first_tier_applies() -> None:
    listing = _listing(
        price_tiers=[
            PriceTier(min_qty=500, unit_price=Decimal("0.10")),
            PriceTier(min_qty=100, unit_price=Decimal("0.12")),
        ]
    )
    assert tier_price(listing, 50) == Decimal("0.12")
    assert tier_price(listing, 499) == Decimal("0.12")
    assert tier_price(listing, 500) == Decimal("0.10")


@pytest.mark.parametrize(
    ("ships_from", "ship_to", "expected", "days"),
    [
        ("US-IL", "US-IL", Zone.LOCAL, 1),
        ("US-IL", "US-OH", Zone.REGIONAL, 2),
        ("US-NJ", "US-NY", Zone.REGIONAL, 2),
        ("US-IL", "US-CA", Zone.NATIONAL, 4),
        ("US-IL", "US", Zone.NATIONAL, 4),
        ("US-IL", "MX-NL", Zone.INTERNATIONAL, 10),
        ("CA-ON", "CA-ON", Zone.LOCAL, 1),
        ("CA-ON", "CA-QC", Zone.NATIONAL, 4),
        ("", "", Zone.INTERNATIONAL, 10),
    ],
)
def test_zones(ships_from: str, ship_to: str, expected: Zone, days: int) -> None:
    assert zone(ships_from, ship_to) is expected
    assert transit_days(ships_from, ship_to) == days


def test_bad_inputs_are_validation_errors() -> None:
    for call in (
        lambda: landed_cost(_listing(), 0, "US-IL"),
        lambda: landed_cost(_listing(currency="EUR"), 10, "US-IL"),
        lambda: landed_cost(_listing(), 10, "US-IL", unit_price=Decimal("0")),
    ):
        with pytest.raises(DomainError) as exc:
            call()
        assert exc.value.code is ErrorCode.VALIDATION
