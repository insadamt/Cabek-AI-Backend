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
from api.service import DossierService


# ============================================================
# APPLICATION SETUP
# ============================================================

def create_app(settings: ApiSettings | None = None) -> FastAPI:
    settings = settings or ApiSettings.from_environment()
    application = FastAPI(
        title="CABEK Dossier API",
        version="0.1.0",
        description="Read existing dossier folders and saved expertise. Scan execution is deferred.",
    )

    # Keep storage configuration on this app instance so tests can use isolated folders.

    application.state.dossier_service = DossierService(DossierRepository(settings.dossiers_dir))
    application.add_exception_handler(DossierError, handle_dossier_error)
    application.add_exception_handler(RequestValidationError, handle_validation_error)
    application.add_exception_handler(HTTPException, handle_http_error)

    # Frontend origins must be configured explicitly; local vehicle files are not public assets.

    if settings.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["GET"],
            allow_headers=["Accept", "Content-Type"],
            expose_headers=["Content-Disposition"],
        )
    application.include_router(router)
    return application


# ============================================================
# UVICORN ENTRY POINT
# ============================================================

app = create_app()
