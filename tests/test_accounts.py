import pytest
from sqlmodel import select

from tests.factories import make_buyer
from trajano.domain.accounts import authenticate, create_agent, create_company, hash_api_key
from trajano.domain.errors import DomainError
from trajano.models import AuditEvent
from trajano.schemas import ErrorCode, Role


def test_api_key_is_issued_once_and_stored_as_a_hash(session) -> None:
    _agent, raw = make_buyer(session)
    assert raw.startswith("tj_")
    assert hash_api_key(raw) != raw
    found = authenticate(session, raw)
    assert found.id == _agent.id


def test_unknown_and_inactive_keys_look_the_same(session) -> None:
    agent, raw = make_buyer(session)
    agent.active = False
    session.flush()
    with pytest.raises(DomainError) as inactive:
        authenticate(session, raw)
    with pytest.raises(DomainError) as unknown:
        authenticate(session, "tj_nope")
    assert inactive.value.code is ErrorCode.UNAUTHORIZED
    assert unknown.value.code is ErrorCode.UNAUTHORIZED
    assert inactive.value.message == unknown.value.message
    assert inactive.value.action == "authenticate"


def test_missing_key_is_not_an_audit_event(session) -> None:
    with pytest.raises(DomainError) as exc:
        authenticate(session, None)
    assert exc.value.action is None


def test_duplicate_domain_and_system_role_are_rejected(session) -> None:
    create_company(session, "One", domain="Shared.Example")
    with pytest.raises(DomainError) as exc:
        create_company(session, "Two", domain="shared.example")
    assert exc.value.code is ErrorCode.CONFLICT
    company = create_company(session, "Three")
    with pytest.raises(DomainError) as role_error:
        create_agent(session, company.id or 0, "sys", [Role.SYSTEM])
    assert role_error.value.code is ErrorCode.VALIDATION


def test_bad_key_through_a_tool_is_audited(session, monkeypatch: pytest.MonkeyPatch) -> None:
    from trajano.interfaces import support
    from trajano.interfaces.mcp_buyer import buyer_mcp

    make_buyer(session)
    session.commit()
    monkeypatch.setattr(support, "current_api_key", lambda: "tj_not-a-real-key")

    async def call():
        from tests.mcp_memory import connect

        async with connect(buyer_mcp) as client:
            return await client.call_tool("get_listing", {"listing_id": 1})

    import asyncio

    result = asyncio.run(call())
    body = _body(result)
    assert body["error"]["code"] == "UNAUTHORIZED"
    rows = list(session.exec(select(AuditEvent)))
    assert len(rows) == 1
    assert rows[0].action == "authenticate"
    assert rows[0].result == "UNAUTHORIZED"


def _body(result) -> dict:
    if result.structuredContent:
        return result.structuredContent
    import json

    return json.loads(result.content[0].text)
