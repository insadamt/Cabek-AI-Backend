"""Configure the local FastAPI integration and shared dossier/scan services.

Depends on configured file storage and optional model workers; write endpoints
share scan locks and remain inside the documented unauthenticated local boundary.
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException

from api.config import ApiSettings
from api.errors import (
    DossierError,
    handle_dossier_error,
    handle_http_error,
    handle_validation_error,
)
from api.repository import DossierRepository
from api.routes import router
from api.scan_routes import router as scan_router
from api.service import DossierService
from scans.backend import ScanBackend
from scans.jobs import ScanManager
from api.vehicle_catalog import VehicleCatalog
from api.vehicle_routes import router as vehicle_router
from api.workflow_routes import router as workflow_router
from api.workflow_service import WorkflowService
from api.upload_limits import UploadLimitMiddleware


# ============================================================
# APPLICATION SETUP
# ============================================================

def create_app(settings: ApiSettings | None = None, scan_backend: ScanBackend | None = None) -> FastAPI:
    settings = settings or ApiSettings.from_environment()
    manager = ScanManager(settings, scan_backend)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        await asyncio.to_thread(manager.start)
        try:
            yield
        finally:
            await asyncio.to_thread(manager.close)

    application = FastAPI(
        title="CABEK Dossier API",
        version="0.1.0",
        description="Read dossiers, queue real scans, and retrieve saved expertise with file-based progress.",
        lifespan=lifespan,
    )

    # Keep storage configuration on this app instance so tests can use isolated folders.

    application.state.dossier_service = DossierService(DossierRepository(settings.dossiers_dir))
    application.state.scan_manager = manager
    application.state.vehicle_catalog = VehicleCatalog()
    application.state.workflow_service = WorkflowService(manager, application.state.vehicle_catalog)
    application.add_middleware(UploadLimitMiddleware)
    application.add_exception_handler(DossierError, handle_dossier_error)
    application.add_exception_handler(RequestValidationError, handle_validation_error)
    application.add_exception_handler(HTTPException, handle_http_error)

    # Frontend origins must be configured explicitly; local vehicle files are not public assets.

    if settings.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["GET", "POST", "PUT", "PATCH"],
            allow_headers=["Accept", "Content-Type"],
            expose_headers=["Content-Disposition"],
        )
    application.include_router(router)
    application.include_router(scan_router)
    application.include_router(vehicle_router)
    application.include_router(workflow_router)
    return application


# ============================================================
# UVICORN ENTRY POINT
# ============================================================

app = create_app()
