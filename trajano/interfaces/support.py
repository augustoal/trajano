import logging
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError
from sqlmodel import Session

from trajano.db import get_engine
from trajano.domain.accounts import authenticate, record_audit, require_role
from trajano.domain.errors import DomainError
from trajano.domain.negotiation import record_refusal
from trajano.models import Agent
from trajano.schemas import ErrorCode, Role, SendResult, SendToolResult

logger = logging.getLogger(__name__)


@dataclass
class Outcome[T]:
    value: T | None = None
    error: DomainError | None = None


def current_api_key() -> str | None:
    """Bearer token on the MCP request, if this call is inside one.

    The in-memory test transport has no HTTP request. Tests replace this function.
    """
    # The low-level server stashes the Starlette request here for the duration
    # of the tool call. There is no public getter that works for both servers.
    from mcp.server.lowlevel.server import request_ctx

    try:
        context = request_ctx.get()
    except LookupError:
        return None
    request = context.request
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    header = headers.get("authorization", "")
    prefix = "bearer "
    if not header.lower().startswith(prefix):
        return None
    token = header[len(prefix) :].strip()
    return token or None


def parse[T: BaseModel](model: type[T], **data: object) -> T:
    try:
        return model(**data)  # type: ignore[arg-type]
    except ValidationError as exc:
        err = exc.errors()[0]
        loc = ".".join(str(part) for part in err["loc"])
        message = f"{loc}: {err['msg']}" if loc else str(err["msg"])
        raise DomainError(ErrorCode.VALIDATION, message) from exc


def attempt[T](work: Callable[[Session], T]) -> Outcome[T]:
    session = Session(get_engine(), expire_on_commit=False)
    try:
        try:
            result = work(session)
        except DomainError as exc:
            session.rollback()
            outcome: Outcome[T] = Outcome(error=exc)
        except NotImplementedError as exc:
            session.rollback()
            outcome = Outcome(
                error=DomainError(ErrorCode.NOT_IMPLEMENTED, str(exc) or "not implemented")
            )
        except Exception:
            session.rollback()
            logger.exception("domain call failed")
            outcome = Outcome(
                error=DomainError(ErrorCode.INTERNAL, "internal error", retryable=True)
            )
        else:
            session.commit()
            outcome = Outcome(value=result)
    finally:
        session.close()
    error = outcome.error
    if error is not None and error.action is not None:
        record_audit(error.agent_id, error.action, error.code.value, error.detail)
    if error is not None and error.refusal is not None:
        record_refusal(error.refusal, error.code, error.message)
    return outcome


def call_as[T: BaseModel](
    result_type: type[T],
    api_key: str | None,
    role: Role,
    work: Callable[[Session, Agent], T],
) -> T:
    def run(session: Session) -> T:
        agent = authenticate(session, api_key)
        require_role(agent, role)
        return work(session, agent)

    outcome = attempt(run)
    if outcome.error is not None:
        return result_type(error=outcome.error.to_tool_error())
    assert outcome.value is not None
    return outcome.value


def buyer_call[T: BaseModel](result_type: type[T], work: Callable[[Session, Agent], T]) -> T:
    return call_as(result_type, current_api_key(), Role.BUYER, work)


def send_result(result: SendResult) -> SendToolResult:
    return SendToolResult(
        thread_id=result.thread_id,
        message_id=result.message_id,
        status=result.status,
        next_actions=result.next_actions,
        order_id=result.order_id,
    )


def seller_call[T: BaseModel](result_type: type[T], work: Callable[[Session, Agent], T]) -> T:
    return call_as(result_type, current_api_key(), Role.SELLER, work)
