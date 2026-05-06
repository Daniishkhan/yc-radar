from fastapi import FastAPI

from yc_radar.api.routes import router
from yc_radar.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        summary="YC company intelligence and prototype-playbook workbench.",
    )
    app.include_router(router)
    return app


app = create_app()

