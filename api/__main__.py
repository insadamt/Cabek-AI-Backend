"""Launch the local API and send startup lighting before importing application services.

Depends on Uvicorn and the optional lighting bridge. Lighting deployments use one
process with a stable configured port; the legacy development launcher keeps reload.
"""

import argparse

import uvicorn

from api.config import ApiSettings
from api.lighting_client import LightingClient


# ============================================================
# LOCAL API LAUNCHER
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Launch the local CABEK API.")
    parser.add_argument("--reload", action="store_true", help="Restart when Python files change.")
    parser.add_argument("--port", type=int, default=0, help="Local HTTP port (zero selects a free port).")
    arguments = parser.parse_args()
    settings = ApiSettings.from_environment()
    if not settings.lighting.enabled:
        uvicorn.run("api.main:app", host="127.0.0.1", port=arguments.port, reload=arguments.reload)
        return
    if arguments.reload:
        parser.error("Lighting requires one persistent process; omit --reload.")
    lighting = LightingClient(settings.lighting)
    lighting.start()
    try:
        from api.main import create_app
        uvicorn.run(create_app(settings, lighting=lighting), host="127.0.0.1", port=arguments.port)
    finally:
        lighting.close()


if __name__ == "__main__":
    main()
