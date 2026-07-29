from __future__ import annotations

import uvicorn

from .config import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        "claude_web_worker.app:create_app",
        host=settings.bind_host,
        port=settings.port,
        factory=True,
        access_log=False,
    )


if __name__ == "__main__":
    main()
