from typing import Any


async def run(url: str, api_key: str, role: str, task: dict[str, Any]) -> dict[str, Any]:
    """LLM buyer or seller over MCP. Jorge owns the body. Not used in CI."""
    raise NotImplementedError("Jorge: llm_agent.run")
