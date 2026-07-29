from __future__ import annotations

import uvicorn

from .app import create_app
from .config import PumpSettings


def main() -> None:
    settings = PumpSettings.from_env()
    uvicorn.run(create_app(settings), host=settings.bind_host, port=settings.port, access_log=False)


if __name__ == "__main__":
    main()
