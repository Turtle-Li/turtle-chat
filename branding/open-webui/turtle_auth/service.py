"""Registration gating and Cloudflare Turnstile verification."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from fastapi import HTTPException, Request, status

from .core import (
    AUTH_SECURITY,
    AuthSecurityConfigurationError,
)


TURNSTILE_ACTION = "turtle_signup"
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TURNSTILE_TOKEN_MAX_LENGTH = 2048
_PUBLIC_CONFIG_CACHE_SECONDS = 2.0
_public_config_cache: dict[str, Any] | None = None
_public_config_cached_at = 0.0
_public_config_lock = asyncio.Lock()


def _normalized_hostname(value: str | None) -> str:
    hostname = str(value or "").strip().lower().rstrip(".")
    if hostname.startswith("[") and "]" in hostname:
        return hostname[1 : hostname.index("]")]
    return hostname.split(":", 1)[0]


def _validate_verification_payload(
    payload: Any,
    *,
    expected_hostname: str,
    expected_action: str = TURNSTILE_ACTION,
) -> None:
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="人机验证未通过，请刷新验证后重试",
        )
    hostname = _normalized_hostname(payload.get("hostname"))
    if not hostname or hostname != _normalized_hostname(expected_hostname):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="人机验证来源不匹配，请刷新页面后重试",
        )
    if str(payload.get("action") or "") != expected_action:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="人机验证用途不匹配，请刷新页面后重试",
        )


async def public_registration_enabled() -> bool:
    config = await public_auth_security_config()
    return bool(config["registration_enabled"])


def invalidate_public_auth_security_cache() -> None:
    global _public_config_cache, _public_config_cached_at
    _public_config_cache = None
    _public_config_cached_at = 0.0


async def public_auth_security_config(*, force: bool = False) -> dict[str, Any]:
    global _public_config_cache, _public_config_cached_at
    now = time.monotonic()
    if (
        not force
        and _public_config_cache is not None
        and now - _public_config_cached_at < _PUBLIC_CONFIG_CACHE_SECONDS
    ):
        return dict(_public_config_cache)
    async with _public_config_lock:
        now = time.monotonic()
        if (
            not force
            and _public_config_cache is not None
            and now - _public_config_cached_at < _PUBLIC_CONFIG_CACHE_SECONDS
        ):
            return dict(_public_config_cache)
        config = await asyncio.to_thread(AUTH_SECURITY.public)
        value = {
            **config,
            "turnstile_action": TURNSTILE_ACTION,
        }
        _public_config_cache = dict(value)
        _public_config_cached_at = time.monotonic()
        return value


async def disable_registration_after_first_admin() -> None:
    await asyncio.to_thread(AUTH_SECURITY.set_registration_enabled, False)
    invalidate_public_auth_security_cache()


async def validate_turnstile_secret(secret: str) -> None:
    """Reject an invalid Siteverify secret without storing or returning it."""

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=10.0),
            trust_env=False,
        ) as client:
            response = await client.post(
                TURNSTILE_VERIFY_URL,
                data={
                    "secret": secret,
                    "response": "turtle-settings-validation",
                },
            )
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="暂时无法连接 Cloudflare 验证配置，请稍后重试",
        ) from exc

    if response.status_code >= 500 or not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Cloudflare 验证服务暂时不可用，请稍后重试",
        )
    error_codes = payload.get("error-codes", []) if isinstance(payload, dict) else []
    if "invalid-input-secret" in error_codes or "missing-input-secret" in error_codes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Turnstile Secret Key 无效",
        )


async def enforce_signup_security(
    request: Request,
    token: str | None,
    *,
    has_users: bool,
) -> None:
    """Enforce Turtle's durable signup switch and optional Turnstile gate."""

    try:
        config = await asyncio.to_thread(AUTH_SECURITY.load)
    except AuthSecurityConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="注册安全配置不可用，请联系管理员",
        ) from exc

    if has_users and not bool(config["registration_enabled"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="当前未开放新用户注册",
        )

    turnstile = config["turnstile"]
    if not bool(turnstile["enabled"]):
        return
    value = str(token or "").strip()
    if not value or len(value) > TURNSTILE_TOKEN_MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请先完成人机验证",
        )
    try:
        secret = await asyncio.to_thread(AUTH_SECURITY.turnstile_secret, config)
    except AuthSecurityConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="人机验证配置不可用，请联系管理员",
        ) from exc
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="人机验证尚未完成配置，请联系管理员",
        )

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=10.0),
            trust_env=False,
        ) as client:
            response = await client.post(
                TURNSTILE_VERIFY_URL,
                data={"secret": secret, "response": value},
            )
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="人机验证服务暂时不可用，请稍后重试",
        ) from exc

    if response.status_code >= 500 or not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="人机验证服务暂时不可用，请稍后重试",
        )
    _validate_verification_payload(
        payload,
        expected_hostname=request.url.hostname or request.headers.get("host", ""),
    )
