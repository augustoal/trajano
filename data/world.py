"""Server-side setup and ground truth for one eval run.

eval.run is the experimenter, not a participant: it provisions the buyer
here and, after the run, reads what was really ordered from the database
instead of trusting the agent's own account.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlmodel import Session, select

from trajano.domain import mandates
from trajano.domain.accounts import create_agent, create_company
from trajano.models import Listing, Offer, Order
from trajano.schemas import MandateDocument, Role


def provision_buyer(
    session: Session, limits: dict[str, Any], expires_at: datetime
) -> dict[str, Any]:
    """A buyer company with one buyer agent and the signed mandate its owner issued."""
    company = create_company(session, "Eval Buyer Co", domain="eval-buyer.example")
    buyer, api_key = create_agent(session, company.id or 0, "Eval buyer", [Role.BUYER])
    owner, _owner_key = create_agent(session, company.id or 0, "Eval owner", [Role.BUYER])
    document = mandates.issue(
        session,
        owner,
        MandateDocument(
            company_id=company.id or 0,
            agent_id=buyer.id or 0,
            max_unit_price=Decimal(str(limits["max_unit_price"])),
            max_total=Decimal(str(limits["max_total"])),
            max_qty=int(limits["max_qty"]),
            expires_at=expires_at,
        ),
        "eval-mandate",
    )
    return {
        "agent_id": buyer.id,
        "api_key": api_key,
        "mandate": document.model_dump(mode="json"),
    }


def buyer_orders(session: Session, agent_id: int) -> list[dict[str, Any]]:
    """Every order the agent placed, with what it bought, in order of creation."""
    rows = session.exec(
        select(Order, Offer, Listing)
        .join(Offer, Order.offer_id == Offer.id)
        .join(Listing, Offer.listing_id == Listing.id)
        .where(Order.buyer_agent_id == agent_id)
        .order_by(Order.id)
    ).all()
    return [
        {
            "order_id": order.id,
            "status": order.status.value,
            "listing_id": listing.id,
            "diameter": listing.diameter,
            "length_mm": format(Decimal(listing.length_mm).normalize(), "f"),
            "material": listing.material,
            "grade": listing.grade,
            "standard": listing.standard,
            "pack_size": listing.pack_size,
            "qty": offer.qty,
            "unit_price": format(Decimal(offer.unit_price), "f"),
            "total": format(Decimal(offer.total).quantize(Decimal("0.01")), "f"),
            "delivery_date": offer.delivery_date.isoformat(),
        }
        for order, offer, listing in rows
    ]
