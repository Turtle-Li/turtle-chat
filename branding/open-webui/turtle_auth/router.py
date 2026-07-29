"""Public and administrator APIs for signup security settings."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, status
from open_webui.models.config import Config
from open_webui.utils.auth import get_admin_user
from pydantic import BaseModel, Field, SecretStr

from .core import (
    AUTH_SECURITY,
    AuthSecurityConfigurationError,
    validate_turnstile_secret_key,
)
from .service import (
    invalidate_public_auth_security_cache,
    public_auth_security_config,
    validate_turnstile_secret,
)


router = APIRouter()


class AuthSecurityAdminForm(BaseModel):
    registration_enabled: bool
    maintenance_enabled: bool = False
    maintenance_message: str = Field(
        default="系统正在维护，请稍后再试。",
        min_length=1,
        max_length=800,
    )
    turnstile_enabled: bool
    turnstile_site_key: str = Field(default="", max_length=256)
    turnstile_secret_key: SecretStr | None = None


@router.get("/config")
async def get_public_auth_security_config():
    return await public_auth_security_config()


@router.get("/admin/config")
async def get_admin_auth_security_config(user=Depends(get_admin_user)):
    return await asyncio.to_thread(AUTH_SECURITY.public, admin=True)


@router.put("/admin/config")
async def update_admin_auth_security_config(
    form_data: AuthSecurityAdminForm,
    user=Depends(get_admin_user),
):
    secret = (
        form_data.turnstile_secret_key.get_secret_value().strip()
        if form_data.turnstile_secret_key is not None
        else ""
    )
    if secret:
        try:
            secret = validate_turnstile_secret_key(secret)
        except AuthSecurityConfigurationError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        await validate_turnstile_secret(secret)
    try:
        result = await asyncio.to_thread(
            AUTH_SECURITY.update_admin,
            {
                "registration_enabled": form_data.registration_enabled,
                "maintenance_enabled": form_data.maintenance_enabled,
                "maintenance_message": form_data.maintenance_message,
                "turnstile_enabled": form_data.turnstile_enabled,
                "turnstile_site_key": form_data.turnstile_site_key,
                "turnstile_secret_key": secret,
            },
        )
        # Keep the low-frequency native Open WebUI settings page visually in
        # sync for this process; Turtle's PostgreSQL value remains authoritative.
        await Config.upsert({"ui.enable_signup": form_data.registration_enabled})
        invalidate_public_auth_security_cache()
        return result
    except AuthSecurityConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
