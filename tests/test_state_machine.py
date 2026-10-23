from datetime import UTC, datetime

import pytest

from trajano.domain.errors import DomainError
from trajano.domain.negotiation import (
    TRANSITIONS,
    decode_cursor,
    encode_cursor,
    is_my_turn,
    next_actions,
    transition,
)
from trajano.models import Agent, Thread
from trajano.schemas import ErrorCode, MessageType, Role, ThreadStatus


def test_every_state_type_and_role_is_decided() -> None:
    roles = (Role.BUYER, Role.SELLER, Role.SYSTEM)
    for state in ThreadStatus:
        for message_type in MessageType:
            for role in roles:
                key = (state, message_type, role)
                if key in TRANSITIONS:
                    assert transition(state, message_type, role) is TRANSITIONS[key]
                else:
                    with pytest.raises(DomainError) as exc:
                        transition(state, message_type, role)
                    assert exc.value.code is ErrorCode.INVALID_TRANSITION


def test_question_does_not_change_the_state() -> None:
    for state in (
        ThreadStatus.OPEN,
        ThreadStatus.QUOTE_REQUESTED,
        ThreadStatus.OFFERED,
        ThreadStatus.COUNTERED,
    ):
        assert transition(state, MessageType.QUESTION, Role.BUYER) is state
    with pytest.raises(DomainError):
        transition(ThreadStatus.ACCEPTED, MessageType.QUESTION, Role.BUYER)


def test_counter_is_one_round_and_the_reply_is_an_offer() -> None:
    assert (
        transition(ThreadStatus.OFFERED, MessageType.COUNTER, Role.BUYER) is ThreadStatus.COUNTERED
    )
    assert (
        transition(ThreadStatus.COUNTERED, MessageType.OFFER, Role.SELLER) is ThreadStatus.OFFERED
    )
    offered = _thread(status=ThreadStatus.OFFERED, rounds_used=0)
    assert MessageType.COUNTER in next_actions(offered, _buyer())
    offered.rounds_used = 1
    actions = next_actions(offered, _buyer())
    assert MessageType.COUNTER not in actions
    assert MessageType.ACCEPT in actions
    assert is_my_turn(actions)


def test_whose_turn_it_is() -> None:
    buyer = _buyer()
    seller = _seller()
    waiting = next_actions(_thread(status=ThreadStatus.QUOTE_REQUESTED), buyer)
    assert waiting == [MessageType.QUESTION, MessageType.WITHDRAW]
    assert not is_my_turn(waiting)
    selling = next_actions(_thread(status=ThreadStatus.QUOTE_REQUESTED), seller)
    assert MessageType.OFFER in selling
    assert MessageType.DECLINE in selling
    assert is_my_turn(selling)
    assert next_actions(_thread(status=ThreadStatus.OPEN), seller) == []
    asked = _thread(status=ThreadStatus.OPEN, pending_question=True)
    assert next_actions(asked, seller) == [MessageType.ANSWER]
    assert is_my_turn(next_actions(asked, seller))
    assert next_actions(_thread(status=ThreadStatus.ACCEPTED), seller) == [
        MessageType.ORDER_CONFIRM,
        MessageType.ORDER_REJECT,
    ]
    assert next_actions(_thread(status=ThreadStatus.SHIPPED), seller) == [MessageType.DELIVERED]
    assert next_actions(_thread(status=ThreadStatus.OFFERED), _stranger()) == []


def test_cursor_round_trip() -> None:
    moment = datetime(2026, 5, 1, 12, 30, 1, 250000, tzinfo=UTC)
    cursor = encode_cursor(moment, 9)
    assert decode_cursor(cursor) == (moment, 9)
    with pytest.raises(DomainError) as exc:
        decode_cursor("nope")
    assert exc.value.code is ErrorCode.VALIDATION


def test_every_message_type_has_a_payload_model() -> None:
    from trajano.schemas import PAYLOAD_MODELS

    assert set(PAYLOAD_MODELS) == set(MessageType)


def _thread(**overrides: object) -> Thread:
    data: dict[str, object] = {
        "listing_id": 1,
        "buyer_agent_id": 1,
        "seller_store_id": 1,
        "seller_company_id": 2,
        "status": ThreadStatus.OPEN,
        "rounds_used": 0,
        "pending_question": False,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    data.update(overrides)
    return Thread(**data)  # type: ignore[arg-type]


def _buyer() -> Agent:
    return Agent(id=1, company_id=3, name="Ada", api_key_hash="b", roles=["buyer"], active=True)


def _seller() -> Agent:
    return Agent(id=8, company_id=2, name="Hex", api_key_hash="s", roles=["seller"], active=True)


def _stranger() -> Agent:
    return Agent(id=4, company_id=9, name="No", api_key_hash="n", roles=["buyer"], active=True)
