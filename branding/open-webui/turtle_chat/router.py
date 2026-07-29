"""Authenticated chat groups, model windows, usage, and administrator APIs."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from open_webui.config import ENABLE_ADMIN_CHAT_ACCESS
from open_webui.internal.db import get_async_session
from open_webui.models.access_grants import AccessGrants
from open_webui.models.chats import Chat
from open_webui.models.folders import Folders
from open_webui.models.users import Users
from open_webui.utils.access_control.folders import has_folder_access
from open_webui.utils.auth import get_admin_user, get_current_user, get_verified_user
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .announcement import (
    ANNOUNCEMENT_BODY_MAX,
    ANNOUNCEMENT_TITLE_MAX,
    render_announcement_markdown,
)
from .concurrency import CHAT_CONCURRENCY, ChatCoordinatorUnavailable
from .history import (
    get_chat_envelope,
    initial_chat_page,
    older_chat_range,
)
from .provider import provider_for_chat
from .subscription_cache import SUBSCRIPTION_CACHE
from .store import (
    CHAT_STORE,
    DEFAULT_SUBSCRIPTION_DAYS,
    MAX_CONCURRENCY,
    MAX_MODEL_LIMIT,
    MAX_STORAGE_QUOTA_BYTES,
    MAX_SUBSCRIPTION_DAYS,
    MAX_WINDOW_SECONDS,
    SELECTIONS,
    ChatAnnouncementConflict,
    ChatAnnouncementNotFound,
    ChatPolicyError,
    chat_plan_presets,
)


router = APIRouter()


async def _history_envelope_for_user(
    chat_id: str,
    user,
    db: AsyncSession,
) -> dict[str, Any]:
    """Apply Open WebUI's chat-read policy without selecting ``chat.chat``."""

    envelope = await get_chat_envelope(chat_id, db=db)
    if envelope is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="对话不存在或无权访问",
        )
    if envelope["user_id"] == user.id:
        return envelope

    meta = envelope.get("meta") if isinstance(envelope.get("meta"), dict) else {}
    if user.role == "admin" and (
        ENABLE_ADMIN_CHAT_ACCESS or meta.get("internal") is True
    ):
        return envelope

    if await AccessGrants.has_access(
        user_id=user.id,
        resource_type="shared_chat",
        resource_id=chat_id,
        permission="read",
        db=db,
    ):
        return envelope

    folder_id = envelope.get("folder_id")
    if folder_id:
        folder = await Folders.get_folder_by_id(folder_id, db=db)
        if folder and await has_folder_access(user.id, folder, "read", db):
            return envelope

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="对话不存在或无权访问",
    )


@router.get("/history/{chat_id}/initial")
async def get_initial_chat_history_page(
    chat_id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Return the lightweight chat shell and current indexed depth window."""

    envelope = await _history_envelope_for_user(chat_id, user, db)
    try:
        return await initial_chat_page(envelope, db=db)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="对话不存在",
        ) from exc


@router.get("/history/{chat_id}/range")
async def get_older_chat_history_range(
    chat_id: str,
    before_depth: int = Query(ge=0, le=2_147_483_647),
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Return every message in the preceding indexed half-open depth range."""

    envelope = await _history_envelope_for_user(chat_id, user, db)
    try:
        return await older_chat_range(
            envelope,
            before_depth=before_depth,
            db=db,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="对话不存在",
        ) from exc


class UserPolicyForm(BaseModel):
    """Legacy one-user override retained for compatibility and migration."""

    allowed: list[str] = Field(min_length=1, max_length=len(SELECTIONS))


class GroupRuleForm(BaseModel):
    selection_key: str
    enabled: bool = False
    limit_count: int | None = Field(default=None, ge=1, le=MAX_MODEL_LIMIT)
    window_seconds: int = Field(default=0, ge=0, le=MAX_WINDOW_SECONDS)
    fallback_key: str | None = None


class GroupForm(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=200)
    storage_quota_bytes: int = Field(default=2 * 1024**3, ge=0, le=MAX_STORAGE_QUOTA_BYTES)
    max_concurrency: int = Field(default=2, ge=1, le=MAX_CONCURRENCY)
    default_user_concurrency: int = Field(default=1, ge=1, le=MAX_CONCURRENCY)
    gpt_account_pool_id: str = Field(default="gpt-default", min_length=1, max_length=80)
    rules: list[GroupRuleForm] = Field(min_length=1, max_length=len(SELECTIONS))


class ResourceGroupForm(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=200)
    storage_quota_bytes: int = Field(default=2 * 1024**3, ge=0, le=MAX_STORAGE_QUOTA_BYTES)
    max_concurrency: int = Field(default=2, ge=1, le=MAX_CONCURRENCY)
    default_user_concurrency: int = Field(default=1, ge=1, le=MAX_CONCURRENCY)


class ModelGroupForm(BaseModel):
    provider_family: str = Field(pattern="^(gpt|claude)$")
    name: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=200)
    account_pool_id: str | None = Field(default=None, max_length=80)
    rules: list[GroupRuleForm] = Field(default_factory=list, max_length=len(SELECTIONS))


class UserGroupForm(BaseModel):
    group_id: str = Field(min_length=1, max_length=80)


class UserConcurrencyForm(BaseModel):
    max_concurrency: int | None = Field(default=None, ge=1, le=MAX_CONCURRENCY)


class BulkUserGroupsForm(BaseModel):
    user_ids: list[str] = Field(min_length=1, max_length=200)
    resource_group_id: str | None = Field(default=None, min_length=1, max_length=80)
    gpt_model_group_id: str | None = Field(default=None, min_length=1, max_length=80)
    claude_model_group_id: str | None = Field(default=None, min_length=1, max_length=80)


class UserSubscriptionForm(BaseModel):
    starts_at: int | None = Field(default=None, ge=0)
    expires_at: int | None = Field(default=None, ge=1)
    duration_days: int = Field(
        default=DEFAULT_SUBSCRIPTION_DAYS,
        ge=1,
        le=MAX_SUBSCRIPTION_DAYS,
    )


class SubscriptionExtendForm(BaseModel):
    days: int = Field(
        default=DEFAULT_SUBSCRIPTION_DAYS,
        ge=1,
        le=MAX_SUBSCRIPTION_DAYS,
    )


class QuotaResetForm(BaseModel):
    selection_key: str | None = None


class AnnouncementForm(BaseModel):
    title: str = Field(default="", max_length=ANNOUNCEMENT_TITLE_MAX)
    body_markdown: str = Field(default="", max_length=ANNOUNCEMENT_BODY_MAX)
    enabled: bool = False


class AnnouncementPreviewForm(BaseModel):
    body_markdown: str = Field(default="", max_length=ANNOUNCEMENT_BODY_MAX)


class AnnouncementDismissForm(BaseModel):
    revision: int = Field(ge=1)


def _model_capability(
    models: dict[str, dict[str, Any]],
    display_name: str = "GPT",
) -> dict[str, Any]:
    versions: list[dict[str, Any]] = []
    for version_id in ("latest", "gpt-5-5", "gpt-5-3", "o3"):
        entries = [item for item in SELECTIONS if item["version"] == version_id]
        levels: list[dict[str, Any]] = []
        for item in entries:
            lane = models[item["key"]]
            levels.append(
                {
                    "id": item["level"],
                    "key": item["key"],
                    "label": item["level_label"],
                    **lane,
                }
            )
        available = [item for item in levels if item["available"]]
        allowed = [item for item in levels if item["allowed"]]
        default_level = available[0]["id"] if available else allowed[0]["id"] if allowed else levels[0]["id"]
        if version_id == "latest":
            if any(item["id"] == "medium" for item in available):
                default_level = "medium"
            elif any(item["id"] == "medium" for item in allowed):
                default_level = "medium"
        versions.append(
            {
                "id": version_id,
                "label": entries[0]["version_label"],
                "default_thinking_level": default_level,
                "allowed": bool(allowed),
                "available": bool(available),
                "status": "available" if available else "exhausted" if allowed else "forbidden",
                "thinking_levels": levels,
            }
        )
    available_versions = [item for item in versions if item["available"]]
    allowed_versions = [item for item in versions if item["allowed"]]
    default_version = (
        "gpt-5-5"
        if any(item["id"] == "gpt-5-5" for item in available_versions)
        else available_versions[0]["id"]
        if available_versions
        else allowed_versions[0]["id"]
        if allowed_versions
        else versions[0]["id"]
    )
    return {
        "id": "gpt-5-web",
        "name": display_name,
        "turtle": {
            "family": "gpt",
            "family_label": display_name,
            "default_version": default_version,
            "version_field": "turtle_model_version",
            "thinking_field": "turtle_thinking_level",
            "picker": {
                "style": "chatgpt",
                "section_label": "智能",
                "mode_order": [
                    {
                        "selection_key": "gpt-5-5:instant",
                        "label": "极速",
                        "badge": "5.5",
                    },
                    {"selection_key": "latest:medium", "label": "中"},
                    {"selection_key": "latest:high", "label": "高"},
                    {"selection_key": "latest:xhigh", "label": "极高"},
                    {"selection_key": "latest:pro", "label": "Pro"},
                ],
                "model_order": ["latest", "gpt-5-5", "gpt-5-3", "o3"],
            },
            "versions": versions,
        },
    }


async def _target_or_404(user_id: str, db: AsyncSession):
    target = await Users.get_user_by_id(user_id, db=db)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    return target


async def _user_payload(
    target,
    subscription: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = await asyncio.to_thread(CHAT_STORE.policy_for_user, target.id, target.role)
    quota = await asyncio.to_thread(CHAT_STORE.quota_summary, target.id, target.role)
    concurrency = await asyncio.to_thread(
        CHAT_STORE.concurrency_for_user, target.id, target.role
    )
    if subscription is None:
        subscription = await SUBSCRIPTION_CACHE.get(target.id, target.role)
    return {
        "id": target.id,
        "name": target.name,
        "email": target.email,
        "role": target.role,
        "policy": policy,
        "quota": quota,
        "concurrency": concurrency,
        "subscription": subscription,
    }


def _bad_request(exc: ChatPolicyError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _announcement_payload(
    value: dict[str, Any],
    *,
    admin: bool,
) -> dict[str, Any]:
    enabled = bool(value.get("enabled"))
    payload: dict[str, Any] = {
        "id": value.get("id"),
        "revision": int(value.get("revision") or 0),
        "title": str(value.get("title") or ""),
        "enabled": enabled,
        "created_at": value.get("created_at"),
        "updated_at": value.get("updated_at"),
        "html": render_announcement_markdown(
            str(value.get("body_markdown") or "")
        ),
    }
    if admin:
        payload.update(
            {
                "body_markdown": str(value.get("body_markdown") or ""),
                "updated_by": value.get("updated_by"),
                "changed": value.get("changed"),
            }
        )
    else:
        payload.update(
            {
                "dismissed": bool(value.get("dismissed")),
                "should_show": bool(value.get("should_show")),
            }
        )
    return payload


def _announcement_list_payload(
    values: list[dict[str, Any]],
    *,
    admin: bool,
) -> list[dict[str, Any]]:
    return [_announcement_payload(value, admin=admin) for value in values]


async def _get_announcement_user(user=Depends(get_current_user)):
    """Allow a pending user to persist only the idempotent announcement receipt."""

    from open_webui.turtle_auth.service import public_auth_security_config

    site_access = await public_auth_security_config()
    if user.role != "admin" and site_access.get("maintenance_enabled"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(
                site_access.get("maintenance_message")
                or "系统正在维护，请稍后再试。"
            ),
        )
    if user.role not in {"pending", "user", "admin"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="当前账号不能访问公告",
        )
    return user


@router.get("/announcements")
async def get_announcements(
    response: Response,
    user=Depends(_get_announcement_user),
):
    response.headers["Cache-Control"] = "private, no-store"
    values = await asyncio.to_thread(
        CHAT_STORE.announcements_for_user,
        user.id,
        user.role,
    )
    announcements = _announcement_list_payload(values, admin=False)
    return {
        "announcements": announcements,
        "unread_count": sum(1 for item in announcements if item["should_show"]),
    }


@router.post("/announcements/{announcement_id}/dismiss")
async def dismiss_announcement_item(
    announcement_id: str,
    form_data: AnnouncementDismissForm,
    response: Response,
    user=Depends(_get_announcement_user),
):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        value = await asyncio.to_thread(
            CHAT_STORE.dismiss_announcement,
            user.id,
            announcement_id,
            form_data.revision,
        )
    except ChatAnnouncementConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return {"announcement": _announcement_payload(value, admin=False)}


@router.get("/announcement")
async def get_announcement(
    response: Response,
    user=Depends(_get_announcement_user),
):
    response.headers["Cache-Control"] = "private, no-store"
    value = await asyncio.to_thread(
        CHAT_STORE.announcement_for_user,
        user.id,
        user.role,
    )
    if not value.get("enabled"):
        return {"announcement": None}
    return {"announcement": _announcement_payload(value, admin=False)}


@router.post("/announcement/dismiss")
async def dismiss_announcement(
    form_data: AnnouncementDismissForm,
    response: Response,
    user=Depends(_get_announcement_user),
):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        value = await asyncio.to_thread(
            CHAT_STORE.dismiss_current_announcement,
            user.id,
            user.role,
            form_data.revision,
        )
    except ChatAnnouncementConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return {"announcement": _announcement_payload(value, admin=False)}


@router.get("/admin/announcements")
async def get_admin_announcements(
    response: Response,
    user=Depends(get_admin_user),
):
    response.headers["Cache-Control"] = "private, no-store"
    values = await asyncio.to_thread(CHAT_STORE.announcements_admin)
    return {"announcements": _announcement_list_payload(values, admin=True)}


@router.post("/admin/announcements", status_code=status.HTTP_201_CREATED)
async def create_admin_announcement(
    form_data: AnnouncementForm,
    response: Response,
    user=Depends(get_admin_user),
):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        value = await asyncio.to_thread(
            CHAT_STORE.create_announcement,
            title=form_data.title,
            body_markdown=form_data.body_markdown,
            enabled=form_data.enabled,
            updated_by=user.id,
        )
    except ChatPolicyError as exc:
        raise _bad_request(exc) from exc
    return {"announcement": _announcement_payload(value, admin=True)}


@router.put("/admin/announcements/{announcement_id}")
async def update_admin_announcement_item(
    announcement_id: str,
    form_data: AnnouncementForm,
    response: Response,
    user=Depends(get_admin_user),
):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        value = await asyncio.to_thread(
            CHAT_STORE.update_announcement,
            announcement_id,
            title=form_data.title,
            body_markdown=form_data.body_markdown,
            enabled=form_data.enabled,
            updated_by=user.id,
        )
    except ChatAnnouncementNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ChatPolicyError as exc:
        raise _bad_request(exc) from exc
    return {"announcement": _announcement_payload(value, admin=True)}


@router.delete("/admin/announcements/{announcement_id}")
async def delete_admin_announcement(
    announcement_id: str,
    response: Response,
    user=Depends(get_admin_user),
):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        result = await asyncio.to_thread(
            CHAT_STORE.delete_announcement,
            announcement_id,
            deleted_by=user.id,
        )
    except ChatAnnouncementNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return result


@router.get("/admin/announcement")
async def get_admin_announcement(
    response: Response,
    user=Depends(get_admin_user),
):
    response.headers["Cache-Control"] = "private, no-store"
    value = await asyncio.to_thread(CHAT_STORE.announcement_admin)
    return {"announcement": _announcement_payload(value, admin=True)}


@router.post("/admin/announcements/preview")
@router.post("/admin/announcement/preview")
async def preview_admin_announcement(
    form_data: AnnouncementPreviewForm,
    response: Response,
    user=Depends(get_admin_user),
):
    response.headers["Cache-Control"] = "private, no-store"
    return {
        "html": render_announcement_markdown(form_data.body_markdown),
    }


@router.put("/admin/announcement")
async def update_admin_announcement(
    form_data: AnnouncementForm,
    response: Response,
    user=Depends(get_admin_user),
):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        value = await asyncio.to_thread(
            CHAT_STORE.set_announcement,
            title=form_data.title,
            body_markdown=form_data.body_markdown,
            enabled=form_data.enabled,
            updated_by=user.id,
        )
    except ChatPolicyError as exc:
        raise _bad_request(exc) from exc
    return {"announcement": _announcement_payload(value, admin=True)}


@router.get("/capabilities")
async def get_capabilities(user=Depends(get_verified_user)):
    subscription = await SUBSCRIPTION_CACHE.get(user.id, user.role)
    policy = await asyncio.to_thread(CHAT_STORE.policy_for_user, user.id, user.role)
    quota = await asyncio.to_thread(CHAT_STORE.quota_summary, user.id, user.role)
    concurrency = await asyncio.to_thread(
        CHAT_STORE.concurrency_for_user, user.id, user.role
    )
    provider_display = await asyncio.to_thread(CHAT_STORE.provider_display_names)
    return {
        "model": _model_capability(
            quota["models"],
            provider_display.get("gpt", "GPT"),
        ),
        "provider_display": provider_display,
        "allowed": policy["allowed"],
        "policy": policy,
        "quota": quota,
        "concurrency": concurrency,
        "subscription": subscription,
        "is_admin": user.role == "admin",
    }


@router.get("/provider-display")
async def get_provider_display(user=Depends(get_verified_user)):
    return {
        "items": await asyncio.to_thread(CHAT_STORE.provider_display_names),
    }


@router.get("/concurrency")
async def get_concurrency_status(
    request_id: str = Query(min_length=8, max_length=64),
    user=Depends(get_verified_user),
):
    try:
        return await CHAT_CONCURRENCY.status(request_id, user.id)
    except ChatCoordinatorUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "chat_concurrency_unavailable", "message": str(exc)},
        ) from exc


@router.get("/conversation-index")
async def get_conversation_index(
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Return only chat IDs and provider families for the sidebar filter.

    Titles, prompts, answers, attachments and model credentials never enter
    this lightweight index. Provider identity lives in the small ``meta``
    column so a long chat does not make sidebar loading slower over time.
    """

    result = await db.execute(
        select(Chat.id, Chat.meta, Chat.updated_at)
        .where(Chat.user_id == user.id, Chat.archived.is_(False))
        .order_by(Chat.updated_at.desc(), Chat.id)
    )
    items = [
        {
            "id": str(chat_id),
            "provider": provider_for_chat(None, meta),
            "updated_at": int(updated_at or 0),
        }
        for chat_id, meta, updated_at in result.all()
    ]
    counts = {"gpt": 0, "claude": 0}
    for item in items:
        counts[item["provider"]] = counts.get(item["provider"], 0) + 1
    return {"items": items, "counts": counts}


@router.get("/usage")
async def get_my_usage(
    limit: int = Query(20, ge=1, le=100),
    user=Depends(get_verified_user),
):
    return {
        "quota": await asyncio.to_thread(CHAT_STORE.quota_summary, user.id, user.role),
        "subscription": await SUBSCRIPTION_CACHE.get(user.id, user.role),
        "items": await asyncio.to_thread(CHAT_STORE.recent_usage, user.id, limit),
    }


@router.get("/admin/users")
async def get_admin_users(
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    result = await Users.get_users(skip=0, limit=None, db=db)
    targets = list(result.get("users", []))
    subscriptions = await SUBSCRIPTION_CACHE.get_many(
        [(target.id, target.role) for target in targets]
    )
    items = [
        await _user_payload(
            target,
            subscriptions[(str(target.id), str(target.role))],
        )
        for target in targets
    ]
    return {
        "items": items,
        "groups": await asyncio.to_thread(CHAT_STORE.list_groups),
        "resource_groups": await asyncio.to_thread(CHAT_STORE.list_groups),
        "model_groups": await asyncio.to_thread(CHAT_STORE.list_model_groups),
        "presets": chat_plan_presets("gpt"),
        "presets_by_provider": {
            family: chat_plan_presets(family) for family in ("gpt", "claude")
        },
        "selections": list(SELECTIONS),
        "note": (
            "模型次数按用户独立计算；分组模板是 Turtle 的调度与公平使用规则，"
            "不代表 OpenAI/Anthropic 官方余额、消息数或 Token。"
        ),
    }


@router.get("/admin/resource-groups")
async def get_admin_resource_groups(user=Depends(get_admin_user)):
    return {"items": await asyncio.to_thread(CHAT_STORE.list_groups)}


@router.post("/admin/resource-groups")
async def create_resource_group(
    form_data: ResourceGroupForm,
    user=Depends(get_admin_user),
):
    try:
        return await asyncio.to_thread(
            CHAT_STORE.create_resource_group,
            name=form_data.name,
            description=form_data.description,
            storage_quota_bytes=form_data.storage_quota_bytes,
            max_concurrency=form_data.max_concurrency,
            default_user_concurrency=form_data.default_user_concurrency,
            updated_by=user.id,
        )
    except ChatPolicyError as exc:
        raise _bad_request(exc) from exc


@router.put("/admin/resource-groups/{group_id}")
async def update_resource_group(
    group_id: str,
    form_data: ResourceGroupForm,
    user=Depends(get_admin_user),
):
    try:
        return await asyncio.to_thread(
            CHAT_STORE.update_resource_group,
            group_id,
            name=form_data.name,
            description=form_data.description,
            storage_quota_bytes=form_data.storage_quota_bytes,
            max_concurrency=form_data.max_concurrency,
            default_user_concurrency=form_data.default_user_concurrency,
            updated_by=user.id,
        )
    except ChatPolicyError as exc:
        raise _bad_request(exc) from exc


@router.delete("/admin/resource-groups/{group_id}")
async def delete_resource_group(group_id: str, user=Depends(get_admin_user)):
    try:
        await asyncio.to_thread(CHAT_STORE.delete_group, group_id)
    except ChatPolicyError as exc:
        raise _bad_request(exc) from exc
    return {"ok": True}


@router.get("/admin/model-groups")
async def get_admin_model_groups(
    provider_family: str | None = Query(default=None, pattern="^(gpt|claude)$"),
    user=Depends(get_admin_user),
):
    selected_family = provider_family or "gpt"
    return {
        "items": await asyncio.to_thread(
            CHAT_STORE.list_model_groups,
            provider_family,
        ),
        "presets": chat_plan_presets(selected_family),
        "presets_by_provider": {
            family: chat_plan_presets(family) for family in ("gpt", "claude")
        },
        "selections": list(SELECTIONS),
    }


@router.post("/admin/model-groups")
async def create_model_group(
    form_data: ModelGroupForm,
    user=Depends(get_admin_user),
):
    try:
        return await asyncio.to_thread(
            CHAT_STORE.create_model_group,
            provider_family=form_data.provider_family,
            name=form_data.name,
            description=form_data.description,
            account_pool_id=form_data.account_pool_id,
            rules=[item.model_dump() for item in form_data.rules],
            updated_by=user.id,
        )
    except ChatPolicyError as exc:
        raise _bad_request(exc) from exc


@router.put("/admin/model-groups/{group_id}")
async def update_model_group(
    group_id: str,
    form_data: ModelGroupForm,
    user=Depends(get_admin_user),
):
    try:
        return await asyncio.to_thread(
            CHAT_STORE.update_model_group,
            group_id,
            provider_family=form_data.provider_family,
            name=form_data.name,
            description=form_data.description,
            account_pool_id=form_data.account_pool_id,
            rules=[item.model_dump() for item in form_data.rules],
            updated_by=user.id,
        )
    except ChatPolicyError as exc:
        raise _bad_request(exc) from exc


@router.delete("/admin/model-groups/{group_id}")
async def delete_model_group(group_id: str, user=Depends(get_admin_user)):
    try:
        await asyncio.to_thread(CHAT_STORE.delete_model_group, group_id)
    except ChatPolicyError as exc:
        raise _bad_request(exc) from exc
    return {"ok": True}


@router.get("/admin/groups")
async def get_admin_groups(user=Depends(get_admin_user)):
    return {
        "items": await asyncio.to_thread(CHAT_STORE.list_groups),
        "presets": chat_plan_presets("gpt"),
        "presets_by_provider": {
            family: chat_plan_presets(family) for family in ("gpt", "claude")
        },
        "selections": list(SELECTIONS),
    }


@router.post("/admin/groups")
async def create_group(form_data: GroupForm, user=Depends(get_admin_user)):
    try:
        return await asyncio.to_thread(
            CHAT_STORE.create_group,
            name=form_data.name,
            description=form_data.description,
            storage_quota_bytes=form_data.storage_quota_bytes,
            max_concurrency=form_data.max_concurrency,
            default_user_concurrency=form_data.default_user_concurrency,
            gpt_account_pool_id=form_data.gpt_account_pool_id,
            rules=[item.model_dump() for item in form_data.rules],
            updated_by=user.id,
        )
    except ChatPolicyError as exc:
        raise _bad_request(exc) from exc


@router.put("/admin/groups/{group_id}")
async def update_group(
    group_id: str,
    form_data: GroupForm,
    user=Depends(get_admin_user),
):
    try:
        return await asyncio.to_thread(
            CHAT_STORE.update_group,
            group_id,
            name=form_data.name,
            description=form_data.description,
            storage_quota_bytes=form_data.storage_quota_bytes,
            max_concurrency=form_data.max_concurrency,
            default_user_concurrency=form_data.default_user_concurrency,
            gpt_account_pool_id=form_data.gpt_account_pool_id,
            rules=[item.model_dump() for item in form_data.rules],
            updated_by=user.id,
        )
    except ChatPolicyError as exc:
        raise _bad_request(exc) from exc


@router.delete("/admin/groups/{group_id}")
async def delete_group(group_id: str, user=Depends(get_admin_user)):
    try:
        await asyncio.to_thread(CHAT_STORE.delete_group, group_id)
    except ChatPolicyError as exc:
        raise _bad_request(exc) from exc
    return {"ok": True}


@router.put("/admin/users/{user_id}/group")
async def assign_user_group(
    user_id: str,
    form_data: UserGroupForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    target = await _target_or_404(user_id, db)
    try:
        await asyncio.to_thread(
            CHAT_STORE.assign_group,
            target.id,
            form_data.group_id,
            assigned_by=user.id,
            role=target.role,
        )
    except ChatPolicyError as exc:
        raise _bad_request(exc) from exc
    return await _user_payload(target)


@router.put("/admin/users/{user_id}/resource-group")
async def assign_user_resource_group(
    user_id: str,
    form_data: UserGroupForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    target = await _target_or_404(user_id, db)
    try:
        await asyncio.to_thread(
            CHAT_STORE.assign_resource_group,
            target.id,
            form_data.group_id,
            assigned_by=user.id,
            role=target.role,
        )
    except ChatPolicyError as exc:
        raise _bad_request(exc) from exc
    return await _user_payload(target)


@router.put("/admin/users/{user_id}/model-groups/{provider_family}")
async def assign_user_model_group(
    user_id: str,
    provider_family: str,
    form_data: UserGroupForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    target = await _target_or_404(user_id, db)
    try:
        await asyncio.to_thread(
            CHAT_STORE.assign_model_group,
            target.id,
            provider_family,
            form_data.group_id,
            assigned_by=user.id,
            role=target.role,
        )
    except ChatPolicyError as exc:
        raise _bad_request(exc) from exc
    return await _user_payload(target)


@router.put("/admin/users/bulk-groups")
async def bulk_assign_user_groups(
    form_data: BulkUserGroupsForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    requested_ids = list(dict.fromkeys(str(value).strip() for value in form_data.user_ids))
    if any(not value or len(value) > 80 for value in requested_ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户列表包含无效 ID",
        )
    if not any(
        (
            form_data.resource_group_id,
            form_data.gpt_model_group_id,
            form_data.claude_model_group_id,
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请至少选择一个需要调整的分组",
        )

    directory = await Users.get_users(skip=0, limit=None, db=db)
    targets = {
        str(target.id): target for target in directory.get("users", [])
        if str(target.id) in requested_ids
    }
    if len(targets) != len(requested_ids):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="部分用户不存在，请刷新列表后重试",
        )
    if any(str(target.role) == "admin" for target in targets.values()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="管理员不参与批量订阅分组调整",
        )

    model_group_ids = {
        provider: group_id
        for provider, group_id in {
            "gpt": form_data.gpt_model_group_id,
            "claude": form_data.claude_model_group_id,
        }.items()
        if group_id
    }
    try:
        updated = await asyncio.to_thread(
            CHAT_STORE.bulk_assign_groups,
            requested_ids,
            resource_group_id=form_data.resource_group_id,
            model_group_ids=model_group_ids,
            assigned_by=user.id,
        )
    except ChatPolicyError as exc:
        raise _bad_request(exc) from exc
    return {"ok": True, "updated": updated}


@router.put("/admin/users/{user_id}/concurrency")
async def update_user_concurrency(
    user_id: str,
    form_data: UserConcurrencyForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    target = await _target_or_404(user_id, db)
    try:
        await asyncio.to_thread(
            CHAT_STORE.set_user_concurrency,
            target.id,
            target.role,
            max_concurrency=form_data.max_concurrency,
            updated_by=user.id,
        )
    except ChatPolicyError as exc:
        raise _bad_request(exc) from exc
    return await _user_payload(target)


@router.put("/admin/users/{user_id}/subscription")
async def update_user_subscription(
    user_id: str,
    form_data: UserSubscriptionForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    target = await _target_or_404(user_id, db)
    try:
        await SUBSCRIPTION_CACHE.set_subscription(
            target.id,
            target.role,
            starts_at=form_data.starts_at,
            expires_at=form_data.expires_at,
            duration_days=form_data.duration_days,
            updated_by=user.id,
        )
    except ChatPolicyError as exc:
        raise _bad_request(exc) from exc
    return await _user_payload(target)


@router.post("/admin/users/{user_id}/subscription/extend")
async def extend_user_subscription(
    user_id: str,
    form_data: SubscriptionExtendForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    target = await _target_or_404(user_id, db)
    try:
        await SUBSCRIPTION_CACHE.extend_subscription(
            target.id,
            target.role,
            days=form_data.days,
            updated_by=user.id,
        )
    except ChatPolicyError as exc:
        raise _bad_request(exc) from exc
    return await _user_payload(target)


@router.post("/admin/users/{user_id}/subscription/cancel")
async def cancel_user_subscription(
    user_id: str,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    target = await _target_or_404(user_id, db)
    try:
        await SUBSCRIPTION_CACHE.cancel_subscription(
            target.id,
            target.role,
            updated_by=user.id,
        )
    except ChatPolicyError as exc:
        raise _bad_request(exc) from exc
    return await _user_payload(target)


@router.get("/admin/users/{user_id}/subscription/events")
async def get_user_subscription_events(
    user_id: str,
    limit: int = Query(20, ge=1, le=100),
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    target = await _target_or_404(user_id, db)
    return {
        "subscription": await SUBSCRIPTION_CACHE.get(target.id, target.role),
        "items": await asyncio.to_thread(
            CHAT_STORE.subscription_events, target.id, limit
        ),
    }


@router.post("/admin/users/{user_id}/quota/reset")
async def reset_user_quota(
    user_id: str,
    form_data: QuotaResetForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    target = await _target_or_404(user_id, db)
    try:
        await asyncio.to_thread(
            CHAT_STORE.reset_quota_windows, target.id, form_data.selection_key
        )
    except ChatPolicyError as exc:
        raise _bad_request(exc) from exc
    return await _user_payload(target)


@router.put("/admin/users/{user_id}/policy")
async def update_user_policy(
    user_id: str,
    form_data: UserPolicyForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Legacy endpoint: applying it intentionally returns the user to custom mode."""
    target = await _target_or_404(user_id, db)
    try:
        await asyncio.to_thread(
            CHAT_STORE.set_policy,
            target.id,
            allowed=form_data.allowed,
            updated_by=user.id,
        )
    except ChatPolicyError as exc:
        raise _bad_request(exc) from exc
    return await _user_payload(target)
