from sqlmodel import Session

from trajano.models import Agent, Offer, Order
from trajano.schemas import DeclineReason, MandateDocument, OrderView


def accept(
    session: Session,
    agent: Agent,
    offer: Offer,
    mandate: MandateDocument,
    idempotency_key: str,
) -> Order:
    """Create a SUBMITTED order in the caller's transaction. Mateo implements this.

    1. If an order with (buyer_agent_id, idempotency_key) exists, return it.
    2. offer.hash matches, expires_at is still in the future (db.utc_now), and
       mandates.verify accepts the document against unit_price, qty, and total.
    3. Decrement stock with a conditional update (stock >= qty). Failure is
       STALE_OFFER and does not insert an order.
    4. Insert the order. Reject restores the stock.
    """
    raise NotImplementedError("Mateo: orders.accept")


def confirm(session: Session, agent: Agent, order_id: int, idempotency_key: str) -> Order:
    raise NotImplementedError("Mateo: orders.confirm")


def reject(
    session: Session,
    agent: Agent,
    order_id: int,
    reason_code: DeclineReason,
    idempotency_key: str,
) -> Order:
    raise NotImplementedError("Mateo: orders.reject")


def ship(
    session: Session, agent: Agent, order_id: int, tracking: str, idempotency_key: str
) -> Order:
    raise NotImplementedError("Mateo: orders.ship")


def deliver(session: Session, agent: Agent, order_id: int, idempotency_key: str) -> Order:
    raise NotImplementedError("Mateo: orders.deliver")


def get_order(session: Session, agent: Agent, order_id: int) -> OrderView:
    raise NotImplementedError("Mateo: orders.get_order")


def export_order_csv(session: Session, agent: Agent, order_id: int) -> str:
    """CSV for one order the caller's company is allowed to see."""
    raise NotImplementedError("Mateo: orders.export_order_csv")
