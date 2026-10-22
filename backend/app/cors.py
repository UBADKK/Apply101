"""CORS for the browser web client, from APPLY101_CORS_ORIGINS only.

No configured origin means no middleware at all, so the API behaves exactly
as before. The SPA authenticates with a bearer token, never cookies, so
credentials stay off. Retry-After is exposed so the browser can read it on
429 responses.
"""
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from .config import cors_origins


CORS_ALLOW_METHODS = ["GET", "POST", "PATCH", "DELETE"]
CORS_ALLOW_HEADERS = ["Authorization", "Content-Type"]
CORS_EXPOSE_HEADERS = ["Retry-After"]


def configure_cors(app: FastAPI) -> None:
    origins = cors_origins()
    if not origins:
        return

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=CORS_ALLOW_METHODS,
        allow_headers=CORS_ALLOW_HEADERS,
        expose_headers=CORS_EXPOSE_HEADERS,
    )
