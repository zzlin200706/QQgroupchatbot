"""FastAPI application entry point."""

from typing import Literal

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from app.config import Settings, get_settings


class HealthResponse(BaseModel):
    status: Literal["ok"]
    app: str
    environment: str


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the HTTP application without starting OneBot services."""

    app_settings = settings or get_settings()
    application = FastAPI(title="qqgroupchatbot", version="0.1.0")

    @application.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            app="qqgroupchatbot",
            environment=app_settings.app_env,
        )

    return application


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main()
