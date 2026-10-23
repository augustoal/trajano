import hmac
from decimal import Decimal
from hashlib import sha256

from sqlmodel import Session, select

from trajano.config import get_settings
from trajano.db import utc_now
from trajano.domain import idempotency
from trajano.domain.errors import DomainError
from trajano.models import Agent, ApprovalRequest, Mandate, Offer, Thread
from trajano.schemas import (
    ApprovalStatus,
    ApprovalView,
    ErrorCode,
    MandateDocument,
    Role,
    mandate_signing_bytes,
)

# The only category in the MVP. A mandate with category None covers every category.
CATEGORY = "fasteners"


def sign(document: MandateDocument) -> str:
    key = get_settings().mandate_signing_key.encode()
    return hmac.new(key, mandate_signing_bytes(document), sha256).hexdigest()


def issue(
    session: Session,
    agent: Agent,
    document: MandateDocument,
    idempotency_key: str,
) -> MandateDocument:
    """Store the mandate and set signature to HMAC-SHA256(server key, mandate_signing_bytes).

    The agent never holds the key, only the signed document. Another agent of
    the same company issues it: an agent cannot set its own limits.
    """
    fingerprint = idempotency.request_fingerprint(
        {"issue": document.model_dump(mode="json", exclude={"id", "signature"})}
    )
    replay = idempotency.begin(session, agent.id or 0, idempotency_key, fingerprint)
    if replay is not None:
        return MandateDocument(**replay)
    if document.company_id != agent.company_id or document.agent_id == agent.id:
        raise DomainError(
            ErrorCode.FORBIDDEN,
            "a mandate is issued by another agent of the same company",
            action="mandate_issue",
            agent_id=agent.id,
            detail={"for_agent_id": document.agent_id},
        )
    holder = session.get(Agent, document.agent_id)
    if holder is None or holder.company_id != agent.company_id:
        raise DomainError(ErrorCode.NOT_FOUND, f"agent {document.agent_id} not found")
    if Role.BUYER.value not in holder.roles:
        raise DomainError(ErrorCode.VALIDATION, "a mandate is for an agent with the buyer role")
    signed = _create(session, document)
    idempotency.complete(session, agent.id or 0, idempotency_key, signed.model_dump(mode="json"))
    return signed


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

    def refuse(reason: str) -> DomainError:
        return DomainError(
            ErrorCode.MANDATE_INVALID,
            f"mandate {reason}",
            action="mandate_verify",
            agent_id=agent.id,
            detail={"mandate_id": document.id, "reason": reason},
        )

    if not hmac.compare_digest(sign(document), document.signature):
        raise refuse("signature does not match")
    row = session.get(Mandate, document.id) if document.id is not None else None
    if row is None:
        raise refuse("is not on file")
    if not hmac.compare_digest(row.signature, document.signature):
        raise refuse("does not match the stored mandate")
    if row.agent_id != agent.id or row.company_id != agent.company_id:
        raise refuse("belongs to another agent")
    if row.revoked_at is not None:
        raise refuse("was revoked")
    if utc_now() >= row.expires_at:
        raise refuse("has expired")
    if row.category not in (None, CATEGORY):
        raise refuse(f"does not cover {CATEGORY}")
    if unit_price > row.max_unit_price:
        raise refuse(f"caps the unit price at {row.max_unit_price}")
    if qty > row.max_qty:
        raise refuse(f"caps the quantity at {row.max_qty}")
    if total > row.max_total:
        raise refuse(f"caps the total at {row.max_total}")
    return row


def revoke(session: Session, agent: Agent, mandate_id: int, idempotency_key: str) -> Mandate:
    """Any agent of the mandate's company can revoke it. Revoking twice is a no-op."""
    fingerprint = idempotency.request_fingerprint({"revoke": mandate_id})
    replay = idempotency.begin(session, agent.id or 0, idempotency_key, fingerprint)
    row = session.get(Mandate, mandate_id)
    if row is None or row.company_id != agent.company_id:
        raise DomainError(ErrorCode.NOT_FOUND, f"mandate {mandate_id} not found")
    if replay is None:
        if row.revoked_at is None:
            row.revoked_at = utc_now()
            session.add(row)
            session.flush()
        idempotency.complete(session, agent.id or 0, idempotency_key, {"mandate_id": mandate_id})
    return row


def get_document(session: Session, agent: Agent, mandate_id: int) -> MandateDocument:
    """The signed document, for the agent it was issued to or its company."""
    row = session.get(Mandate, mandate_id)
    if row is None or row.company_id != agent.company_id:
        raise DomainError(ErrorCode.NOT_FOUND, f"mandate {mandate_id} not found")
    return _document(row)


def request_approval(
    session: Session,
    agent: Agent,
    offer_id: int,
    idempotency_key: str,
) -> ApprovalView:
    """Ask the company to approve one offer. One pending request per offer and agent."""
    fingerprint = idempotency.request_fingerprint({"request_approval": offer_id})
    replay = idempotency.begin(session, agent.id or 0, idempotency_key, fingerprint)
    if replay is not None:
        return ApprovalView(**replay)
    offer = session.get(Offer, offer_id)
    thread = session.get(Thread, offer.thread_id) if offer is not None else None
    if offer is None or thread is None or thread.buyer_agent_id != agent.id:
        raise DomainError(ErrorCode.NOT_FOUND, f"offer {offer_id} not found")
    if utc_now() > offer.expires_at:
        raise DomainError(ErrorCode.STALE_OFFER, "the offer has expired")
    approval = session.exec(
        select(ApprovalRequest).where(
            ApprovalRequest.offer_id == offer_id,
            ApprovalRequest.agent_id == agent.id,
            ApprovalRequest.status == ApprovalStatus.PENDING,
        )
    ).first()
    if approval is None:
        approval = ApprovalRequest(offer_id=offer_id, agent_id=agent.id or 0)
        session.add(approval)
        session.flush()
    view = _approval_view(approval)
    idempotency.complete(session, agent.id or 0, idempotency_key, view.model_dump(mode="json"))
    return view


def get_approval(session: Session, agent: Agent, approval_id: int) -> ApprovalView:
    return _approval_view(_company_approval(session, agent, approval_id))


def decide_approval(
    session: Session,
    agent: Agent,
    approval_id: int,
    *,
    approve: bool,
    idempotency_key: str,
) -> ApprovalView:
    """Approve or reject a pending request. The requester cannot decide its own.

    Approval issues a mandate for exactly that offer: its unit price, quantity,
    and total, expiring with the offer. The requester reads it with get_document.
    """
    fingerprint = idempotency.request_fingerprint(
        {"decide_approval": approval_id, "approve": approve}
    )
    replay = idempotency.begin(session, agent.id or 0, idempotency_key, fingerprint)
    if replay is not None:
        return ApprovalView(**replay)
    approval = _company_approval(session, agent, approval_id)
    if approval.agent_id == agent.id:
        raise DomainError(
            ErrorCode.FORBIDDEN,
            "an agent cannot decide its own approval request",
            action="decide_approval",
            agent_id=agent.id,
            detail={"approval_id": approval_id},
        )
    if approval.status is not ApprovalStatus.PENDING:
        raise DomainError(ErrorCode.INVALID_TRANSITION, f"approval is {approval.status.value}")
    if approve:
        offer = session.get(Offer, approval.offer_id)
        if offer is None:
            raise DomainError(ErrorCode.INTERNAL, "approval is missing its offer", retryable=True)
        if utc_now() > offer.expires_at:
            raise DomainError(ErrorCode.STALE_OFFER, "the offer has expired")
        signed = _create(
            session,
            MandateDocument(
                company_id=agent.company_id,
                agent_id=approval.agent_id,
                category=CATEGORY,
                max_unit_price=Decimal(offer.unit_price),
                max_total=Decimal(offer.total),
                max_qty=offer.qty,
                currency=offer.currency,
                expires_at=offer.expires_at,
            ),
        )
        approval.mandate_id = signed.id
        approval.status = ApprovalStatus.APPROVED
    else:
        approval.status = ApprovalStatus.REJECTED
    session.add(approval)
    session.flush()
    view = _approval_view(approval)
    idempotency.complete(session, agent.id or 0, idempotency_key, view.model_dump(mode="json"))
    return view


def _create(session: Session, document: MandateDocument) -> MandateDocument:
    if document.expires_at <= utc_now():
        raise DomainError(ErrorCode.VALIDATION, "expires_at must be in the future")
    signature = sign(document)
    row = Mandate(
        company_id=document.company_id,
        agent_id=document.agent_id,
        category=document.category,
        max_unit_price=document.max_unit_price,
        max_total=document.max_total,
        max_qty=document.max_qty,
        currency=document.currency,
        expires_at=document.expires_at,
        signature=signature,
    )
    session.add(row)
    session.flush()
    return document.model_copy(update={"id": row.id, "signature": signature})


def _document(row: Mandate) -> MandateDocument:
    return MandateDocument(
        id=row.id,
        company_id=row.company_id,
        agent_id=row.agent_id,
        category=row.category,
        max_unit_price=Decimal(row.max_unit_price),
        max_total=Decimal(row.max_total),
        max_qty=row.max_qty,
        currency=row.currency,
        expires_at=row.expires_at,
        signature=row.signature,
    )


def _company_approval(session: Session, agent: Agent, approval_id: int) -> ApprovalRequest:
    approval = session.get(ApprovalRequest, approval_id)
    requester = session.get(Agent, approval.agent_id) if approval is not None else None
    if approval is None or requester is None or requester.company_id != agent.company_id:
        raise DomainError(ErrorCode.NOT_FOUND, f"approval {approval_id} not found")
    return approval


def _approval_view(approval: ApprovalRequest) -> ApprovalView:
    return ApprovalView(
        id=approval.id or 0,
        offer_id=approval.offer_id,
        agent_id=approval.agent_id,
        status=approval.status,
        mandate_id=approval.mandate_id,
    )
