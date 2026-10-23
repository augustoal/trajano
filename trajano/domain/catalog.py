from decimal import Decimal

from sqlmodel import Session, select

from trajano.domain import idempotency
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
    quantize_unit,
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
    listing = session.get(Listing, listing_id)
    if listing is None:
        raise DomainError(ErrorCode.NOT_FOUND, f"listing {listing_id} not found")
    _owned_store(session, agent_company_id, listing.store_id, action="get_listing_for_seller")
    public = get_listing(session, listing_id)
    return ListingSeller(**public.model_dump(), price_floor=listing.price_floor)


def create_store(
    session: Session,
    agent_id: int,
    company_id: int,
    name: str,
    idempotency_key: str,
) -> StoreView:
    cleaned = name.strip()
    fingerprint = idempotency.request_fingerprint({"op": "create_store", "name": cleaned})
    stored = idempotency.begin(session, agent_id, idempotency_key, fingerprint)
    if stored is not None:
        return StoreView.model_validate(stored)
    if not cleaned:
        raise DomainError(ErrorCode.VALIDATION, "store name is required")
    if session.get(Company, company_id) is None:
        raise DomainError(ErrorCode.NOT_FOUND, "company not found")
    store = Store(company_id=company_id, name=cleaned)
    session.add(store)
    session.flush()
    view = StoreView(id=store.id or 0, company_id=company_id, name=store.name)
    idempotency.complete(session, agent_id, idempotency_key, view.model_dump(mode="json"))
    return view


def create_listing(
    session: Session,
    agent_id: int,
    company_id: int,
    store_id: int,
    body: ListingWrite,
    idempotency_key: str,
) -> ListingSeller:
    """Insert version 1. price_tiers are stored with unit_price as a decimal string."""
    fingerprint = idempotency.request_fingerprint(
        {"op": "create_listing", "store_id": store_id, "listing": body.model_dump(mode="json")}
    )
    stored = idempotency.begin(session, agent_id, idempotency_key, fingerprint)
    if stored is not None:
        return ListingSeller.model_validate(stored)
    _owned_store(session, company_id, store_id, action="create_listing", agent_id=agent_id)
    if _current(session, store_id, body.sku) is not None:
        raise DomainError(
            ErrorCode.CONFLICT,
            f"sku {body.sku} already has a listing in this store; use update_listing",
        )
    listing = _insert_version(session, store_id, body, version=1)
    return _finish_write(session, agent_id, company_id, listing, idempotency_key)


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
    fingerprint = idempotency.request_fingerprint(
        {"op": "update_listing", "listing_id": listing_id, "listing": body.model_dump(mode="json")}
    )
    stored = idempotency.begin(session, agent_id, idempotency_key, fingerprint)
    if stored is not None:
        return ListingSeller.model_validate(stored)
    previous = session.get(Listing, listing_id)
    if previous is None:
        raise DomainError(ErrorCode.NOT_FOUND, f"listing {listing_id} not found")
    _owned_store(session, company_id, previous.store_id, action="update_listing", agent_id=agent_id)
    if not previous.is_current:
        current = _current(session, previous.store_id, previous.sku)
        newer = f"; the current one is {current.id}" if current is not None else ""
        raise DomainError(
            ErrorCode.CONFLICT, f"listing {listing_id} is not the current version{newer}"
        )
    if body.sku != previous.sku:
        raise DomainError(ErrorCode.VALIDATION, "sku cannot change; create a new listing")
    previous.is_current = False
    session.add(previous)
    # The one-current index would reject the new row if this were not flushed first.
    session.flush()
    listing = _insert_version(session, previous.store_id, body, version=previous.version + 1)
    return _finish_write(session, agent_id, company_id, listing, idempotency_key)


def _owned_store(
    session: Session,
    company_id: int,
    store_id: int,
    *,
    action: str,
    agent_id: int | None = None,
) -> Store:
    store = session.get(Store, store_id)
    if store is None:
        raise DomainError(ErrorCode.NOT_FOUND, f"store {store_id} not found")
    if store.company_id != company_id:
        raise DomainError(
            ErrorCode.FORBIDDEN,
            "this store belongs to another company",
            action=action,
            agent_id=agent_id,
            detail={"store_id": store_id},
        )
    return store


def _current(session: Session, store_id: int, sku: str) -> Listing | None:
    return session.exec(
        select(Listing).where(
            Listing.store_id == store_id,
            Listing.sku == sku,
            Listing.is_current == True,  # noqa: E712
        )
    ).first()


def _insert_version(
    session: Session, store_id: int, body: ListingWrite, *, version: int
) -> Listing:
    listing = Listing(
        store_id=store_id,
        sku=body.sku,
        version=version,
        is_current=True,
        diameter=body.diameter,
        length_mm=body.length_mm,
        material=body.material,
        grade=body.grade,
        standard=body.standard,
        price_tiers=[
            {"min_qty": tier.min_qty, "unit_price": quantize_unit(tier.unit_price)}
            for tier in body.price_tiers
        ],
        price_floor=body.price_floor,
        moq=body.moq,
        pack_size=body.pack_size,
        stock=body.stock,
        lead_time_days=body.lead_time_days,
        ships_from=body.ships_from,
        seller_notes=body.seller_notes,
        currency=body.currency,
    )
    session.add(listing)
    session.flush()
    return listing


def _finish_write(
    session: Session,
    agent_id: int,
    company_id: int,
    listing: Listing,
    idempotency_key: str,
) -> ListingSeller:
    view = get_listing_for_seller(session, company_id, listing.id or 0)
    idempotency.complete(session, agent_id, idempotency_key, view.model_dump(mode="json"))
    return view


def _tiers(raw: list[dict]) -> list[PriceTier]:
    tiers = [
        PriceTier(min_qty=int(item["min_qty"]), unit_price=Decimal(str(item["unit_price"])))
        for item in raw
    ]
    tiers.sort(key=lambda tier: tier.min_qty)
    return tiers
