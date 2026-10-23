class TrajanoClient:
    """MCP client plus inbox polling. Jorge implements this."""

    def __init__(self, url: str, api_key: str) -> None:
        self.url = url
        self.api_key = api_key

    async def get_inbox(self, since: str | None = None) -> dict:
        raise NotImplementedError("Jorge: client.get_inbox")
