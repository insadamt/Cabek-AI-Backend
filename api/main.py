"""Configure the local FastAPI integration and shared dossier/scan services.

Depends on configured file storage and optional model workers; write endpoints
share scan locks inside the trusted local boundary; lighting commands are sent to the separate ESP32 API.
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
from api.capture_service import CaptureService
from api.capture_routes import router as capture_router
from api.lighting_client import LightingClient
from api.settings_routes import router as settings_router, internal_router
from api.operation_settings import OperationSettingsStore
from api.tunnel_lifecycle import TunnelLifecycle
from api.diagnostic_logs import DiagnosticLogStore
from api.diagnostic_middleware import DiagnosticMiddleware
from api.diagnostics import DiagnosticsService
from api.diagnostic_routes import router as diagnostic_router
from api.camera_routes import router as camera_router
from api.econstat_service import EconstatService
from api.econstat_routes import router as econstat_router


# ============================================================
# APPLICATION SETUP
# ============================================================

def create_app(settings: ApiSettings | None = None, scan_backend: ScanBackend | None = None,
               lighting: LightingClient | None = None) -> FastAPI:
    settings = settings or ApiSettings.from_environment()
    lighting = lighting or LightingClient(settings.lighting)
    manager = ScanManager(settings, scan_backend, lighting=lighting)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.diagnostic_logs.start()
        try:
            await asyncio.to_thread(application.state.tunnel_lifecycle.recover)
            lighting.start()
            await asyncio.to_thread(manager.start)
            await asyncio.to_thread(application.state.capture_service.start)
            lighting.application_ready()
            yield
        finally:
            try:
                await asyncio.to_thread(application.state.econstat_service.close)
                await asyncio.to_thread(application.state.capture_service.close)
                await asyncio.to_thread(manager.close)
            finally:
                await asyncio.to_thread(application.state.tunnel_lifecycle.close)
                await asyncio.to_thread(lighting.close)
                application.state.diagnostic_logs.close()

    application = FastAPI(
        title="CABEK Dossier API",
        version="0.2.2",
        description="Read dossiers, queue real scans, and retrieve saved expertise with file-based progress.",
        lifespan=lifespan,
    )

    # Keep storage configuration on this app instance so tests can use isolated folders.

    application.state.dossier_service = DossierService(DossierRepository(settings.dossiers_dir))
    application.state.econstat_service = EconstatService(application.state.dossier_service.repository, settings.econstat)
    application.state.scan_manager = manager
    application.state.lighting = lighting
    application.state.vehicle_catalog = VehicleCatalog()
    application.state.workflow_service = WorkflowService(manager, application.state.vehicle_catalog)
    application.state.capture_service = CaptureService(application.state.workflow_service, settings, lighting)
    application.state.internal_lighting = application.state.capture_service.internal_lighting
    application.state.operation_settings = OperationSettingsStore(settings.runtime_root, settings.tunnel_mode == 'mock')
    lifecycle = TunnelLifecycle(manager.repository, settings.runtime_root, lighting, application.state.internal_lighting)
    application.state.tunnel_lifecycle = lifecycle
    manager.tunnel_lifecycle = lifecycle
    application.state.capture_service.lifecycle = lifecycle
    application.state.capture_service.operation_settings = application.state.operation_settings
    log_store = DiagnosticLogStore(settings.runtime_root, manager.repository)
    application.state.diagnostic_logs = log_store
    application.state.diagnostics = DiagnosticsService(settings, manager, application.state.capture_service,
                                                      application.state.vehicle_catalog, lifecycle, log_store)
    application.add_middleware(UploadLimitMiddleware)
    application.add_middleware(DiagnosticMiddleware)
    application.add_exception_handler(DossierError, handle_dossier_error)
    application.add_exception_handler(RequestValidationError, handle_validation_error)
    application.add_exception_handler(HTTPException, handle_http_error)

    # Frontend origins must be configured explicitly; local vehicle files are not public assets.

    if settings.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Accept", "Content-Type", "Idempotency-Key", "Authorization"],
            expose_headers=["Content-Disposition", "X-Request-ID"],
        )
    application.include_router(router)
    application.include_router(scan_router)
    application.include_router(vehicle_router)
    application.include_router(workflow_router)
    application.include_router(capture_router)
    application.include_router(settings_router)
    application.include_router(internal_router)
    application.include_router(diagnostic_router)
    application.include_router(camera_router)
    application.include_router(econstat_router)
    from api.private_demo.service import PrivateDemoService
    from api.private_demo.routes import router as private_demo_router
    application.state.private_demo = PrivateDemoService(application, settings)
    manager.private_demo = application.state.private_demo
    application.include_router(private_demo_router)
    return application


# ============================================================
# UVICORN ENTRY POINT
# ============================================================

app = create_app()
