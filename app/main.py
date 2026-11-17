"""The ASGI app: public read API, admin ingest API, the web UI with accounts — one entry, default-deny auth."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from . import accounts, admin, calendar_api, courses, documents, embeddings_api, home, ingest, search, uploads
from .auth import AuthMiddleware
from .config import Settings, conninfo, settings
from .db import close_pool, open_pool


def create_app(cfg: Settings | None = None) -> FastAPI:
    cfg = cfg or settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        open_pool(conninfo(cfg))
        # load the search model before taking requests: loading it on the first search made that search take 5–7 s
        # after every start (each deploy); gunicorn queues requests on the socket meanwhile
        from .embed import embedder
        embedder()
        yield
        close_pool()

    app = FastAPI(title="School information API", version="0.1.0", lifespan=lifespan)

    @app.get("/v1/health")
    def health():
        return {"ok": True}

    app.include_router(home.router)
    app.include_router(documents.router)
    app.include_router(courses.router)   # before search: /v1/courses/facets must win over /v1/courses/{code}
    app.include_router(search.router)
    app.include_router(calendar_api.router)
    app.include_router(accounts.router)   # before admin: /admin/login, /admin/settings … are not admin pages
    app.include_router(uploads.router)
    app.include_router(admin.router)
    app.include_router(embeddings_api.router)
    app.include_router(ingest.router)
    # order matters: the session middleware (added last = outermost) must run before the auth check
    app.add_middleware(AuthMiddleware, settings=cfg)
    app.add_middleware(SessionMiddleware, secret_key=cfg.session_secret or "unset", https_only=True, same_site="strict",
                       session_cookie="school_admin", max_age=8 * 3600)
    return app


app = create_app()
