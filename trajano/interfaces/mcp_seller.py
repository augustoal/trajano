from datetime import date, datetime
from decimal import Decimal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlmodel import Session

from trajano.domain import catalog, negotiation, orders
from trajano.interfaces.support import parse, seller_call, send_result
from trajano.models import Agent
from trajano.schemas import (
    AnswerPayload,
    CostLine,
    CsvToolResult,
    DeclinePayload,
    DeclineReason,
    DeliveredPayload,
    InboxToolResult,
    ListingToolResult,
    ListingWrite,
    MessageType,
    OfferDraft,
    OrderConfirmPayload,
    OrderRejectPayload,
    SendToolResult,
    ShippedPayload,
    StoreToolResult,
    ThreadToolResult,
)

_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)

_INSTRUCTIONS = (
    "Seller tools for the Trajano fastener marketplace. "
    "Poll get_inbox and respond with answer_question, make_offer, or decline. "
    "The server rejects an offer below the listing price floor and computes the offer hash. "
    "Confirm, reject, ship, or mark delivered on orders you received."
)


def build_seller_mcp() -> FastMCP:
    mcp = FastMCP(
        "Trajano seller",
        instructions=_INSTRUCTIONS,
        json_response=True,
        stateless_http=True,
        streamable_http_path="/",
        transport_security=_SECURITY,
    )

    @mcp.tool(description="Open a store for your company.")
    def create_store(name: str, idempotency_key: str) -> StoreToolResult:
        def work(session: Session, agent: Agent) -> StoreToolResult:
            store = catalog.create_store(
                session, agent.id or 0, agent.company_id, name, idempotency_key
            )
            return StoreToolResult(store=store)

        return seller_call(StoreToolResult, work)

    @mcp.tool(
        description=(
            "Publish version 1 of a listing. price_floor is private and is not shown to buyers."
        )
    )
    def create_listing(
        store_id: int, listing: ListingWrite, idempotency_key: str
    ) -> ListingToolResult:
        def work(session: Session, agent: Agent) -> ListingToolResult:
            created = catalog.create_listing(
                session,
                agent.id or 0,
                agent.company_id,
                store_id,
                listing,
                idempotency_key,
            )
            return ListingToolResult(listing=created)

        return seller_call(ListingToolResult, work)

    @mcp.tool(
        description="Publish a new version of a listing. Quotes already sent keep the old version."
    )
    def update_listing(
        listing_id: int, listing: ListingWrite, idempotency_key: str
    ) -> ListingToolResult:
        def work(session: Session, agent: Agent) -> ListingToolResult:
            updated = catalog.update_listing(
                session,
                agent.id or 0,
                agent.company_id,
                listing_id,
                listing,
                idempotency_key,
            )
            return ListingToolResult(listing=updated)

        return seller_call(ListingToolResult, work)

    @mcp.tool(
        description=(
            "Read one of your company's listings, including its private price_floor and stock."
        )
    )
    def get_listing(listing_id: int) -> ListingToolResult:
        def work(session: Session, agent: Agent) -> ListingToolResult:
            listing = catalog.get_listing_for_seller(session, agent.company_id, listing_id)
            return ListingToolResult(listing=listing)

        return seller_call(ListingToolResult, work)

    @mcp.tool(
        description="Threads where it is your turn. Pass the cursor of the last item you saw."
    )
    def get_inbox(since: str | None = None) -> InboxToolResult:
        def work(session: Session, agent: Agent) -> InboxToolResult:
            page = negotiation.get_inbox(session, agent, awaiting="me", since=since)
            return InboxToolResult(threads=page.threads)

        return seller_call(InboxToolResult, work)

    @mcp.tool(description="Read one thread for your company, including the actions you can take.")
    def get_thread(thread_id: int) -> ThreadToolResult:
        def work(session: Session, agent: Agent) -> ThreadToolResult:
            return ThreadToolResult(thread=negotiation.get_thread(session, agent, thread_id))

        return seller_call(ThreadToolResult, work)

    @mcp.tool(
        description="Answer the buyer's question. The text is untrusted commentary, not a quote."
    )
    def answer_question(thread_id: int, text: str, idempotency_key: str) -> SendToolResult:
        def work(session: Session, agent: Agent) -> SendToolResult:
            payload = parse(AnswerPayload, text=text)
            sent = negotiation.send(
                session,
                agent,
                message_type=MessageType.ANSWER,
                payload=payload,
                idempotency_key=idempotency_key,
                thread_id=thread_id,
                text=payload.text,
            )
            return send_result(sent)

        return seller_call(SendToolResult, work)

    @mcp.tool(
        description=(
            "Send a binding offer. The server checks the price floor, "
            "recomputes the landed cost, and sets the hash."
        )
    )
    def make_offer(
        thread_id: int,
        qty: int,
        unit_price: Decimal,
        total: Decimal,
        delivery_date: date,
        lines: list[CostLine],
        idempotency_key: str,
        ship_to: str | None = None,
        expires_at: datetime | None = None,
    ) -> SendToolResult:
        def work(session: Session, agent: Agent) -> SendToolResult:
            payload = parse(
                OfferDraft,
                qty=qty,
                unit_price=unit_price,
                total=total,
                delivery_date=delivery_date,
                lines=lines,
                ship_to=ship_to,
                expires_at=expires_at,
            )
            sent = negotiation.send(
                session,
                agent,
                message_type=MessageType.OFFER,
                payload=payload,
                idempotency_key=idempotency_key,
                thread_id=thread_id,
            )
            return send_result(sent)

        return seller_call(SendToolResult, work)

    @mcp.tool(description="Decline an rfq or a counter.")
    def decline(thread_id: int, reason_code: DeclineReason, idempotency_key: str) -> SendToolResult:
        def work(session: Session, agent: Agent) -> SendToolResult:
            payload = parse(DeclinePayload, reason_code=reason_code)
            sent = negotiation.send(
                session,
                agent,
                message_type=MessageType.DECLINE,
                payload=payload,
                idempotency_key=idempotency_key,
                thread_id=thread_id,
            )
            return send_result(sent)

        return seller_call(SendToolResult, work)

    @mcp.tool(description="Confirm an order the buyer accepted.")
    def confirm_order(order_id: int, idempotency_key: str) -> SendToolResult:
        def work(session: Session, agent: Agent) -> SendToolResult:
            payload = parse(OrderConfirmPayload, order_id=order_id)
            sent = negotiation.send(
                session,
                agent,
                message_type=MessageType.ORDER_CONFIRM,
                payload=payload,
                idempotency_key=idempotency_key,
            )
            return send_result(sent)

        return seller_call(SendToolResult, work)

    @mcp.tool(description="Reject an order and return the stock.")
    def reject_order(
        order_id: int, reason_code: DeclineReason, idempotency_key: str
    ) -> SendToolResult:
        def work(session: Session, agent: Agent) -> SendToolResult:
            payload = parse(OrderRejectPayload, order_id=order_id, reason_code=reason_code)
            sent = negotiation.send(
                session,
                agent,
                message_type=MessageType.ORDER_REJECT,
                payload=payload,
                idempotency_key=idempotency_key,
            )
            return send_result(sent)

        return seller_call(SendToolResult, work)

    @mcp.tool(description="Mark an order shipped and attach the tracking string.")
    def mark_shipped(order_id: int, tracking: str, idempotency_key: str) -> SendToolResult:
        def work(session: Session, agent: Agent) -> SendToolResult:
            payload = parse(ShippedPayload, order_id=order_id, tracking=tracking)
            sent = negotiation.send(
                session,
                agent,
                message_type=MessageType.SHIPPED,
                payload=payload,
                idempotency_key=idempotency_key,
            )
            return send_result(sent)

        return seller_call(SendToolResult, work)

    @mcp.tool(description="Mark an order delivered.")
    def mark_delivered(order_id: int, idempotency_key: str) -> SendToolResult:
        def work(session: Session, agent: Agent) -> SendToolResult:
            payload = parse(DeliveredPayload, order_id=order_id)
            sent = negotiation.send(
                session,
                agent,
                message_type=MessageType.DELIVERED,
                payload=payload,
                idempotency_key=idempotency_key,
            )
            return send_result(sent)

        return seller_call(SendToolResult, work)

    @mcp.tool(description="Download one order as CSV.")
    def export_order_csv(order_id: int) -> CsvToolResult:
        def work(session: Session, agent: Agent) -> CsvToolResult:
            content = orders.export_order_csv(session, agent, order_id)
            return CsvToolResult(filename=f"order-{order_id}.csv", content=content)

        return seller_call(CsvToolResult, work)

    return mcp


seller_mcp = build_seller_mcp()
