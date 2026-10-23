from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import and_, or_
from sqlmodel import Session, col, select

from trajano.db import utc_now
from trajano.domain.errors import DomainError
from trajano.models import Agent, Message, Offer, Thread
from trajano.schemas import (
    ErrorCode,
    ExpiredPayload,
    InboxItem,
    InboxPage,
    MessageType,
    MessageView,
    Role,
    SendResult,
    ThreadStatus,
    ThreadView,
)

# Question is legal before ACCEPTED but it is not a turn: the inbox uses
# is_my_turn so a buyer waiting on an offer is not told it is their move.
_TURN = {
    MessageType.RFQ,
    MessageType.OFFER,
    MessageType.DECLINE,
    MessageType.COUNTER,
    MessageType.ACCEPT,
    MessageType.ANSWER,
    MessageType.ORDER_CONFIRM,
    MessageType.ORDER_REJECT,
    MessageType.SHIPPED,
    MessageType.DELIVERED,
}

_INBOX_PAGE = 100

_QUIET = (
    ThreadStatus.OPEN,
    ThreadStatus.QUOTE_REQUESTED,
    ThreadStatus.OFFERED,
    ThreadStatus.COUNTERED,
)


def _build_transitions() -> dict[tuple[ThreadStatus, MessageType, Role], ThreadStatus]:
    table: dict[tuple[ThreadStatus, MessageType, Role], ThreadStatus] = {}

    def allow(
        state: ThreadStatus,
        message_type: MessageType,
        role: Role,
        destination: ThreadStatus,
    ) -> None:
        table[(state, message_type, role)] = destination

    for state in _QUIET:
        allow(state, MessageType.QUESTION, Role.BUYER, state)
        allow(state, MessageType.ANSWER, Role.SELLER, state)
        allow(state, MessageType.WITHDRAW, Role.BUYER, ThreadStatus.WITHDRAWN)
    for state in (*_QUIET, ThreadStatus.ACCEPTED):
        allow(state, MessageType.EXPIRED, Role.SYSTEM, ThreadStatus.EXPIRED)

    allow(ThreadStatus.OPEN, MessageType.RFQ, Role.BUYER, ThreadStatus.QUOTE_REQUESTED)
    allow(ThreadStatus.QUOTE_REQUESTED, MessageType.OFFER, Role.SELLER, ThreadStatus.OFFERED)
    allow(ThreadStatus.QUOTE_REQUESTED, MessageType.DECLINE, Role.SELLER, ThreadStatus.DECLINED)
    allow(ThreadStatus.OFFERED, MessageType.COUNTER, Role.BUYER, ThreadStatus.COUNTERED)
    allow(ThreadStatus.OFFERED, MessageType.ACCEPT, Role.BUYER, ThreadStatus.ACCEPTED)
    allow(ThreadStatus.COUNTERED, MessageType.OFFER, Role.SELLER, ThreadStatus.OFFERED)
    allow(ThreadStatus.COUNTERED, MessageType.DECLINE, Role.SELLER, ThreadStatus.DECLINED)
    allow(ThreadStatus.ACCEPTED, MessageType.ORDER_CONFIRM, Role.SELLER, ThreadStatus.CONFIRMED)
    allow(ThreadStatus.ACCEPTED, MessageType.ORDER_REJECT, Role.SELLER, ThreadStatus.REJECTED)
    allow(ThreadStatus.CONFIRMED, MessageType.SHIPPED, Role.SELLER, ThreadStatus.SHIPPED)
    allow(ThreadStatus.SHIPPED, MessageType.DELIVERED, Role.SELLER, ThreadStatus.DELIVERED)
    return table


TRANSITIONS = _build_transitions()


def transition(state: ThreadStatus, message_type: MessageType, role: Role) -> ThreadStatus:
    try:
        return TRANSITIONS[(state, message_type, role)]
    except KeyError:
        raise DomainError(
            ErrorCode.INVALID_TRANSITION,
            f"{role.value} cannot send {message_type.value} while the thread is {state.value}",
        ) from None


def party_role(thread: Thread, agent: Agent) -> Role | None:
    if not agent.active:
        return None
    if agent.id == thread.buyer_agent_id and Role.BUYER.value in agent.roles:
        return Role.BUYER
    if agent.company_id == thread.seller_company_id and Role.SELLER.value in agent.roles:
        return Role.SELLER
    return None


def next_actions(thread: Thread, agent: Agent) -> list[MessageType]:
    role = party_role(thread, agent)
    if role is None:
        return []
    legal = {
        message_type
        for (state, message_type, actor) in TRANSITIONS
        if state == thread.status and actor == role
    }
    actions = [message_type for message_type in MessageType if message_type in legal]
    if thread.rounds_used >= 1:
        actions = [
            message_type for message_type in actions if message_type is not MessageType.COUNTER
        ]
    if not thread.pending_question:
        actions = [
            message_type for message_type in actions if message_type is not MessageType.ANSWER
        ]
    return actions


def is_my_turn(actions: list[MessageType]) -> bool:
    return any(action in _TURN for action in actions)


def encode_cursor(updated_at: datetime, thread_id: int) -> str:
    if updated_at.tzinfo is None:
        raise ValueError("updated_at must be timezone-aware")
    stamp = updated_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return f"{stamp}|{thread_id}"


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        stamp, raw_id = cursor.split("|", 1)
        parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        return parsed, int(raw_id)
    except (ValueError, IndexError):
        raise DomainError(ErrorCode.VALIDATION, "inbox cursor is invalid") from None


def send(
    session: Session,
    agent: Agent,
    *,
    message_type: MessageType,
    payload: object,
    idempotency_key: str,
    thread_id: int | None = None,
    listing_id: int | None = None,
    text: str | None = None,
) -> SendResult:
    """Apply one typed message. Augusto implements the body.

    1. idempotency.begin; on a hit, return the stored SendResult.
    2. Load the thread or create one for a first question (OPEN) or rfq
       (created in OPEN, then this transition). Refuse OWN_COMPANY.
    3. transition() must be legal. rounds_used >= 1 makes a counter ROUND_LIMIT.
       answer requires pending_question. Question sets the flag; answer clears it.
    4. Validate payload with PAYLOAD_MODELS[message_type]. Copy any text onto
       Message.text. Text is never authoritative.
    5. offer: unit_price >= price_floor, expires_at defaults to now + offer TTL
       and cannot be later than that, recompute pricing.landed_cost and refuse
       the offer unless qty, unit_price, lines, and total match, then offer_hash.
       The offer that answers a counter is final.
    6. accept: orders.accept in this same transaction.
    7. rfq: accounts.enforce_rfq_rate_limit.
    8. Persist Message and the new Thread.status together, then idempotency.complete.
    Expiry is lazy: an offer with now > expires_at becomes EXPIRED on read or accept.
    """
    raise NotImplementedError("Augusto: negotiation.send")


def get_inbox(
    session: Session,
    agent: Agent,
    *,
    awaiting: Literal["me"] = "me",
    since: str | None = None,
) -> InboxPage:
    """Threads on the caller's side of the deal.

    awaiting="me" keeps rows where is_my_turn(next_actions(...)).
    since is an exclusive encode_cursor. Order by (updated_at, id).
    Expire stale offers before deciding whose turn it is.
    """
    sides = []
    if Role.BUYER.value in agent.roles:
        sides.append(Thread.buyer_agent_id == agent.id)
    if Role.SELLER.value in agent.roles:
        sides.append(Thread.seller_company_id == agent.company_id)
    if not sides:
        return InboxPage(threads=[])

    offered = session.exec(
        select(Thread).where(or_(*sides), Thread.status == ThreadStatus.OFFERED)
    ).all()
    for thread in offered:
        _expire_if_stale(session, thread)

    query = select(Thread).where(or_(*sides))
    if since is not None:
        stamp, thread_id = decode_cursor(since)
        query = query.where(
            or_(
                Thread.updated_at > stamp,
                and_(Thread.updated_at == stamp, col(Thread.id) > thread_id),
            )
        )
    query = query.order_by(col(Thread.updated_at), col(Thread.id))

    items = []
    for thread in session.exec(query).all():
        if awaiting == "me" and not is_my_turn(next_actions(thread, agent)):
            continue
        items.append(
            InboxItem(
                thread_id=thread.id or 0,
                status=thread.status,
                updated_at=thread.updated_at,
                cursor=encode_cursor(thread.updated_at, thread.id or 0),
            )
        )
        if len(items) == _INBOX_PAGE:
            break
    return InboxPage(threads=items)


def get_thread(session: Session, agent: Agent, thread_id: int) -> ThreadView:
    """The thread, its messages, and next_actions. FORBIDDEN for anyone else."""
    thread = session.get(Thread, thread_id)
    if thread is None:
        raise DomainError(ErrorCode.NOT_FOUND, f"thread {thread_id} not found")
    if party_role(thread, agent) is None:
        raise DomainError(
            ErrorCode.FORBIDDEN,
            "this thread belongs to other parties",
            action="get_thread",
            agent_id=agent.id,
            detail={"thread_id": thread_id},
        )
    _expire_if_stale(session, thread)
    messages = session.exec(
        select(Message).where(Message.thread_id == thread.id).order_by(col(Message.id))
    ).all()
    return ThreadView(
        id=thread.id or 0,
        listing_id=thread.listing_id,
        status=thread.status,
        rounds_used=thread.rounds_used,
        pending_question=thread.pending_question,
        qty=thread.qty,
        ship_to=thread.ship_to,
        need_by=thread.need_by,
        updated_at=thread.updated_at,
        next_actions=next_actions(thread, agent),
        messages=[
            MessageView(
                id=message.id or 0,
                type=message.type,
                sender_agent_id=message.sender_agent_id,
                payload=message.payload,
                text=message.text,
                created_at=message.created_at,
            )
            for message in messages
        ],
    )


def _expire_if_stale(session: Session, thread: Thread) -> None:
    if thread.status is not ThreadStatus.OFFERED:
        return
    offer = session.exec(
        select(Offer).where(Offer.thread_id == thread.id).order_by(col(Offer.id).desc())
    ).first()
    now = utc_now()
    if offer is None or now <= offer.expires_at:
        return
    thread.status = transition(thread.status, MessageType.EXPIRED, Role.SYSTEM)
    thread.updated_at = now
    session.add(thread)
    session.add(
        Message(
            thread_id=thread.id or 0,
            sender_agent_id=None,
            type=MessageType.EXPIRED,
            payload=ExpiredPayload(offer_id=offer.id).model_dump(mode="json"),
            # Deterministic so two readers expiring the same offer collide on the index.
            idempotency_key=f"expire-offer-{offer.id}",
            created_at=now,
        )
    )
    session.flush()
