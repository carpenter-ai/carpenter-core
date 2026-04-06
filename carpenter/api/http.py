"""Starlette HTTP server for Carpenter."""
import logging
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..coordinator import Coordinator
from .webhooks import routes as webhooks_routes
from .chat import routes as chat_routes
from .review import routes as review_routes
from .credentials import routes as credentials_routes
from .analytics import routes as analytics_routes
from .auth import TokenAuthMiddleware
from .ui import routes as ui_routes

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: Starlette):
    """Thin wrapper: delegate lifecycle to Coordinator."""
    coordinator = Coordinator()
    await coordinator.start(app=app)
    app.state.coordinator = coordinator

    yield

    await coordinator.stop()


def create_app() -> Starlette:
    """Create and configure the Starlette application."""
    all_routes = (
        webhooks_routes
        + chat_routes
        + review_routes
        + credentials_routes
        + analytics_routes
        + ui_routes
    )

    app = Starlette(
        routes=all_routes,
        lifespan=lifespan,
    )

    # Auth middleware
    app.add_middleware(TokenAuthMiddleware)

    # Return JSON for HTTPException (Starlette defaults to plain text)
    app.add_exception_handler(HTTPException, http_exception_handler)

    return app


async def http_exception_handler(request: Request, exc: HTTPException):
    """Return JSON body for HTTPException (Starlette defaults to plain text)."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )
