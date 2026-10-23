from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy import JSON, Column, Index, Numeric, UniqueConstraint, text
from sqlalchemy import Enum as SAEnum
from sqlmodel import Field, SQLModel

from trajano.db import UTCDateTime, utc_now
from trajano.schemas import (
    ApprovalStatus,
    MessageType,
    OrderStatus,
    ThreadStatus,
)


def _enum_column(enum_cls: type[Enum]) -> Column:
    # Store the value ("question"), not the member name ("QUESTION"), as varchar
    # so Postgres does not need an ALTER TYPE when a message type is added.
    return Column(
        SAEnum(
            enum_cls,
            native_enum=False,
            create_constraint=False,
            length=32,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
    )


def _money(*, nullable: bool = False) -> Column:
    return Column(Numeric(14, 4), nullable=nullable)


def _when(*, nullable: bool = False) -> Column:
    return Column(UTCDateTime(), nullable=nullable)


class Company(SQLModel, table=True):
    __tablename__ = "companies"

    id: int | None = Field(default=None, primary_key=True)
    name: str
    # The company's web domain, not the domain layer.
    domain: str | None = Field(default=None, unique=True, index=True)
    verified: bool = False


class Agent(SQLModel, table=True):
    __tablename__ = "agents"

    id: int | None = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    name: str
    api_key_hash: str = Field(unique=True, index=True)
    roles: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    active: bool = True


class Store(SQLModel, table=True):
    __tablename__ = "stores"

    id: int | None = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    name: str


class Listing(SQLModel, table=True):
    """One row per version. Offers point at this id, which is the version they quoted."""

    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("store_id", "sku", "version", name="uq_listing_version"),
        Index(
            "uq_listing_one_current",
            "store_id",
            "sku",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current"),
        ),
        Index("ix_listing_search", "diameter", "length_mm", "material"),
    )

    id: int | None = Field(default=None, primary_key=True)
    store_id: int = Field(foreign_key="stores.id", index=True)
    sku: str
    version: int = 1
    is_current: bool = True
    diameter: str
    length_mm: Decimal = Field(sa_column=Column(Numeric(8, 3), nullable=False))
    material: str
    grade: str
    standard: str
    price_tiers: list[dict[str, Any]] = Field(sa_column=Column(JSON, nullable=False))
    # Private. Buyer reads go through ListingPublic, which has no floor.
    price_floor: Decimal = Field(sa_column=_money())
    moq: int
    pack_size: int
    stock: int
    lead_time_days: int
    ships_from: str
    seller_notes: str | None = None
    currency: str = "USD"
    created_at: datetime = Field(default_factory=utc_now, sa_column=_when())


class Thread(SQLModel, table=True):
    __tablename__ = "threads"
    __table_args__ = (
        Index("ix_thread_buyer_updated", "buyer_agent_id", "updated_at"),
        Index("ix_thread_seller_updated", "seller_company_id", "updated_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    listing_id: int = Field(foreign_key="listings.id", index=True)
    buyer_agent_id: int = Field(foreign_key="agents.id", index=True)
    seller_store_id: int = Field(foreign_key="stores.id", index=True)
    # Denormalized so "is this my company's thread?" does not join stores.
    seller_company_id: int = Field(foreign_key="companies.id", index=True)
    status: ThreadStatus = Field(default=ThreadStatus.OPEN, sa_column=_enum_column(ThreadStatus))
    # Buyer counters already sent. A counter is legal only at 0. The offer that
    # answers it is final, and another counter is ROUND_LIMIT.
    rounds_used: int = 0
    pending_question: bool = False
    qty: int | None = None
    ship_to: str | None = None
    need_by: date | None = None
    created_at: datetime = Field(default_factory=utc_now, sa_column=_when())
    updated_at: datetime = Field(default_factory=utc_now, sa_column=_when())


class Message(SQLModel, table=True):
    __tablename__ = "messages"
    __table_args__ = (
        # sender_agent_id is NULL for Trajano itself. coalesce makes those keys
        # unique too: SQLite treats NULL as distinct in a unique constraint.
        # Agent ids start at 1, so 0 does not collide with a real sender.
        Index(
            "uq_message_idempotency",
            text("coalesce(sender_agent_id, 0)"),
            "idempotency_key",
            unique=True,
        ),
        Index("ix_message_thread", "thread_id", "id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    thread_id: int = Field(foreign_key="threads.id", index=True)
    sender_agent_id: int | None = Field(default=None, foreign_key="agents.id")
    type: MessageType = Field(sa_column=_enum_column(MessageType))
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    text: str | None = None
    idempotency_key: str
    created_at: datetime = Field(default_factory=utc_now, sa_column=_when())


class Offer(SQLModel, table=True):
    __tablename__ = "offers"

    id: int | None = Field(default=None, primary_key=True)
    thread_id: int = Field(foreign_key="threads.id", index=True)
    message_id: int = Field(foreign_key="messages.id")
    listing_id: int = Field(foreign_key="listings.id", index=True)
    lines: list[dict[str, Any]] = Field(sa_column=Column(JSON, nullable=False))
    unit_price: Decimal = Field(sa_column=_money())
    qty: int
    total: Decimal = Field(sa_column=_money())
    currency: str = "USD"
    ship_to: str
    delivery_date: date
    expires_at: datetime = Field(sa_column=_when())
    hash: str = Field(index=True)
    final: bool = False
    created_at: datetime = Field(default_factory=utc_now, sa_column=_when())


class Order(SQLModel, table=True):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("buyer_agent_id", "idempotency_key", name="uq_order_idempotency"),
    )

    id: int | None = Field(default=None, primary_key=True)
    offer_id: int = Field(foreign_key="offers.id", index=True)
    buyer_company_id: int = Field(foreign_key="companies.id", index=True)
    buyer_agent_id: int = Field(foreign_key="agents.id", index=True)
    seller_company_id: int = Field(foreign_key="companies.id", index=True)
    status: OrderStatus = Field(default=OrderStatus.SUBMITTED, sa_column=_enum_column(OrderStatus))
    mandate_id: int = Field(foreign_key="mandates.id")
    idempotency_key: str
    tracking: str | None = None
    reason_code: str | None = None
    created_at: datetime = Field(default_factory=utc_now, sa_column=_when())
    confirmed_at: datetime | None = Field(default=None, sa_column=_when(nullable=True))
    rejected_at: datetime | None = Field(default=None, sa_column=_when(nullable=True))
    shipped_at: datetime | None = Field(default=None, sa_column=_when(nullable=True))
    delivered_at: datetime | None = Field(default=None, sa_column=_when(nullable=True))


class Mandate(SQLModel, table=True):
    __tablename__ = "mandates"

    id: int | None = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    agent_id: int = Field(foreign_key="agents.id", index=True)
    category: str | None = "fasteners"
    max_unit_price: Decimal = Field(sa_column=_money())
    max_total: Decimal = Field(sa_column=_money())
    max_qty: int
    currency: str = "USD"
    expires_at: datetime = Field(sa_column=_when())
    revoked_at: datetime | None = Field(default=None, sa_column=_when(nullable=True))
    signature: str
    created_at: datetime = Field(default_factory=utc_now, sa_column=_when())


class ApprovalRequest(SQLModel, table=True):
    __tablename__ = "approval_requests"

    id: int | None = Field(default=None, primary_key=True)
    offer_id: int = Field(foreign_key="offers.id", index=True)
    agent_id: int = Field(foreign_key="agents.id", index=True)
    status: ApprovalStatus = Field(
        default=ApprovalStatus.PENDING, sa_column=_enum_column(ApprovalStatus)
    )
    mandate_id: int | None = Field(default=None, foreign_key="mandates.id")
    created_at: datetime = Field(default_factory=utc_now, sa_column=_when())


class AuditEvent(SQLModel, table=True):
    __tablename__ = "audit_events"

    id: int | None = Field(default=None, primary_key=True)
    agent_id: int | None = Field(default=None, foreign_key="agents.id", index=True)
    action: str
    result: str
    detail: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(default_factory=utc_now, sa_column=_when())


class IdempotencyKey(SQLModel, table=True):
    """Replay record for writes that are not messages or orders.

    Messages and orders also have their own unique (agent, key) indexes.
    Those indexes are the race backstop; this table returns the original body.
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("agent_id", "key", name="uq_idempotency_agent_key"),)

    id: int | None = Field(default=None, primary_key=True)
    agent_id: int = Field(foreign_key="agents.id", index=True)
    key: str
    fingerprint: str
    response: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now, sa_column=_when())
