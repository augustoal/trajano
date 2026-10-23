from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from tests.factories import make_buyer, make_listing, make_offer, make_seller, make_thread
from trajano.app import create_app
from trajano.db import freeze_clock
from trajano.domain.accounts import create_agent, owner_dashboard, set_agent_active
from trajano.domain.errors import DomainError
from trajano.models import Agent, ApprovalRequest, AuditEvent, Mandate
from trajano.schemas import ApprovalStatus, ErrorCode, Role, ThreadStatus

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def _mandate(session, agent: Agent, **overrides: object) -> Mandate:
    data: dict[str, object] = {
        "company_id": agent.company_id,
        "agent_id": agent.id,
        "max_unit_price": Decimal("0.20"),
        "max_total": Decimal("500"),
        "max_qty": 5000,
        "expires_at": NOW + timedelta(days=30),
        "signature": "0" * 64,
    }
    data.update(overrides)
    mandate = Mandate(**data)  # type: ignore[arg-type]
    session.add(mandate)
    session.flush()
    return mandate


def test_pause_and_restore_a_teammate(session) -> None:
    owner, _key = make_buyer(session, "Ada")
    teammate, _mate_key = create_agent(session, owner.company_id, "Bot", [Role.BUYER])

    paused = set_agent_active(session, owner, teammate.id or 0, active=False, idempotency_key="p1")
    assert paused.active is False
    again = set_agent_active(session, owner, teammate.id or 0, active=False, idempotency_key="p1")
    assert again.id == teammate.id

    restored = set_agent_active(session, owner, teammate.id or 0, active=True, idempotency_key="p2")
    assert restored.active is True

    audits = session.exec(select(AuditEvent).where(AuditEvent.action == "set_agent_active")).all()
    assert [event.detail["active"] for event in audits] == [False, True]


def test_pause_refusals(session) -> None:
    owner, _key = make_buyer(session, "Ada")
    stranger, _other_key = make_buyer(session, "Bea")

    with pytest.raises(DomainError) as exc:
        set_agent_active(session, owner, stranger.id or 0, active=False, idempotency_key="x")
    assert exc.value.code is ErrorCode.FORBIDDEN
    assert exc.value.action == "set_agent_active"

    with pytest.raises(DomainError) as exc:
        set_agent_active(session, owner, owner.id or 0, active=False, idempotency_key="self")
    assert exc.value.code is ErrorCode.VALIDATION

    with pytest.raises(DomainError) as exc:
        set_agent_active(session, owner, 999, active=False, idempotency_key="none")
    assert exc.value.code is ErrorCode.NOT_FOUND


def test_dashboard_shows_only_live_company_state(session) -> None:
    freeze_clock(NOW)
    seller, _seller_key, store = make_seller(session)
    buyer, _key = make_buyer(session, "Ada")
    rival, _rival_key = make_buyer(session, "Bea")
    listing = make_listing(session, store)
    thread = make_thread(session, buyer, listing, status=ThreadStatus.OFFERED)
    offer = make_offer(session, thread, seller, expires_at=NOW + timedelta(hours=1))

    live = _mandate(session, buyer)
    _mandate(session, buyer, revoked_at=NOW - timedelta(days=1))
    _mandate(session, buyer, expires_at=NOW - timedelta(days=1))
    _mandate(session, rival)
    pending = ApprovalRequest(offer_id=offer.id or 0, agent_id=buyer.id or 0)
    done = ApprovalRequest(
        offer_id=offer.id or 0, agent_id=buyer.id or 0, status=ApprovalStatus.APPROVED
    )
    session.add_all([pending, done])
    session.flush()

    view = owner_dashboard(session, buyer)
    assert [row.id for row in view.mandates] == [live.id]
    assert [row.id for row in view.approvals] == [pending.id]
    assert [row.id for row in view.agents] == [buyer.id]


def test_owner_page_pauses_an_agent(session) -> None:
    owner, key = make_buyer(session, "Ada")
    teammate, _mate_key = create_agent(session, owner.company_id, "Bot", [Role.BUYER])
    session.commit()

    with TestClient(create_app()) as client:
        client.post("/login", data={"api_key": key, "next": "/owner"})
        page = client.post(
            "/owner/agent",
            data={"agent_id": str(teammate.id), "active": "false", "idempotency_key": "web-p"},
        )
        assert page.status_code == 200
        assert "Bot paused" in page.text

    session.refresh(teammate)
    assert teammate.active is False
