import hmac
import secrets
from datetime import timedelta
from hashlib import sha256

from sqlalchemy import func
from sqlmodel import Session, col, select

from trajano.config import get_settings
from trajano.db import utc_now
from trajano.domain import idempotency
from trajano.domain.errors import DomainError
from trajano.models import Agent, ApprovalRequest, AuditEvent, Company, Mandate, Message
from trajano.schemas import (
    AgentView,
    ApprovalStatus,
    ApprovalView,
    ErrorCode,
    MandateView,
    MessageType,
    OwnerView,
    Role,
)

_KEY_PREFIX = "tj_"


def generate_api_key() -> str:
    return _KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(raw: str) -> str:
    # Keys are random, so a fast HMAC is right. This is not a password store.
    pepper = get_settings().api_key_pepper.encode()
    return hmac.new(pepper, raw.encode(), sha256).hexdigest()


def create_company(
    session: Session,
    name: str,
    *,
    domain: str | None = None,
    verified: bool = False,
) -> Company:
    cleaned = name.strip()
    if not cleaned:
        raise DomainError(ErrorCode.VALIDATION, "company name is required")
    normalized = domain.strip().lower() if domain else None
    if normalized == "":
        normalized = None
    if normalized is not None:
        taken = session.exec(select(Company).where(Company.domain == normalized)).first()
        if taken is not None:
            raise DomainError(ErrorCode.CONFLICT, "company domain already registered")
    company = Company(name=cleaned, domain=normalized, verified=verified)
    session.add(company)
    session.flush()
    return company


def create_agent(
    session: Session,
    company_id: int,
    name: str,
    roles: list[Role],
) -> tuple[Agent, str]:
    if not roles:
        raise DomainError(ErrorCode.VALIDATION, "agent needs a role")
    if any(role is Role.SYSTEM for role in roles):
        raise DomainError(ErrorCode.VALIDATION, "system is not an assignable role")
    if session.get(Company, company_id) is None:
        raise DomainError(ErrorCode.NOT_FOUND, "company not found")
    cleaned = name.strip()
    if not cleaned:
        raise DomainError(ErrorCode.VALIDATION, "agent name is required")
    raw = generate_api_key()
    agent = Agent(
        company_id=company_id,
        name=cleaned,
        api_key_hash=hash_api_key(raw),
        roles=[role.value for role in roles],
        active=True,
    )
    session.add(agent)
    session.flush()
    return agent, raw


def authenticate(session: Session, api_key: str | None) -> Agent:
    if not api_key:
        raise DomainError(ErrorCode.UNAUTHORIZED, "missing API key")
    agent = session.exec(select(Agent).where(Agent.api_key_hash == hash_api_key(api_key))).first()
    if agent is None or not agent.active:
        raise DomainError(
            ErrorCode.UNAUTHORIZED,
            "invalid API key",
            action="authenticate",
            detail={"reason": "unknown" if agent is None else "inactive"},
        )
    return agent


def require_role(agent: Agent, role: Role) -> None:
    if role.value not in agent.roles:
        raise DomainError(
            ErrorCode.FORBIDDEN,
            "agent does not have this role",
            action="require_role",
            agent_id=agent.id,
            detail={"role": role.value},
        )


def enforce_rfq_rate_limit(session: Session, agent: Agent) -> None:
    """Per-company cap on rfq sends. Settings.rfq_rate_limit_per_hour is the cap.

    A refusal is RATE_LIMITED and retryable.
    """
    cap = get_settings().rfq_rate_limit_per_hour
    sent = session.exec(
        select(func.count())
        .select_from(Message)
        .join(Agent, col(Message.sender_agent_id) == Agent.id)
        .where(
            Agent.company_id == agent.company_id,
            Message.type == MessageType.RFQ,
            Message.created_at > utc_now() - timedelta(hours=1),
        )
    ).one()
    if sent >= cap:
        raise DomainError(
            ErrorCode.RATE_LIMITED,
            f"your company has sent {cap} rfqs in the last hour; retry later",
            retryable=True,
            action="rfq_rate_limit",
            agent_id=agent.id,
            detail={"cap": cap},
        )


def set_agent_active(
    session: Session,
    actor: Agent,
    agent_id: int,
    *,
    active: bool,
    idempotency_key: str,
) -> Agent:
    """Pause or restore an agent in the actor's company."""
    fingerprint = idempotency.request_fingerprint(
        {"op": "set_agent_active", "agent_id": agent_id, "active": active}
    )
    stored = idempotency.begin(session, actor.id or 0, idempotency_key, fingerprint)
    target = session.get(Agent, agent_id)
    if target is None:
        raise DomainError(ErrorCode.NOT_FOUND, f"agent {agent_id} not found")
    if stored is not None:
        return target
    if target.company_id != actor.company_id:
        raise DomainError(
            ErrorCode.FORBIDDEN,
            "this agent belongs to another company",
            action="set_agent_active",
            agent_id=actor.id,
            detail={"target_agent_id": agent_id},
        )
    # A paused agent cannot sign in to restore itself.
    if target.id == actor.id and not active:
        raise DomainError(ErrorCode.VALIDATION, "you cannot pause yourself")
    target.active = active
    session.add(target)
    session.add(
        AuditEvent(
            agent_id=actor.id,
            action="set_agent_active",
            result="ok",
            detail={"target_agent_id": agent_id, "active": active},
            created_at=utc_now(),
        )
    )
    session.flush()
    idempotency.complete(
        session, actor.id or 0, idempotency_key, {"agent_id": agent_id, "active": active}
    )
    return target


def owner_dashboard(session: Session, agent: Agent) -> OwnerView:
    """Pending approvals, live mandates, and agents for the caller's company."""
    company_agents = session.exec(
        select(Agent).where(Agent.company_id == agent.company_id).order_by(col(Agent.id))
    ).all()
    approvals = session.exec(
        select(ApprovalRequest)
        .join(Agent, col(ApprovalRequest.agent_id) == Agent.id)
        .where(
            Agent.company_id == agent.company_id,
            ApprovalRequest.status == ApprovalStatus.PENDING,
        )
        .order_by(col(ApprovalRequest.id))
    ).all()
    live = session.exec(
        select(Mandate)
        .where(
            Mandate.company_id == agent.company_id,
            col(Mandate.revoked_at).is_(None),
            Mandate.expires_at > utc_now(),
        )
        .order_by(col(Mandate.id))
    ).all()
    return OwnerView(
        approvals=[
            ApprovalView(
                id=row.id or 0,
                offer_id=row.offer_id,
                agent_id=row.agent_id,
                status=row.status,
                mandate_id=row.mandate_id,
            )
            for row in approvals
        ],
        mandates=[
            MandateView(
                id=row.id or 0,
                agent_id=row.agent_id,
                category=row.category,
                max_unit_price=row.max_unit_price,
                max_total=row.max_total,
                max_qty=row.max_qty,
                currency=row.currency,
                expires_at=row.expires_at,
                revoked_at=row.revoked_at,
            )
            for row in live
        ],
        agents=[
            AgentView(id=row.id or 0, name=row.name, roles=row.roles, active=row.active)
            for row in company_agents
        ],
    )


def record_audit(
    agent_id: int | None,
    action: str,
    result: str,
    detail: dict | None = None,
) -> None:
    """Write an audit row in its own transaction so a rolled-back refusal still lands."""
    from trajano.db import get_engine

    with Session(get_engine(), expire_on_commit=False) as session:
        session.add(
            AuditEvent(
                agent_id=agent_id,
                action=action,
                result=result,
                detail=detail or {},
                created_at=utc_now(),
            )
        )
        session.commit()
