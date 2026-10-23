from decimal import Decimal

from sqlmodel import Session

from trajano.domain.errors import DomainError
from trajano.models import Company, Listing, Store
from trajano.schemas import (
    ErrorCode,
    ListingPublic,
    ListingSeller,
    ListingWrite,
    PriceableListing,
    PriceTier,
    SearchPage,
    SearchSpec,
    StoreView,
)

# length, destination, need-by, and quantity. Order is part of the error text.
_REQUIRED_SEARCH = ("length_mm", "ship_to", "need_by", "qty")


def as_priceable(listing: Listing) -> PriceableListing:
    return PriceableListing(
        id=listing.id or 0,
        version=listing.version,
        price_tiers=_tiers(listing.price_tiers),
        pack_size=listing.pack_size,
        ships_from=listing.ships_from,
        currency=listing.currency,
    )


def search(session: Session, spec: SearchSpec) -> SearchPage:
    """Filter current listings and sort by landed cost, then listing id.

    Exclude, with a reason, rows that miss MOQ, stock, or need_by given the
    lead time. Only the current version of each (store, sku) is eligible.
    Calls pricing.landed_cost for every hit so search and offers match.
    """
    missing = [name for name in _REQUIRED_SEARCH if getattr(spec, name) is None]
    if missing:
        raise DomainError(
            ErrorCode.SPEC_INCOMPLETE,
            "missing " + ", ".join(missing),
        )
    raise NotImplementedError("Augusto: catalog.search")


def get_listing(session: Session, listing_id: int) -> ListingPublic:
    listing = session.get(Listing, listing_id)
    if listing is None:
        raise DomainError(ErrorCode.NOT_FOUND, f"listing {listing_id} not found")
    store = session.get(Store, listing.store_id)
    if store is None:
        raise DomainError(ErrorCode.INTERNAL, "listing is missing its store", retryable=True)
    company = session.get(Company, store.company_id)
    if company is None:
        raise DomainError(ErrorCode.INTERNAL, "store is missing its company", retryable=True)
    return ListingPublic(
        id=listing.id or 0,
        store_id=store.id or 0,
        store_name=store.name,
        company_name=company.name,
        sku=listing.sku,
        version=listing.version,
        diameter=listing.diameter,
        length_mm=Decimal(listing.length_mm),
        material=listing.material,
        grade=listing.grade,
        standard=listing.standard,
        price_tiers=_tiers(listing.price_tiers),
        moq=listing.moq,
        pack_size=listing.pack_size,
        stock=listing.stock,
        lead_time_days=listing.lead_time_days,
        ships_from=listing.ships_from,
        seller_notes=listing.seller_notes,
        currency=listing.currency,
    )


def get_listing_for_seller(
    session: Session, agent_company_id: int, listing_id: int
) -> ListingSeller:
    """Same as get_listing plus price_floor, only for the store's company."""
    raise NotImplementedError("Augusto: catalog.get_listing_for_seller")


def create_store(
    session: Session,
    agent_id: int,
    company_id: int,
    name: str,
    idempotency_key: str,
) -> StoreView:
    raise NotImplementedError("Augusto: catalog.create_store")


def create_listing(
    session: Session,
    agent_id: int,
    company_id: int,
    store_id: int,
    body: ListingWrite,
    idempotency_key: str,
) -> ListingSeller:
    """Insert version 1. price_tiers are stored with unit_price as a decimal string."""
    raise NotImplementedError("Augusto: catalog.create_listing")


def update_listing(
    session: Session,
    agent_id: int,
    company_id: int,
    listing_id: int,
    body: ListingWrite,
    idempotency_key: str,
) -> ListingSeller:
    """Insert the next version of the same (store, sku) and mark the previous one not current.

    Offers keep pointing at the version they quoted.
    """
    raise NotImplementedError("Augusto: catalog.update_listing")


def _tiers(raw: list[dict]) -> list[PriceTier]:
    tiers = [
        PriceTier(min_qty=int(item["min_qty"]), unit_price=Decimal(str(item["unit_price"])))
        for item in raw
    ]
    tiers.sort(key=lambda tier: tier.min_qty)
    return tiers
