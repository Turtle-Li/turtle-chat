from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from .auth import AuthError, load_auth_session, save_auth_session
from .client import ClaudeClient, collect_stream_text
from .config import Settings
from .models import CLAUDE_ROUTES, ROUTE_BY_KEY, save_verified_routes


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify exact Claude Web routes before publishing them to Turtle's Chat"
    )
    parser.add_argument("--auth-path", default=".runtime/claude-auth/session.json")
    parser.add_argument("--verified-models-path")
    parser.add_argument("--route", action="append", choices=tuple(ROUTE_BY_KEY))
    parser.add_argument("--list", action="store_true")
    return parser


def _settings(auth_path: Path, verified_path: Path) -> Settings:
    return Settings(
        worker_api_key="route-verification-local-only",
        auth_path=auth_path,
        verified_models_path=verified_path,
        proxy_url=os.getenv("CLAUDE_PROXY_URL", "").strip() or None,
    )


async def _verify(auth_path: Path, verified_path: Path, requested: tuple[str, ...]) -> tuple[str, ...]:
    session = load_auth_session(auth_path)
    client = ClaudeClient(_settings(auth_path, verified_path), session)
    passed: list[str] = []
    try:
        organization = await client.validate()
        if session.organization_uuid != organization:
            save_auth_session(auth_path, session.with_organization(organization))
        for key in requested:
            route = ROUTE_BY_KEY[key]
            handle = None
            try:
                handle = await client.start_completion(
                    "Reply with exactly the two uppercase letters OK and nothing else.",
                    route,
                )
                content = await collect_stream_text(handle.response)
                if content.strip():
                    passed.append(key)
                    print(f"PASS {route.version_label} / {route.level_label}")
                else:
                    print(f"FAIL {route.version_label} / {route.level_label}: empty response")
            except Exception:
                print(f"FAIL {route.version_label} / {route.level_label}: route unavailable")
            finally:
                if handle is not None:
                    try:
                        await client.delete_conversation(handle.conversation_id)
                    except Exception:
                        pass
    finally:
        await client.close()
    if "claude-sonnet-5:standard" not in passed:
        raise AuthError(
            "Claude Sonnet 5 / standard did not pass; refusing to publish a partial family"
        )
    return save_verified_routes(verified_path, passed)


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.list:
        for route in CLAUDE_ROUTES:
            print(f"{route.key}\t{route.version_label} / {route.level_label}")
        return
    auth_path = Path(args.auth_path).expanduser().absolute()
    verified_path = Path(
        args.verified_models_path or auth_path.parent / "verified-models.json"
    ).expanduser().absolute()
    requested = tuple(args.route or (route.key for route in CLAUDE_ROUTES))
    try:
        verified = asyncio.run(_verify(auth_path, verified_path, requested))
    except AuthError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Published {len(verified)} verified Claude route(s).")


if __name__ == "__main__":
    main()
