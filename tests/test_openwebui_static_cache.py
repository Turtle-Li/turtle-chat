from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "branding"
    / "open-webui"
    / "turtle_static.py"
)
SPEC = importlib.util.spec_from_file_location(
    "turtle_openwebui_static_cache",
    MODULE_PATH,
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TurtleStaticCacheMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def _headers(
        self,
        path: str,
        query: bytes = b"",
        *,
        status: int = 200,
    ) -> dict[str, str]:
        messages = []

        async def app(_scope, _receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b""})

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            messages.append(message)

        middleware = MODULE.TurtleStaticCacheMiddleware(app)
        await middleware(
            {
                "type": "http",
                "method": "GET",
                "path": path,
                "query_string": query,
                "headers": [],
            },
            receive,
            send,
        )
        return {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in messages[0]["headers"]
        }

    async def test_versioned_asset_is_immutable(self) -> None:
        headers = await self._headers(
            "/static/turtle-admin.js",
            b"v=20260725.5",
        )
        self.assertEqual(
            headers["cache-control"],
            "public, max-age=31536000, immutable",
        )

    async def test_html_is_never_cached(self) -> None:
        headers = await self._headers("/static/turtle-admin.html")
        self.assertEqual(headers["cache-control"], "no-store")

    async def test_api_response_is_untouched(self) -> None:
        headers = await self._headers("/api/v1/turtle/chat/policy")
        self.assertNotIn("cache-control", headers)

    async def test_missing_versioned_asset_is_not_cached_as_immutable(self) -> None:
        headers = await self._headers(
            "/static/missing.js",
            b"v=20260725.5",
            status=404,
        )
        self.assertNotIn("cache-control", headers)
