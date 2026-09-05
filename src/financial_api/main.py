"""Single-tenant HTTP surface. Shopped owns persistence and pagination checkpoints."""

import secrets
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from .config import Item, Settings
from .plaid import Plaid, UpstreamError


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str = Field(min_length=1, max_length=200)
    cursor: str | None = Field(default=None, max_length=16384)
    count: int = Field(default=500, ge=1, le=500, strict=True)


def create_app(settings: Settings | None = None, transport: httpx.BaseTransport | None = None):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            app.state.settings = settings or Settings.from_env()
        except ValueError:
            # No chained validation exceptions: those can contain connection secrets.
            raise RuntimeError(
                "Invalid financial-api configuration; check required variables in README.md"
            ) from None
        with httpx.Client(
            timeout=httpx.Timeout(30.0, connect=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            app.state.plaid = Plaid(app.state.settings, client)
            yield

    app = FastAPI(
        title="Financial API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    bearer = HTTPBearer(auto_error=False)

    def authorize(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        if credentials is None or not secrets.compare_digest(
            credentials.credentials.encode(), request.app.state.settings.api_token.encode()
        ):
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})

    auth = [Depends(authorize)]

    def get_item(request: Request, item_id: str) -> Item:
        for item in request.app.state.settings.items:
            if item.item_id == item_id:
                return item
        raise HTTPException(404, "Unknown item_id")

    @app.middleware("http")
    async def private_responses(request: Request, call_next):
        # Bound body size before FastAPI materializes JSON (sync cursors are <=16 KiB).
        if request.method == "POST":
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 32768:
                    return JSONResponse(
                        {"error": {"code": "REQUEST_TOO_LARGE"}},
                        status_code=413,
                        headers={"Cache-Control": "no-store"},
                    )
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # FastAPI's default includes caller input. Never echo cursors or accidental secrets.
        return JSONResponse(
            {"error": {"code": "INVALID_REQUEST", "message": "Check the API request schema"}},
            status_code=422,
        )

    @app.exception_handler(UpstreamError)
    async def upstream_error(request: Request, exc: UpstreamError):
        return JSONResponse(
            {"error": {"code": exc.code, "restart_from_committed_cursor": exc.status == 409}},
            status_code=exc.status,
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        return JSONResponse(
            {"error": {"code": "INTERNAL_ERROR"}},
            status_code=500,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/health")
    def health():
        # Liveness only. No account counts, paths, or remote calls.
        return {"status": "ok"}

    @app.get("/openapi.json", dependencies=auth, include_in_schema=False)
    def openapi():
        return app.openapi()

    @app.get("/items", dependencies=auth)
    def items(request: Request):
        return {
            "items": [
                {"item_id": i.item_id, "institution_name": i.institution_name}
                for i in request.app.state.settings.items
            ]
        }

    @app.get("/accounts", dependencies=auth)
    def accounts(request: Request, item_id: Annotated[str, Query(min_length=1, max_length=200)]):
        return request.app.state.plaid.accounts(get_item(request, item_id))

    @app.get("/transactions", dependencies=auth)
    def transactions(
        request: Request,
        item_id: Annotated[str, Query(min_length=1, max_length=200)],
        start_date: date,
        end_date: date,
        count: Annotated[int, Query(ge=1, le=500)] = 500,
        offset: Annotated[int, Query(ge=0, le=2147483647)] = 0,
    ):
        if start_date > end_date:
            raise HTTPException(422, "start_date must be on or before end_date")
        return request.app.state.plaid.transactions(
            get_item(request, item_id), start_date.isoformat(), end_date.isoformat(), count, offset
        )

    @app.post("/transactions/sync", dependencies=auth)
    def sync(request: Request, body: SyncRequest):
        return request.app.state.plaid.sync(
            get_item(request, body.item_id), body.cursor, body.count
        )

    return app


app = create_app()
