"""HTTP health, metrics and controlled sync trigger endpoints."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app
from sqlalchemy import text

from app.config import ConfigurationError, Settings, validate_runtime_configuration
from app.database import create_session_factory
from app.integrations import build_kingdee_client, build_wps_client
from app.logging import configure_logging
from app.service import SyncEngine, SyncLockedError


def create_app(settings: Settings | None = None) -> FastAPI:
    current_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(current_settings.log_level)
        validate_runtime_configuration(current_settings)
        sessions = create_session_factory(current_settings.database_url)
        app.state.sessions = sessions
        app.state.engine = SyncEngine(current_settings, sessions, build_kingdee_client(current_settings), build_wps_client(current_settings))
        yield

    app = FastAPI(title="Kingdee WPS Sync Service", version="0.1.0", lifespan=lifespan)
    app.mount("/metrics", make_asgi_app())

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready(request: Request) -> dict[str, str]:
        try:
            with request.app.state.sessions() as session:
                session.execute(text("SELECT 1"))
            return {"status": "ready"}
        except Exception as exc:
            return JSONResponse(status_code=503, content={"status": "not_ready", "error": str(exc)})

    @app.post("/sync")
    def sync(request: Request, dry_run: bool = False) -> dict[str, object]:
        try:
            return request.app.state.engine.run(dry_run=dry_run).to_dict()
        except SyncLockedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/dlq/replay")
    def replay_dlq(request: Request) -> dict[str, int]:
        try:
            return request.app.state.engine.replay_dlq()
        except ConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    return app


app = create_app()
