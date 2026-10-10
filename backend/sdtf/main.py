from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from . import observability as obs
from .api.routes_core import router as core_router
from .api.routes_transform import router as transform_router
from .config import settings
from .db import get_engine, init_schema, session_scope
from .security.auth import seed_dev_users


def create_app() -> FastAPI:
    app = FastAPI(title="SAP Selective Data Transformation Factory", version=__version__, description="Metadata-driven SAP carve-out / SDT / merger / Bluefield transformation platform. This build runs simulated migrations on synthetic data only.")
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    app.include_router(core_router, prefix="/api/v1")
    app.include_router(transform_router, prefix="/api/v1")

    @app.exception_handler(ValueError)
    async def _value_error(_: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(PermissionError)
    async def _perm_error(_: Request, exc: PermissionError):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.on_event("startup")
    def _startup():
        obs.setup(service_name=__import__("os").getenv("OTEL_SERVICE_NAME", "sdtf-api"))
        init_schema()
        obs.instrument_engine(get_engine())
        with session_scope() as s:
            seed_dev_users(s)

    obs.setup()
    obs.instrument_app(app)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": __version__, "environment": settings.environment, "telemetry": obs.status()}

    return app


app = create_app()
