import logging
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from trajano import __version__
from trajano.config import get_settings
from trajano.db import init_db, warn_if_dev_secrets
from trajano.interfaces.mcp_buyer import build_buyer_mcp
from trajano.interfaces.mcp_seller import build_seller_mcp
from trajano.interfaces.web import install

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    warn_if_dev_secrets(settings)
    buyer = build_buyer_mcp()
    seller = build_seller_mcp()
    # Build the ASGI apps before the lifespan so each session manager exists.
    buyer_http = buyer.streamable_http_app()
    seller_http = seller.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        init_db()
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(buyer.session_manager.run())
            await stack.enter_async_context(seller.session_manager.run())
            yield

    app = FastAPI(title="Trajano", version=__version__, lifespan=lifespan)
    app.mount("/mcp/buyer", buyer_http)
    app.mount("/mcp/seller", seller_http)
    install(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


class _ASGI:
    """Uvicorn target. The app is built on first use so imports do not open a database."""

    def __init__(self) -> None:
        self._app: FastAPI | None = None

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if self._app is None:
            self._app = create_app()
        await self._app(scope, receive, send)


app = _ASGI()
