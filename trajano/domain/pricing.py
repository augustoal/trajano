"""Landed cost: tier, pack rounding, freight by zone, and tax.

Search, offers, the rule seller, and the solver all call landed_cost so they
compute the same number. This module stays free of the database and the clock.

Places are "CC-RR" codes (country, then state or province), for example
"US-IL". Anything else is still priced: an unknown place is never local.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from trajano.domain.errors import DomainError
from trajano.schemas import MONEY, UNIT, Breakdown, CostLine, ErrorCode, PriceableListing

CURRENCY = "USD"


class Zone(StrEnum):
    LOCAL = "local"
    REGIONAL = "regional"
    NATIONAL = "national"
    INTERNATIONAL = "international"


@dataclass(frozen=True)
class _Rate:
    base: Decimal
    per_pack: Decimal
    transit_days: int


_FREIGHT = {
    Zone.LOCAL: _Rate(Decimal("6.00"), Decimal("0.15"), 1),
    Zone.REGIONAL: _Rate(Decimal("9.00"), Decimal("0.25"), 2),
    Zone.NATIONAL: _Rate(Decimal("14.00"), Decimal("0.40"), 4),
    Zone.INTERNATIONAL: _Rate(Decimal("45.00"), Decimal("1.20"), 10),
}

# US Census regions. Two states in the same region ship as REGIONAL.
_US_REGIONS = {
    "northeast": "CT ME MA NH RI VT NJ NY PA",
    "midwest": "IL IN MI OH WI IA KS MN MO NE ND SD",
    "south": "DE DC FL GA MD NC SC VA WV AL KY MS TN AR LA OK TX",
    "west": "AZ CO ID MT NV NM UT WY AK CA HI OR WA",
}
_US_REGION = {state: region for region, states in _US_REGIONS.items() for state in states.split()}

# Synthetic, close to state base rates without local add-ons. Tax applies to
# goods only. Destinations outside the US are exports and carry no tax.
_US_TAX_PERCENT = {
    "AL": "4", "AK": "0", "AZ": "5.6", "AR": "6.5", "CA": "7.25", "CO": "2.9",
    "CT": "6.35", "DE": "0", "DC": "6", "FL": "6", "GA": "4", "HI": "4",
    "ID": "6", "IL": "6.25", "IN": "7", "IA": "6", "KS": "6.5", "KY": "6",
    "LA": "5", "ME": "5.5", "MD": "6", "MA": "6.25", "MI": "6", "MN": "6.875",
    "MS": "7", "MO": "4.225", "MT": "0", "NE": "5.5", "NV": "6.85", "NH": "0",
    "NJ": "6.625", "NM": "4.875", "NY": "4", "NC": "4.75", "ND": "5", "OH": "5.75",
    "OK": "4.5", "OR": "0", "PA": "6", "RI": "7", "SC": "6", "SD": "4.2",
    "TN": "7", "TX": "6.25", "UT": "6.1", "VT": "6", "VA": "5.3", "WA": "6.5",
    "WV": "6", "WI": "5", "WY": "4",
}  # fmt: skip
_US_TAX_DEFAULT = "6"


def _place(code: str) -> tuple[str, str]:
    country, _, region = code.strip().upper().partition("-")
    return country, region


def zone(ships_from: str, ship_to: str) -> Zone:
    origin, destination = _place(ships_from), _place(ship_to)
    if not origin[0] or origin[0] != destination[0]:
        return Zone.INTERNATIONAL
    if origin[1] and origin[1] == destination[1]:
        return Zone.LOCAL
    if origin[0] == "US":
        region = _US_REGION.get(origin[1])
        if region is not None and region == _US_REGION.get(destination[1]):
            return Zone.REGIONAL
    return Zone.NATIONAL


def transit_days(ships_from: str, ship_to: str) -> int:
    """Days in transit after the lead time. Search and the solver use this for need_by."""
    return _FREIGHT[zone(ships_from, ship_to)].transit_days


def tax_percent(ship_to: str) -> Decimal:
    country, region = _place(ship_to)
    if country != "US":
        return Decimal("0")
    return Decimal(_US_TAX_PERCENT.get(region, _US_TAX_DEFAULT))


def tier_price(listing: PriceableListing, qty: int) -> Decimal:
    """The highest tier whose min_qty fits qty. Below every tier, the first tier applies."""
    tiers = sorted(listing.price_tiers, key=lambda tier: tier.min_qty)
    price = tiers[0].unit_price
    for tier in tiers:
        if tier.min_qty <= qty:
            price = tier.unit_price
    return price


def billed_qty(qty: int, pack_size: int) -> int:
    return -(-qty // pack_size) * pack_size


def landed_cost(
    listing: PriceableListing,
    qty: int,
    ship_to: str,
    *,
    unit_price: Decimal | None = None,
) -> Breakdown:
    """Price qty units delivered to ship_to.

    qty rounds up to whole packs and the tier is chosen on the billed quantity.
    unit_price replaces the tier price for a negotiated offer; everything else
    is computed the same way, so an offer can be checked against this function.
    Lines are rounded to cents and the total is the sum of the rounded lines.
    """
    if qty < 1:
        raise DomainError(ErrorCode.VALIDATION, "qty must be at least 1")
    if listing.currency != CURRENCY:
        raise DomainError(ErrorCode.VALIDATION, f"only {CURRENCY} listings can be priced")
    if unit_price is not None and unit_price <= 0:
        raise DomainError(ErrorCode.VALIDATION, "unit_price must be positive")

    qty_billed = billed_qty(qty, listing.pack_size)
    price = _unit(unit_price if unit_price is not None else tier_price(listing, qty_billed))
    packs = qty_billed // listing.pack_size
    shipping_zone = zone(listing.ships_from, ship_to)
    rate = _FREIGHT[shipping_zone]
    percent = tax_percent(ship_to)

    lines = [CostLine(kind="goods", description=f"{qty} units", amount=_money(price * qty))]
    extra = qty_billed - qty
    if extra:
        lines.append(
            CostLine(
                kind="pack_rounding",
                description=f"{extra} units to fill packs of {listing.pack_size}",
                amount=_money(price * extra),
            )
        )
    goods = sum((line.amount for line in lines), Decimal("0"))
    freight = _money(rate.base + rate.per_pack * packs)
    lines.append(
        CostLine(
            kind="freight", description=f"{shipping_zone.value}, {packs} packs", amount=freight
        )
    )
    tax = _money(goods * percent / 100)
    if tax:
        lines.append(CostLine(kind="tax", description=f"sales tax {percent}%", amount=tax))

    return Breakdown(
        listing_id=listing.id,
        listing_version=listing.version,
        qty_requested=qty,
        qty_billed=qty_billed,
        unit_price=price,
        currency=listing.currency,
        lines=lines,
        goods=goods,
        freight=freight,
        tax=tax,
        total=goods + freight + tax,
    )


def _unit(value: Decimal) -> Decimal:
    return value.quantize(UNIT, rounding=ROUND_HALF_UP)


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)
