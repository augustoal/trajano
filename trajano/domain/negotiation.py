from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ValidationError
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from trajano.config import get_settings
from trajano.db import utc_now
from trajano.domain import accounts, idempotency, orders, pricing
from trajano.domain.catalog import as_priceable
from trajano.domain.errors import DomainError, Refusal
from trajano.models import Agent, Listing, Message, Offer, Order, Store, Thread
from trajano.schemas import (
    PAYLOAD_MODELS,
    AcceptPayload,
    CostLine,
    CounterPayload,
    DeliveredPayload,
    ErrorCode,
    ExpiredPayload,
    InboxItem,
    InboxPage,
    MessageType,
    MessageView,
    OfferDraft,
    OfferFingerprint,
    OfferPayload,
    OrderConfirmPayload,
    OrderRejectPayload,
    QuestionPayload,
    RefusalCode,
    RefusedPayload,
    RfqPayload,
    Role,
    SendResult,
    ShippedPayload,
    ThreadStatus,
    ThreadView,
    offer_hash,
    quantize_money,
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

_REFUSALS = {code.value for code in RefusalCode}

_ORDER_STEPS = (
    MessageType.ORDER_CONFIRM,
    MessageType.ORDER_REJECT,
    MessageType.SHIPPED,
    MessageType.DELIVERED,
)

# The seller sends a draft; the server prices it, sets the hash, and stores an OfferPayload.
_INPUT_MODELS: dict[MessageType, type[BaseModel]] = {
    **PAYLOAD_MODELS,
    MessageType.OFFER: OfferDraft,
}

# Messages that may open a thread from a listing instead of naming one.
_OPENERS = (MessageType.QUESTION, MessageType.RFQ)

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
    body = _validate(message_type, payload)
    fingerprint = idempotency.request_fingerprint(
        {
            "op": "send",
            "type": message_type.value,
            "thread_id": thread_id,
            "listing_id": listing_id,
            "payload": body.model_dump(mode="json"),
            "text": text,
        }
    )
    stored = idempotency.begin(session, agent.id or 0, idempotency_key, fingerprint)
    if stored is not None:
        return SendResult.model_validate(stored)
    if text is not None and len(text) > get_settings().text_max_length:
        raise DomainError(ErrorCode.VALIDATION, "text is too long")

    # accept and the order steps name an offer or an order instead of a thread.
    hidden = False
    if thread_id is None and isinstance(body, AcceptPayload):
        thread, hidden = _thread_for_offer(session, body.offer_id), True
    elif thread_id is None and message_type in _ORDER_STEPS:
        thread, hidden = _thread_for_order(session, body.order_id), True  # type: ignore[attr-defined]
    elif thread_id is None:
        if message_type not in _OPENERS or listing_id is None:
            raise DomainError(ErrorCode.VALIDATION, "thread_id is required")
        thread = _thread_for_listing(session, agent, listing_id)
    else:
        thread = session.get(Thread, thread_id)
        if thread is None:
            raise DomainError(ErrorCode.NOT_FOUND, f"thread {thread_id} not found")
    if isinstance(body, QuestionPayload | RfqPayload) and body.listing_id != thread.listing_id:
        raise DomainError(ErrorCode.VALIDATION, "listing_id does not match the thread")

    role = party_role(thread, agent)
    if role is None and hidden:
        # Same answer as an id that does not exist, as orders.get_order does.
        target = "offer" if isinstance(body, AcceptPayload) else "order"
        raise DomainError(ErrorCode.NOT_FOUND, f"{target} not found")
    if role is None:
        raise DomainError(
            ErrorCode.FORBIDDEN,
            "this thread belongs to other parties",
            action="send",
            agent_id=agent.id,
            detail={"thread_id": thread.id, "type": message_type.value},
        )

    try:
        return _apply(session, agent, thread, role, message_type, body, idempotency_key, text)
    except DomainError as exc:
        if exc.code.value in _REFUSALS:
            exc.refusal = Refusal(
                thread_id=thread.id or 0,
                key=f"refused-{agent.id}-{idempotency_key}",
            )
        raise


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
        select(Thread).where(
            or_(*sides),
            col(Thread.status).in_([ThreadStatus.OFFERED, ThreadStatus.ACCEPTED]),
        )
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


def _apply(
    session: Session,
    agent: Agent,
    thread: Thread,
    role: Role,
    message_type: MessageType,
    body: BaseModel,
    idempotency_key: str,
    text: str | None,
) -> SendResult:
    _expire_if_stale(session, thread)
    if thread.status is ThreadStatus.EXPIRED and message_type in (
        MessageType.COUNTER,
        MessageType.ACCEPT,
    ):
        raise DomainError(ErrorCode.STALE_OFFER, "the offer expired")
    if message_type is MessageType.COUNTER and thread.rounds_used >= 1:
        raise DomainError(
            ErrorCode.ROUND_LIMIT,
            "only one counter is allowed; accept or withdraw the final offer",
            action="send",
            agent_id=agent.id,
            detail={"thread_id": thread.id},
        )
    destination = transition(thread.status, message_type, role)

    now = utc_now()
    stored_payload = body.model_dump(mode="json")
    offer: OfferPayload | None = None
    order: Order | None = None
    match body:
        case QuestionPayload():
            thread.pending_question = True
        case RfqPayload():
            if body.need_by < now.date():
                raise DomainError(ErrorCode.VALIDATION, "need_by is in the past")
            accounts.enforce_rfq_rate_limit(session, agent)
            thread.qty = body.qty
            thread.ship_to = body.ship_to
            thread.need_by = body.need_by
        case CounterPayload():
            _check_live_offer(session, thread, body.offer_id)
            thread.rounds_used += 1
        case OfferDraft():
            offer = _price_offer(
                session, agent, thread, body, final=thread.status is ThreadStatus.COUNTERED
            )
            stored_payload = offer.model_dump(mode="json")
        case AcceptPayload():
            accepted = _check_live_offer(session, thread, body.offer_id)
            if body.hash != accepted.hash:
                raise DomainError(
                    ErrorCode.STALE_OFFER,
                    "the hash does not match this offer; read the thread again",
                    action="accept",
                    agent_id=agent.id,
                    detail={"offer_id": accepted.id},
                )
            order = orders.accept(session, agent, accepted, body.mandate, idempotency_key)
            # The seller reads this message. The mandate carries the buyer's budget.
            stored_payload = {
                "offer_id": accepted.id,
                "hash": accepted.hash,
                "order_id": order.id,
                "mandate_id": order.mandate_id,
            }
        case OrderConfirmPayload():
            order = orders.confirm(session, agent, body.order_id, idempotency_key)
        case OrderRejectPayload():
            order = orders.reject(session, agent, body.order_id, body.reason_code, idempotency_key)
        case ShippedPayload():
            order = orders.ship(session, agent, body.order_id, body.tracking, idempotency_key)
        case DeliveredPayload():
            order = orders.deliver(session, agent, body.order_id, idempotency_key)
    if message_type is MessageType.ANSWER:
        if not thread.pending_question:
            raise DomainError(ErrorCode.INVALID_TRANSITION, "no question is waiting for an answer")
        thread.pending_question = False

    message = Message(
        thread_id=thread.id or 0,
        sender_agent_id=agent.id,
        type=message_type,
        payload=stored_payload,
        text=text,
        idempotency_key=idempotency_key,
        created_at=now,
    )
    thread.status = destination
    thread.updated_at = now
    session.add(thread)
    session.add(message)
    session.flush()
    if offer is not None:
        session.add(
            Offer(
                thread_id=thread.id or 0,
                message_id=message.id or 0,
                **offer.model_dump(exclude={"listing_version", "lines"}),
                lines=[line.model_dump(mode="json") for line in offer.lines],
            )
        )
        session.flush()

    result = SendResult(
        thread_id=thread.id or 0,
        message_id=message.id or 0,
        status=thread.status,
        next_actions=next_actions(thread, agent),
        order_id=order.id if order is not None else None,
    )
    idempotency.complete(session, agent.id or 0, idempotency_key, result.model_dump(mode="json"))
    return result


def record_refusal(refusal: Refusal, code: ErrorCode, message: str) -> None:
    """Add a system refused message in its own transaction, after send rolled back."""
    from trajano.db import get_engine

    with Session(get_engine(), expire_on_commit=False) as session:
        session.add(
            Message(
                thread_id=refusal.thread_id,
                sender_agent_id=None,
                type=MessageType.REFUSED,
                payload=RefusedPayload(code=RefusalCode(code.value), message=message).model_dump(
                    mode="json"
                ),
                idempotency_key=refusal.key,
                created_at=utc_now(),
            )
        )
        try:
            session.commit()
        except IntegrityError:
            # A retried refusal already left its message.
            session.rollback()


def _validate(message_type: MessageType, payload: object) -> BaseModel:
    model = _INPUT_MODELS[message_type]
    if isinstance(payload, model):
        return payload
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise DomainError(ErrorCode.VALIDATION, f"invalid {message_type.value} payload") from exc


def _thread_for_listing(session: Session, agent: Agent, listing_id: int) -> Thread:
    """Reuse the buyer's live thread on this listing, or open one in OPEN."""
    listing = session.get(Listing, listing_id)
    if listing is None:
        raise DomainError(ErrorCode.NOT_FOUND, f"listing {listing_id} not found")
    if not listing.is_current:
        raise DomainError(
            ErrorCode.CONFLICT, f"listing {listing_id} has a newer version; read it again"
        )
    store = session.get(Store, listing.store_id)
    if store is None:
        raise DomainError(ErrorCode.INTERNAL, "listing is missing its store", retryable=True)
    if store.company_id == agent.company_id:
        raise DomainError(
            ErrorCode.OWN_COMPANY,
            "you cannot buy from your own company",
            action="send",
            agent_id=agent.id,
            detail={"listing_id": listing_id},
        )
    live = session.exec(
        select(Thread)
        .where(
            Thread.buyer_agent_id == agent.id,
            Thread.listing_id == listing_id,
            col(Thread.status).in_(_QUIET),
        )
        .order_by(col(Thread.id).desc())
    ).first()
    if live is not None:
        return live
    thread = Thread(
        listing_id=listing_id,
        buyer_agent_id=agent.id or 0,
        seller_store_id=store.id or 0,
        seller_company_id=store.company_id,
        status=ThreadStatus.OPEN,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    session.add(thread)
    session.flush()
    return thread


def _check_live_offer(session: Session, thread: Thread, offer_id: int) -> Offer:
    latest = session.exec(
        select(Offer).where(Offer.thread_id == thread.id).order_by(col(Offer.id).desc())
    ).first()
    if latest is None or session.get(Offer, offer_id) is None:
        raise DomainError(ErrorCode.NOT_FOUND, f"offer {offer_id} not found")
    if latest.id != offer_id:
        raise DomainError(ErrorCode.STALE_OFFER, f"offer {offer_id} is not the current offer")
    return latest


def _thread_for_offer(session: Session, offer_id: int) -> Thread:
    offer = session.get(Offer, offer_id)
    thread = session.get(Thread, offer.thread_id) if offer is not None else None
    if thread is None:
        raise DomainError(ErrorCode.NOT_FOUND, "offer not found")
    return thread


def _thread_for_order(session: Session, order_id: int) -> Thread:
    order = session.get(Order, order_id)
    offer = session.get(Offer, order.offer_id) if order is not None else None
    thread = session.get(Thread, offer.thread_id) if offer is not None else None
    if thread is None:
        raise DomainError(ErrorCode.NOT_FOUND, "order not found")
    return thread


def _price_offer(
    session: Session, agent: Agent, thread: Thread, draft: OfferDraft, *, final: bool
) -> OfferPayload:
    """Check the seller's draft against pricing.landed_cost and return the hashed offer.

    qty is the quantity requested; the offer stores the billed quantity, whole packs.
    The quote is on the current version of the thread's listing.
    """
    quoted = session.get(Listing, thread.listing_id)
    listing = (
        session.exec(
            select(Listing).where(
                Listing.store_id == quoted.store_id,
                Listing.sku == quoted.sku,
                Listing.is_current == True,  # noqa: E712
            )
        ).first()
        if quoted is not None
        else None
    )
    if listing is None or thread.ship_to is None:
        raise DomainError(ErrorCode.INTERNAL, "thread is missing its listing", retryable=True)
    if draft.ship_to is not None and draft.ship_to != thread.ship_to:
        raise DomainError(ErrorCode.VALIDATION, f"ship_to must be {thread.ship_to}, as in the rfq")
    if draft.currency != listing.currency:
        raise DomainError(ErrorCode.VALIDATION, f"currency must be {listing.currency}")
    if draft.unit_price < listing.price_floor:
        raise DomainError(
            ErrorCode.BELOW_FLOOR,
            "unit_price is below this listing's price floor",
            action="offer",
            agent_id=agent.id,
            detail={"thread_id": thread.id, "listing_id": listing.id},
        )
    landed = pricing.landed_cost(
        as_priceable(listing), draft.qty, thread.ship_to, unit_price=draft.unit_price
    )
    if listing.stock < landed.qty_billed:
        raise DomainError(
            ErrorCode.OUT_OF_STOCK, f"{listing.stock} in stock, {landed.qty_billed} needed"
        )
    if quantize_money(draft.total) != quantize_money(landed.total) or _line_keys(
        draft.lines
    ) != _line_keys(landed.lines):
        raise DomainError(
            ErrorCode.VALIDATION,
            f"lines and total must match the landed cost; the total is {landed.total}",
        )

    now = utc_now()
    latest = now + timedelta(hours=get_settings().offer_ttl_hours)
    expires_at = draft.expires_at or latest
    if expires_at.tzinfo is None:
        raise DomainError(ErrorCode.VALIDATION, "expires_at needs a timezone")
    if not now < expires_at <= latest:
        raise DomainError(
            ErrorCode.VALIDATION,
            f"expires_at must be within {get_settings().offer_ttl_hours} hours from now",
        )
    if draft.delivery_date < now.date():
        raise DomainError(ErrorCode.VALIDATION, "delivery_date is in the past")

    fingerprint = OfferFingerprint(
        listing_id=listing.id or 0,
        listing_version=listing.version,
        lines=landed.lines,
        unit_price=landed.unit_price,
        qty=landed.qty_billed,
        total=landed.total,
        currency=landed.currency,
        ship_to=thread.ship_to,
        delivery_date=draft.delivery_date,
        # The hash keeps whole seconds, so the stored value does too.
        expires_at=expires_at.astimezone(UTC).replace(microsecond=0),
        final=final,
    )
    return OfferPayload(**fingerprint.model_dump(), hash=offer_hash(fingerprint))


def _line_keys(lines: list[CostLine]) -> list[tuple[str, str]]:
    return [(line.kind, quantize_money(line.amount)) for line in lines]


def _expire_if_stale(session: Session, thread: Thread) -> None:
    if thread.status is ThreadStatus.ACCEPTED:
        orders.expire_unconfirmed(session, thread)
        return
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
