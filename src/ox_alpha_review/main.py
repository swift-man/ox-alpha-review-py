from __future__ import annotations

import uvicorn

from ox_alpha_review.config import load_settings
from ox_alpha_review.interfaces.http_app import (
    RequestBodyTooLarge,
    _read_limited_body,
    app_factory,
    create_app,
)

__all__ = [
    "RequestBodyTooLarge",
    "_read_limited_body",
    "app_factory",
    "create_app",
    "run",
]


def run() -> None:
    settings = load_settings()
    uvicorn.run(
        "ox_alpha_review.main:app_factory",
        factory=True,
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    run()
