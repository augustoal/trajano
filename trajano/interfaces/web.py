import json
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated
from urllib.parse import quote as urlquote

from fastapi import APIRouter, FastAPI, Form, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlmodel import Session

from trajano.db import get_settings
from trajano.domain import accounts, catalog, mandates, negotiation
from trajano.domain.accounts import authenticate, require_role
from trajano.domain.errors import DomainError
from trajano.interfaces.support import attempt, parse
from trajano.models import Agent
from trajano.schemas import (
    AcceptPayload,
    CounterPayload,
    ErrorCode,
    MessageType,
    QuestionPayload,
    RfqPayload,
    Role,
    SearchSpec,
    WithdrawPayload,
    WithdrawReason,
)

COOKIE = "trajano_api_key"
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
router = APIRouter()

_FILTERS = (
    "diameter",
    "length_mm",
    "length_tolerance_mm",
    "material",
    "grade",
    "standard",
    "ship_to",
    "need_by",
    "qty",
)


def install(app: FastAPI) -> None:
    app.include_router(router)

    @app.exception_handler(RequestValidationError)
    def invalid_form(request: Request, _exc: RequestValidationError) -> Response:
        return TEMPLATES.TemplateResponse(
            request,
            "error.html",
            {
                "error": "Check the form and try again.",
                "error_code": "VALIDATION",
                "agent_name": None,
            },
            status_code=422,
        )


@router.get("/login")
def login_form(request: Request, next: str = "/") -> Response:
    return TEMPLATES.TemplateResponse(
        request,
        "login.html",
        {"next": _safe_next(next), "error": None, "error_code": None, "agent_name": None},
    )


@router.post("/login")
def login(
    request: Request,
    api_key: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
) -> Response:
    outcome = attempt(lambda session: authenticate(session, api_key.strip()))
    destination = _safe_next(next)
    if outcome.error is not None:
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {
                "next": destination,
                "error": "That API key is not valid.",
                "error_code": outcome.error.code.value,
                "agent_name": None,
            },
            status_code=401,
        )
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(
        COOKIE,
        api_key.strip(),
        httponly=True,
        samesite="lax",
        secure=get_settings().cookie_secure,
        max_age=60 * 60 * 24 * 14,
        path="/",
    )
    return response


@router.post("/logout")
def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response


@router.get("/")
def listings(request: Request) -> Response:
    def work(session: Session, agent: Agent) -> object:
        if not _submitted(request):
            return None
        return catalog.search(session, _spec_from_query(request))

    return _buyer_page(request, "listings.html", work, extra=_filter_values(request))


@router.get("/listing/{listing_id}")
def listing_page(request: Request, listing_id: int) -> Response:
    def work(session: Session, agent: Agent) -> object:
        del agent
        return catalog.get_listing(session, listing_id)

    return _buyer_page(
        request,
        "listing.html",
        work,
        extra={"question_key": str(uuid.uuid4()), "quote_key": str(uuid.uuid4())},
    )


@router.post("/listing/{listing_id}/question")
def ask(
    request: Request,
    listing_id: int,
    text: Annotated[str, Form()],
    idempotency_key: Annotated[str, Form()],
) -> Response:
    def work(session: Session, agent: Agent) -> object:
        payload = parse(QuestionPayload, listing_id=listing_id, text=text)
        sent = negotiation.send(
            session,
            agent,
            message_type=MessageType.QUESTION,
            payload=payload,
            idempotency_key=idempotency_key,
            listing_id=listing_id,
            text=payload.text,
        )
        return _to_thread(sent.thread_id)

    return _buyer_page(request, "listing.html", work)


@router.post("/listing/{listing_id}/quote")
def request_quote_form(
    request: Request,
    listing_id: int,
    qty: Annotated[int, Form()],
    ship_to: Annotated[str, Form()],
    need_by: Annotated[date, Form()],
    idempotency_key: Annotated[str, Form()],
) -> Response:
    def work(session: Session, agent: Agent) -> object:
        payload = parse(
            RfqPayload,
            listing_id=listing_id,
            qty=qty,
            ship_to=ship_to,
            need_by=need_by,
        )
        sent = negotiation.send(
            session,
            agent,
            message_type=MessageType.RFQ,
            payload=payload,
            idempotency_key=idempotency_key,
            listing_id=listing_id,
        )
        return _to_thread(sent.thread_id)

    return _buyer_page(request, "listing.html", work)


@router.get("/threads")
def threads(request: Request) -> Response:
    def work(session: Session, agent: Agent) -> object:
        return negotiation.get_inbox(session, agent, awaiting="me")

    return _buyer_page(request, "threads.html", work)


@router.get("/threads/{thread_id}")
def thread_page(request: Request, thread_id: int) -> Response:
    def work(session: Session, agent: Agent) -> object:
        return negotiation.get_thread(session, agent, thread_id)

    return _buyer_page(request, "thread.html", work, extra=_thread_forms())


@router.post("/threads/{thread_id}/counter")
def counter(
    request: Request,
    thread_id: int,
    offer_id: Annotated[int, Form()],
    idempotency_key: Annotated[str, Form()],
    unit_price: Annotated[str, Form()] = "",
    qty: Annotated[str, Form()] = "",
) -> Response:
    def work(session: Session, agent: Agent) -> object:
        payload = parse(
            CounterPayload,
            offer_id=offer_id,
            unit_price=unit_price.strip() or None,
            qty=qty.strip() or None,
        )
        sent = negotiation.send(
            session,
            agent,
            message_type=MessageType.COUNTER,
            payload=payload,
            idempotency_key=idempotency_key,
            thread_id=thread_id,
        )
        return _to_thread(sent.thread_id)

    return _buyer_page(request, "thread.html", work, extra=_thread_forms())


@router.post("/threads/{thread_id}/accept")
def accept(
    request: Request,
    thread_id: int,
    offer_id: Annotated[int, Form()],
    offer_hash: Annotated[str, Form()],
    mandate: Annotated[str, Form()],
    idempotency_key: Annotated[str, Form()],
) -> Response:
    def work(session: Session, agent: Agent) -> object:
        try:
            document = json.loads(mandate)
        except json.JSONDecodeError as exc:
            raise DomainError(ErrorCode.VALIDATION, "mandate must be the signed JSON") from exc
        payload = parse(
            AcceptPayload,
            offer_id=offer_id,
            hash=offer_hash.strip(),
            mandate=document,
            idempotency_key=idempotency_key,
        )
        sent = negotiation.send(
            session,
            agent,
            message_type=MessageType.ACCEPT,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        return _to_thread(sent.thread_id)

    return _buyer_page(request, "thread.html", work, extra=_thread_forms())


@router.post("/threads/{thread_id}/withdraw")
def withdraw(
    request: Request,
    thread_id: int,
    reason_code: Annotated[str, Form()],
    idempotency_key: Annotated[str, Form()],
) -> Response:
    def work(session: Session, agent: Agent) -> object:
        payload = parse(WithdrawPayload, reason_code=reason_code)
        sent = negotiation.send(
            session,
            agent,
            message_type=MessageType.WITHDRAW,
            payload=payload,
            idempotency_key=idempotency_key,
            thread_id=thread_id,
        )
        return _to_thread(sent.thread_id)

    return _buyer_page(request, "thread.html", work, extra=_thread_forms())


@router.get("/owner")
def owner(request: Request) -> Response:
    def work(session: Session, agent: Agent) -> object:
        return accounts.owner_dashboard(session, agent)

    return _page(request, "owner.html", work, role=None)


@router.post("/owner/revoke")
def revoke(
    request: Request,
    mandate_id: Annotated[int, Form()],
    idempotency_key: Annotated[str, Form()],
) -> Response:
    def work(session: Session, agent: Agent) -> object:
        mandates.revoke(session, agent, mandate_id, idempotency_key)
        return accounts.owner_dashboard(session, agent)

    return _page(request, "owner.html", work, role=None)


@router.post("/owner/agent")
def pause_agent(
    request: Request,
    agent_id: Annotated[int, Form()],
    active: Annotated[str, Form()],
    idempotency_key: Annotated[str, Form()],
) -> Response:
    def work(session: Session, agent: Agent) -> object:
        accounts.set_agent_active(
            session,
            agent,
            agent_id,
            active=active == "true",
            idempotency_key=idempotency_key,
        )
        return accounts.owner_dashboard(session, agent)

    return _page(request, "owner.html", work, role=None)


def _buyer_page(
    request: Request,
    template: str,
    work,
    *,
    extra: dict | None = None,
) -> Response:
    return _page(request, template, work, role=Role.BUYER, extra=extra)


def _page(
    request: Request,
    template: str,
    work,
    *,
    role: Role | None,
    extra: dict | None = None,
) -> Response:
    key = request.cookies.get(COOKIE)
    if not key:
        return RedirectResponse(f"/login?next={urlquote(request.url.path)}", status_code=303)

    def run(session: Session) -> tuple[Agent, object]:
        agent = authenticate(session, key)
        try:
            if role is not None:
                require_role(agent, role)
            return agent, work(session, agent)
        except DomainError as exc:
            exc.detail = {**exc.detail, "viewer_name": agent.name}
            raise
        except NotImplementedError as exc:
            wrapped = DomainError(ErrorCode.NOT_IMPLEMENTED, str(exc) or "not implemented")
            wrapped.detail = {"viewer_name": agent.name}
            raise wrapped from exc

    outcome = attempt(run)
    context = {
        "agent_name": None,
        "error": None,
        "error_code": None,
        "result": None,
        "question_key": str(uuid.uuid4()),
        "quote_key": str(uuid.uuid4()),
        "idempotency_key": str(uuid.uuid4()),
    }
    if extra:
        context.update(extra)
    if outcome.error is not None:
        if outcome.error.code is ErrorCode.UNAUTHORIZED:
            response = RedirectResponse("/login", status_code=303)
            response.delete_cookie(COOKIE, path="/")
            return response
        viewer = outcome.error.detail.get("viewer_name")
        if isinstance(viewer, str):
            context["agent_name"] = viewer
        listing_id = request.path_params.get("listing_id")
        thread_id = request.path_params.get("thread_id")
        if listing_id is not None and outcome.error.code is not ErrorCode.NOT_FOUND:
            loaded = attempt(lambda session: catalog.get_listing(session, int(listing_id)))
            if loaded.error is None:
                context["result"] = loaded.value
        if thread_id is not None and outcome.error.code is not ErrorCode.NOT_FOUND:
            loaded = attempt(
                lambda session: negotiation.get_thread(
                    session, authenticate(session, key), int(thread_id)
                )
            )
            if loaded.error is None:
                context["result"] = loaded.value
        context["error"] = outcome.error.message
        context["error_code"] = outcome.error.code.value
        status = 404 if outcome.error.code is ErrorCode.NOT_FOUND else 400
        return TEMPLATES.TemplateResponse(request, template, context, status_code=status)
    agent, result = outcome.value  # type: ignore[misc]
    if isinstance(result, Response):
        return result
    context["agent_name"] = agent.name
    context["result"] = result
    return TEMPLATES.TemplateResponse(request, template, context)


def _submitted(request: Request) -> bool:
    return any(request.query_params.get(name) for name in _FILTERS)


def _filter_values(request: Request) -> dict[str, str]:
    return {name: request.query_params.get(name, "") for name in _FILTERS}


def _spec_from_query(request: Request) -> SearchSpec:
    params = request.query_params

    def text(name: str) -> str | None:
        raw = params.get(name, "").strip()
        return raw or None

    def number(name: str) -> Decimal | None:
        raw = params.get(name, "").strip()
        if not raw:
            return None
        try:
            return Decimal(raw)
        except InvalidOperation as exc:
            raise DomainError(ErrorCode.VALIDATION, f"{name} is not a number") from exc

    def whole(name: str) -> int | None:
        raw = params.get(name, "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError as exc:
            raise DomainError(ErrorCode.VALIDATION, f"{name} is not a whole number") from exc

    def day(name: str) -> date | None:
        raw = params.get(name, "").strip()
        if not raw:
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise DomainError(ErrorCode.VALIDATION, f"{name} must be YYYY-MM-DD") from exc

    try:
        return SearchSpec(
            diameter=text("diameter"),
            length_mm=number("length_mm"),
            length_tolerance_mm=number("length_tolerance_mm") or Decimal("0"),
            material=text("material"),
            grade=text("grade"),
            standard=text("standard"),
            ship_to=text("ship_to"),
            need_by=day("need_by"),
            qty=whole("qty"),
        )
    except ValidationError as exc:
        raise DomainError(ErrorCode.VALIDATION, "search filters are invalid") from exc


def _to_thread(thread_id: int) -> Response:
    # Redirect after a write, so a reload never resubmits the form.
    return RedirectResponse(f"/threads/{thread_id}", status_code=303)


def _thread_forms() -> dict[str, object]:
    return {
        "counter_key": str(uuid.uuid4()),
        "accept_key": str(uuid.uuid4()),
        "withdraw_key": str(uuid.uuid4()),
        "withdraw_reasons": [reason.value for reason in WithdrawReason],
    }


def _safe_next(value: str) -> str:
    if value.startswith("/") and not value.startswith("//"):
        return value
    return "/"
