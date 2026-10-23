from dataclasses import dataclass

from trajano.schemas import ErrorCode, ToolError


@dataclass(frozen=True)
class Refusal:
    """Where a refused system message goes once the failed transaction is rolled back."""

    thread_id: int
    key: str


class DomainError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        action: str | None = None,
        agent_id: int | None = None,
        detail: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        # action is set when the refusal should be written to the audit log.
        # Catalog misses and incomplete searches stay quiet.
        self.action = action
        self.agent_id = agent_id
        self.detail = detail or {}
        self.refusal: Refusal | None = None

    def to_tool_error(self) -> ToolError:
        return ToolError(code=self.code, message=self.message, retryable=self.retryable)
