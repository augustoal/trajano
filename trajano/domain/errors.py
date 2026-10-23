from trajano.schemas import ErrorCode, ToolError


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

    def to_tool_error(self) -> ToolError:
        return ToolError(code=self.code, message=self.message, retryable=self.retryable)
