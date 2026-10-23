from decimal import Decimal

from sqlmodel import Session

from trajano.models import Agent, Mandate
from trajano.schemas import ApprovalView, MandateDocument


def issue(
    session: Session,
    agent: Agent,
    document: MandateDocument,
    idempotency_key: str,
) -> MandateDocument:
    """Store the mandate and set signature to HMAC-SHA256(server key, mandate_signing_bytes).

    The agent never holds the key, only the signed document. Hex digest, 64 chars.
    """
    raise NotImplementedError("Mateo: mandates.issue")


def verify(
    session: Session,
    agent: Agent,
    document: MandateDocument,
    *,
    unit_price: Decimal,
    qty: int,
    total: Decimal,
) -> Mandate:
    """Check the HMAC, the stored row, revocation, the expiry, the agent, and the limits.

    Limits: unit_price <= max_unit_price, qty <= max_qty, total <= max_total,
    and the category covers the listing. Failure is MANDATE_INVALID.
    """
    raise NotImplementedError("Mateo: mandates.verify")


def revoke(session: Session, agent: Agent, mandate_id: int, idempotency_key: str) -> Mandate:
    raise NotImplementedError("Mateo: mandates.revoke")


def request_approval(
    session: Session,
    agent: Agent,
    offer_id: int,
    idempotency_key: str,
) -> ApprovalView:
    raise NotImplementedError("Mateo: mandates.request_approval")


def get_approval(session: Session, agent: Agent, approval_id: int) -> ApprovalView:
    raise NotImplementedError("Mateo: mandates.get_approval")
