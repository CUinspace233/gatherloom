from collections.abc import Awaitable, Callable
from os import environ
from typing import Literal

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import storage
from app.attachments import UPLOAD_PATH
from app.attachments import router as attachments_router
from app.auth import router as auth_router
from app.conversations import router as conversations_router
from app.entries import router as entries_router
from app.projects import router as projects_router
from app.search_api import router as search_router
from app.summaries import router as summaries_router

app = FastAPI(title="Gatherloom API")
app.include_router(auth_router)
app.include_router(projects_router)
app.include_router(entries_router)
app.include_router(attachments_router)
app.include_router(search_router)
app.include_router(conversations_router)
app.include_router(summaries_router)
# Multipart framing and form fields add a little to the file itself.
MAX_UPLOAD_REQUEST = storage.MAX_ATTACHMENT_BYTES + 64 * 1024
allowed_clients = frozenset(
    address.strip()
    for address in environ.get("GATHERLOOM_DEV_CLIENTS", "").split(",")
    if address.strip()
)


@app.middleware("http")
async def apply_response_policy(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response: Response
    if allowed_clients and (
        request.client is None or request.client.host not in allowed_clients
    ):
        response = JSONResponse(
            status_code=403, content={"detail": "client is not allowed"}
        )
    elif (
        request.method == "POST"
        and request.url.path == UPLOAD_PATH
        and (error := upload_size_error(request))
    ):
        response = error
    else:
        response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


def upload_size_error(request: Request) -> JSONResponse | None:
    # Reject before the body is read; the server enforces the declared length.
    length = request.headers.get("content-length")
    if length is None or not length.isdigit():
        return JSONResponse(
            status_code=411, content={"detail": "content length required"}
        )
    if int(length) > MAX_UPLOAD_REQUEST:
        return JSONResponse(
            status_code=413, content={"detail": "attachment exceeds 10 MiB"}
        )
    return None


@app.exception_handler(RequestValidationError)
async def validation_error(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    detail = [
        {key: error[key] for key in ("loc", "msg", "type")} for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": detail})


class HealthStatus(BaseModel):
    status: Literal["ok"]


@app.get("/healthz")
async def healthz() -> HealthStatus:
    return HealthStatus(status="ok")
