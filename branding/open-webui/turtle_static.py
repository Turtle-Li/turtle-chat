"""Scoped browser caching for the public shell and Turtle/Open WebUI assets."""

from __future__ import annotations

from urllib.parse import parse_qs

from starlette.datastructures import MutableHeaders


class TurtleStaticCacheMiddleware:
    """Cache only the static shell/build assets; never cache APIs or app HTML."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "")
        method = str(scope.get("method") or "GET").upper()
        query = parse_qs(
            bytes(scope.get("query_string") or b"").decode("latin-1"),
            keep_blank_values=True,
        )

        async def send_with_cache(message):
            if message.get("type") == "http.response.start":
                cache_control = None
                status_code = int(message.get("status") or 200)
                if (
                    200 <= status_code < 400
                    and method in {"GET", "HEAD"}
                    and path == "/"
                ):
                    # The root document contains no account data. A short
                    # browser/shared-cache lifetime removes a full public
                    # round trip on routine revisits while versioned assets
                    # continue to provide deterministic invalidation.
                    cache_control = (
                        "public, max-age=60, stale-while-revalidate=300"
                    )
                elif path.endswith(".html"):
                    cache_control = "no-store"
                elif (
                    200 <= status_code < 400
                    and path.startswith("/_app/immutable/")
                ):
                    cache_control = "public, max-age=31536000, immutable"
                elif (
                    200 <= status_code < 400
                    and path.startswith("/static/")
                    and query.get("v")
                ):
                    cache_control = "public, max-age=31536000, immutable"
                elif 200 <= status_code < 400 and (
                    path.startswith("/static/") or path == "/manifest.json"
                ):
                    cache_control = (
                        "public, max-age=604800, stale-while-revalidate=86400"
                    )
                if cache_control:
                    MutableHeaders(scope=message)["Cache-Control"] = cache_control
            await send(message)

        await self.app(scope, receive, send_with_cache)
