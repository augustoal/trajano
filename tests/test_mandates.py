from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.factories import make_buyer, make_listing, make_priced_offer, make_seller
from trajano.config import reset_settings
from trajano.db import freeze_clock, utc_now
from trajano.domain import mandates
from trajano.domain.accounts import create_agent
from trajano.domain.errors import DomainError
from trajano.models import Agent
from trajano.schemas import ApprovalStatus, ErrorCode, MandateDocument, Role

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def frozen() -> None:
    freeze_clock(NOW)


def _owner(session, buyer: Agent) -> Agent:
    owner, _key = create_agent(session, buyer.company_id, "Owner", [Role.BUYER])
    return owner


def _document(buyer: Agent, **overrides: object) -> MandateDocument:
    data: dict[str, object] = {
        "company_id": buyer.company_id,
        "agent_id": buyer.id,
        "max_unit_price": Decimal("0.20"),
        "max_total": Decimal("50.00"),
        "max_qty": 500,
        "expires_at": NOW + timedelta(days=30),
    }
    data.update(overrides)
    return MandateDocument(**data)  # type: ignore[arg-type]


def _issued(session, buyer: Agent, **overrides: object) -> MandateDocument:
    return mandates.issue(session, _owner(session, buyer), _document(buyer, **overrides), "m-1")


def _verify(session, buyer: Agent, document: MandateDocument, **terms: object):
    limits: dict[str, object] = {"unit_price": Decimal("0.12"), "qty": 100, "total": Decimal("20")}
    limits.update(terms)
    return mandates.verify(session, buyer, document, **limits)  # type: ignore[arg-type]


def _refusal(call) -> DomainError:
    with pytest.raises(DomainError) as exc:
        call()
    assert exc.value.code is ErrorCode.MANDATE_INVALID
    assert exc.value.action == "mandate_verify"
    return exc.value


def test_issue_signs_and_verify_accepts(session) -> None:
    buyer, _key = make_buyer(session)
    signed = _issued(session, buyer)
    assert signed.id is not None
    assert len(signed.signature) == 64
    assert int(signed.signature, 16) >= 0
    row = _verify(session, buyer, signed)
    assert row.id == signed.id
    # The limits are inclusive.
    _verify(session, buyer, signed, unit_price=Decimal("0.20"), qty=500, total=Decimal("50"))


def test_issue_replays_and_rejects_a_reused_key(session) -> None:
    buyer, _key = make_buyer(session)
    owner = _owner(session, buyer)
    first = mandates.issue(session, owner, _document(buyer), "same")
    assert mandates.issue(session, owner, _document(buyer), "same") == first
    with pytest.raises(DomainError) as exc:
        mandates.issue(session, owner, _document(buyer, max_qty=9), "same")
    assert exc.value.code is ErrorCode.CONFLICT


def test_an_agent_cannot_issue_its_own_or_another_companys_mandate(session) -> None:
    buyer, _key = make_buyer(session)
    stranger, _key = make_buyer(session, "Eve")
    for issuer in (buyer, stranger):
        with pytest.raises(DomainError) as exc:
            mandates.issue(session, issuer, _document(buyer), "x")
        assert exc.value.code is ErrorCode.FORBIDDEN


def test_issue_needs_a_future_expiry(session) -> None:
    buyer, _key = make_buyer(session)
    with pytest.raises(DomainError) as exc:
        _issued(session, buyer, expires_at=NOW)
    assert exc.value.code is ErrorCode.VALIDATION


def test_a_tampered_or_foreign_signature_is_refused(session, monkeypatch) -> None:
    buyer, _key = make_buyer(session)
    signed = _issued(session, buyer)
    raised = signed.model_copy(update={"max_total": Decimal("5000")})
    assert _refusal(lambda: _verify(session, buyer, raised)).detail["reason"] == (
        "signature does not match"
    )
    monkeypatch.setenv("TRAJANO_MANDATE_SIGNING_KEY", "someone-else")
    reset_settings()
    forged = signed.model_copy(update={"signature": mandates.sign(signed)})
    reset_settings()
    monkeypatch.setenv("TRAJANO_MANDATE_SIGNING_KEY", "test-mandate-key")
    reset_settings()
    _refusal(lambda: _verify(session, buyer, forged))


def test_a_signed_document_that_is_not_on_file_is_refused(session) -> None:
    buyer, _key = make_buyer(session)
    unsaved = _document(buyer, id=999)
    unsaved = unsaved.model_copy(update={"signature": mandates.sign(unsaved)})
    assert "not on file" in _refusal(lambda: _verify(session, buyer, unsaved)).message


def test_another_agent_cannot_use_the_mandate(session) -> None:
    buyer, _key = make_buyer(session)
    signed = _issued(session, buyer)
    colleague, _key = create_agent(session, buyer.company_id, "Bob", [Role.BUYER])
    assert "another agent" in _refusal(lambda: _verify(session, colleague, signed)).message


def test_revoked_and_expired_mandates_are_refused(session) -> None:
    buyer, _key = make_buyer(session)
    signed = _issued(session, buyer)
    freeze_clock(NOW + timedelta(days=30))
    assert "expired" in _refusal(lambda: _verify(session, buyer, signed)).message
    freeze_clock(NOW)
    owner = _owner(session, buyer)
    revoked = mandates.revoke(session, owner, signed.id or 0, "r-1")
    assert revoked.revoked_at == utc_now()
    assert mandates.revoke(session, owner, signed.id or 0, "r-1").revoked_at == utc_now()
    assert "revoked" in _refusal(lambda: _verify(session, buyer, signed)).message


def test_revoke_is_scoped_to_the_company(session) -> None:
    buyer, _key = make_buyer(session)
    signed = _issued(session, buyer)
    stranger, _key = make_buyer(session, "Eve")
    with pytest.raises(DomainError) as exc:
        mandates.revoke(session, stranger, signed.id or 0, "r")
    assert exc.value.code is ErrorCode.NOT_FOUND


@pytest.mark.parametrize(
    ("terms", "words"),
    [
        ({"unit_price": Decimal("0.2001")}, "unit price"),
        ({"qty": 501}, "quantity"),
        ({"total": Decimal("50.01")}, "total"),
    ],
)
def test_limits(session, terms: dict, words: str) -> None:
    buyer, _key = make_buyer(session)
    signed = _issued(session, buyer)
    assert words in _refusal(lambda: _verify(session, buyer, signed, **terms)).message


def test_category_must_cover_fasteners(session) -> None:
    buyer, _key = make_buyer(session)
    _refusal(lambda: _verify(session, buyer, _issued(session, buyer, category="valves")))
    _verify(
        session,
        buyer,
        mandates.issue(session, _owner(session, buyer), _document(buyer, category=None), "m-2"),
    )


def test_approval_flow_issues_a_mandate_for_exactly_the_offer(session) -> None:
    buyer, _key = make_buyer(session)
    _seller, _key, store = make_seller(session)
    offer = make_priced_offer(session, buyer, make_listing(session, store))
    asked = mandates.request_approval(session, buyer, offer.id or 0, "a-1")
    assert asked.status is ApprovalStatus.PENDING
    assert mandates.request_approval(session, buyer, offer.id or 0, "a-2").id == asked.id

    with pytest.raises(DomainError) as exc:
        mandates.decide_approval(session, buyer, asked.id, approve=True, idempotency_key="d")
    assert exc.value.code is ErrorCode.FORBIDDEN

    owner = _owner(session, buyer)
    decided = mandates.decide_approval(session, owner, asked.id, approve=True, idempotency_key="d")
    assert decided.status is ApprovalStatus.APPROVED
    assert mandates.get_approval(session, buyer, asked.id) == decided
    document = mandates.get_document(session, buyer, decided.mandate_id or 0)
    terms = {"unit_price": offer.unit_price, "qty": offer.qty, "total": offer.total}
    _verify(session, buyer, document, **terms)
    _refusal(lambda: _verify(session, buyer, document, total=offer.total + Decimal("0.01")))

    with pytest.raises(DomainError) as exc:
        mandates.decide_approval(session, owner, asked.id, approve=False, idempotency_key="d2")
    assert exc.value.code is ErrorCode.INVALID_TRANSITION


def test_approval_rejection_and_scope(session) -> None:
    buyer, _key = make_buyer(session)
    _seller, _key, store = make_seller(session)
    offer = make_priced_offer(session, buyer, make_listing(session, store))
    stranger, _key = make_buyer(session, "Eve")
    with pytest.raises(DomainError) as exc:
        mandates.request_approval(session, stranger, offer.id or 0, "a")
    assert exc.value.code is ErrorCode.NOT_FOUND

    asked = mandates.request_approval(session, buyer, offer.id or 0, "a")
    with pytest.raises(DomainError) as exc:
        mandates.get_approval(session, stranger, asked.id)
    assert exc.value.code is ErrorCode.NOT_FOUND
    owner = _owner(session, buyer)
    decided = mandates.decide_approval(session, owner, asked.id, approve=False, idempotency_key="d")
    assert decided.status is ApprovalStatus.REJECTED
    assert decided.mandate_id is None


def test_an_expired_offer_cannot_be_sent_for_approval(session) -> None:
    buyer, _key = make_buyer(session)
    _seller, _key, store = make_seller(session)
    offer = make_priced_offer(session, buyer, make_listing(session, store))
    freeze_clock(NOW + timedelta(hours=49))
    with pytest.raises(DomainError) as exc:
        mandates.request_approval(session, buyer, offer.id or 0, "a")
    assert exc.value.code is ErrorCode.STALE_OFFER
