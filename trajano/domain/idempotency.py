import hashlib
import json
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from trajano.domain.errors import DomainError
from trajano.models import IdempotencyKey
from trajano.schemas import ErrorCode, canonical_json


def request_fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def begin(session: Session, agent_id: int, key: str, fingerprint: str) -> dict[str, Any] | None:
    """Return the stored response when this key already finished.

    Otherwise insert an in-progress row and return None. The caller completes
    it in the same transaction. A reused key with a different fingerprint is
    CONFLICT and is not retryable. An in-progress key is CONFLICT and retryable.
    Call this before other writes in the transaction.
    """
    if not key or len(key) > 80:
        raise DomainError(ErrorCode.VALIDATION, "idempotency key must be 1 to 80 characters")
    existing = _lookup(session, agent_id, key)
    if existing is not None:
        return _replay(existing, fingerprint)
    row = IdempotencyKey(agent_id=agent_id, key=key, fingerprint=fingerprint, response=None)
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        existing = _lookup(session, agent_id, key)
        if existing is None:
            raise
        return _replay(existing, fingerprint)
    return None


def complete(session: Session, agent_id: int, key: str, response: dict[str, Any]) -> None:
    row = _lookup(session, agent_id, key)
    if row is None:
        raise DomainError(ErrorCode.INTERNAL, "idempotency row is missing", retryable=True)
    # JSON columns cannot store Decimal. Callers pass model_dump(mode="json")
    # or another structure canonical_json accepts.
    row.response = json.loads(canonical_json(response))
    session.flush()


def _lookup(session: Session, agent_id: int, key: str) -> IdempotencyKey | None:
    return session.exec(
        select(IdempotencyKey).where(
            IdempotencyKey.agent_id == agent_id,
            IdempotencyKey.key == key,
        )
    ).first()


def _replay(existing: IdempotencyKey, fingerprint: str) -> dict[str, Any]:
    if existing.fingerprint != fingerprint:
        raise DomainError(
            ErrorCode.CONFLICT,
            "idempotency key was already used with a different payload",
            action="idempotency",
            agent_id=existing.agent_id,
        )
    if existing.response is None:
        raise DomainError(
            ErrorCode.CONFLICT,
            "a request with this idempotency key is in progress",
            retryable=True,
            action="idempotency",
            agent_id=existing.agent_id,
        )
    return existing.response
