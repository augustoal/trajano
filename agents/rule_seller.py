from typing import Any, Literal, TypedDict

# Keep these aligned with trajano.schemas.DeclineReason. This module must not
# import the server, except trajano.domain.pricing once the math exists.
DECLINE_REASONS = (
    "OUT_OF_STOCK",
    "CANT_MEET_DATE",
    "BELOW_FLOOR",
    "SPEC_MISMATCH",
    "OTHER",
)


class OfferAction(TypedDict):
    action: Literal["offer"]
    unit_price: str
    qty: int
    total: str
    delivery_date: str
    lines: list[dict[str, str]]


class DeclineAction(TypedDict):
    action: Literal["decline"]
    reason_code: str


class ConfirmAction(TypedDict):
    action: Literal["confirm"]


class RejectAction(TypedDict):
    action: Literal["reject"]
    reason_code: str


class AnswerAction(TypedDict):
    action: Literal["answer"]
    text: str


Action = OfferAction | DeclineAction | ConfirmAction | RejectAction | AnswerAction


def decide(thread: dict[str, Any], rules: dict[str, Any]) -> Action:
    """Pure seller decision from a thread view and the seller's YAML rules.

    No network and no database. Jorge owns the body.
    """
    raise NotImplementedError("Jorge: rule_seller.decide")
