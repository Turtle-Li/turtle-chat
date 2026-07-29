"""Atomic, batched persistence for one existing-chat foreground turn."""

from __future__ import annotations

import copy
import logging
import time
from collections.abc import Callable
from typing import Any


log = logging.getLogger(__name__)


async def _stage_normalized_message(
    *,
    session: Any,
    chat_id: str,
    user_id: str,
    message_id: str,
    data: dict[str, Any],
) -> None:
    """Stage one ``chat_message`` upsert in the caller's transaction."""

    from open_webui.models.chat_messages import ChatMessage, get_usage
    from open_webui.utils.response import merge_usage, normalize_usage

    now = int(time.time())
    composite_id = f"{chat_id}-{message_id}"
    existing = await session.get(ChatMessage, composite_id)
    if existing is None:
        session.add(
            ChatMessage(
                id=composite_id,
                chat_id=chat_id,
                user_id=user_id,
                role=data.get("role", "user"),
                parent_id=data.get("parent_id") or data.get("parentId"),
                content=data.get("content"),
                output=data.get("output"),
                model_id=data.get("model_id") or data.get("model"),
                files=data.get("files"),
                sources=data.get("sources"),
                embeds=data.get("embeds"),
                meta=data.get("meta"),
                done=data.get("done", True),
                status_history=data.get("status_history")
                or data.get("statusHistory"),
                error=data.get("error"),
                usage=get_usage(data),
                context_summary=data.get("context_summary")
                or data.get("contextSummary"),
                created_at=data.get("timestamp", now),
                updated_at=now,
            )
        )
        return

    if "role" in data:
        existing.role = data["role"]
    if "parent_id" in data or "parentId" in data:
        existing.parent_id = data.get("parent_id") or data.get("parentId")
    for key in ("content", "output", "files", "sources", "embeds", "meta", "error"):
        if key in data:
            setattr(existing, key, data.get(key))
    if "model_id" in data or "model" in data:
        existing.model_id = data.get("model_id") or data.get("model")
    if "done" in data:
        existing.done = data.get("done", True)
    if "status_history" in data or "statusHistory" in data:
        existing.status_history = data.get("status_history") or data.get(
            "statusHistory"
        )
    if "context_summary" in data or "contextSummary" in data:
        existing.context_summary = data.get("context_summary") or data.get(
            "contextSummary"
        )
    usage = get_usage(data)
    if usage:
        existing_usage = (
            normalize_usage(existing.usage or {}) if existing.usage else {}
        )
        existing.usage = (
            existing_usage
            if usage == existing_usage
            else merge_usage(existing_usage, usage)
        )
    existing.updated_at = now


class ExistingChatWriteBatch:
    """Merge related message-graph writes into one locked chat-row update.

    Open WebUI's ordinary path updates the embedded chat JSON, normalized
    ``chat_message`` row, and Turtle range index for every small graph change.
    A foreground turn changes the user message, its two adjacent links, and
    the assistant placeholder together. Rewriting the same JSON four times
    delays upstream dispatch without adding durability. This batch commits
    the authoritative JSON and normalized rows in one transaction. The range
    index intentionally becomes stale by timestamp and rebuilds lazily from
    that authoritative JSON on the next bounded history read.
    """

    def __init__(
        self,
        chat_model: Any,
        upsert_message_to_history: Callable[[dict, str, dict], dict],
    ) -> None:
        self.chat_id = str(chat_model.id)
        self.user_id = str(chat_model.user_id)
        self.chat = copy.deepcopy(chat_model.chat or {})
        self.variables = copy.deepcopy(chat_model.variables or {})
        self._upsert_message_to_history = upsert_message_to_history
        self._message_operations: list[tuple[str, dict[str, Any]]] = []
        self._changed_message_ids: set[str] = set()
        self._changed_message_order: list[str] = []

        history = self.chat.get("history")
        if not isinstance(history, dict):
            history = {}
            self.chat["history"] = history
        if not isinstance(history.get("messages"), dict):
            history["messages"] = {}

    @property
    def messages(self) -> dict[str, dict[str, Any]]:
        return self.chat["history"]["messages"]

    def get_message(self, message_id: str | None) -> dict[str, Any] | None:
        if not message_id:
            return None
        message = self.messages.get(str(message_id))
        return copy.deepcopy(message) if isinstance(message, dict) else None

    def stage_message(self, message_id: str, message: dict[str, Any]) -> dict[str, Any]:
        message_id = str(message_id)
        patch = copy.deepcopy(message)
        saved = self._upsert_message_to_history(
            self.chat["history"],
            message_id,
            patch,
        )
        self._message_operations.append((message_id, patch))
        if message_id not in self._changed_message_ids:
            self._changed_message_order.append(message_id)
        self._changed_message_ids.add(message_id)
        return copy.deepcopy(saved)

    def update_chat_fields(
        self,
        *,
        files: list[Any] | None,
        models: list[str] | None,
    ) -> None:
        if files is not None:
            self.chat["files"] = copy.deepcopy(files)
        if models:
            self.chat["models"] = list(models)

    def update_variables(self, variables: dict[str, Any] | None) -> None:
        self.variables = copy.deepcopy(variables) if isinstance(variables, dict) else {}

    async def commit(self) -> None:
        batch_started = time.perf_counter()
        # These imports deliberately stay lazy so the helper remains testable
        # outside the pinned Open WebUI image.
        from open_webui.internal.db import get_async_db_context
        from open_webui.models.chats import Chat, Chats
        from open_webui.turtle_chat.provider import meta_with_provider
        from sqlalchemy import select
        from sqlalchemy.orm.attributes import flag_modified

        async with get_async_db_context() as session:
            row_lock_started = time.perf_counter()
            result = await session.execute(
                select(Chat)
                .where(Chat.id == self.chat_id)
                .with_for_update()
            )
            chat_item = result.scalar_one_or_none()
            if chat_item is None or str(chat_item.user_id) != self.user_id:
                raise RuntimeError("chat write target is no longer available")
            row_lock_ms = int((time.perf_counter() - row_lock_started) * 1000)

            Chats._sanitize_chat_row(chat_item)
            latest_chat = copy.deepcopy(chat_item.chat or {})
            Chats._repair_chat_current_id(latest_chat)
            history = latest_chat.get("history")
            if not isinstance(history, dict):
                history = {}
                latest_chat["history"] = history
            if not isinstance(history.get("messages"), dict):
                history["messages"] = {}

            saved_messages: dict[str, dict[str, Any]] = {}
            for message_id, message in self._message_operations:
                saved_messages[message_id] = copy.deepcopy(
                    self._upsert_message_to_history(
                        history,
                        message_id,
                        copy.deepcopy(message),
                    )
                )

            if "files" in self.chat:
                latest_chat["files"] = copy.deepcopy(self.chat["files"])
            if self.chat.get("models"):
                latest_chat["models"] = list(self.chat["models"])

            clean_chat = Chats._clean_null_bytes(latest_chat)
            chat_item.chat = clean_chat
            chat_item.variables = copy.deepcopy(self.variables)
            chat_item.title = (
                Chats._clean_null_bytes(clean_chat["title"])
                if "title" in clean_chat
                else "New Chat"
            )
            chat_item.meta = meta_with_provider(chat_item.meta, clean_chat)
            chat_item.current_message_id = Chats.get_current_message_id(clean_chat)
            chat_item.updated_at = int(time.time())
            flag_modified(chat_item, "chat")

            changed = {
                message_id: saved_messages[message_id]
                for message_id in self._changed_message_order
                if message_id in self._changed_message_ids
                and message_id in saved_messages
            }
            normalized_started = time.perf_counter()
            normalized_error: Exception | None = None
            if changed:
                try:
                    async with session.begin_nested():
                        for message_id, message in changed.items():
                            await _stage_normalized_message(
                                session=session,
                                chat_id=self.chat_id,
                                user_id=self.user_id,
                                message_id=message_id,
                                data=message,
                            )
                        # Surface a compatibility-row error inside the
                        # savepoint so the authoritative chat JSON can still
                        # commit on the fallback path.
                        await session.flush()
                except Exception as exc:
                    normalized_error = exc
            normalized_ms = int(
                (time.perf_counter() - normalized_started) * 1000
            )

            chat_commit_started = time.perf_counter()
            await session.commit()
            chat_commit_ms = int(
                (time.perf_counter() - chat_commit_started) * 1000
            )

        if normalized_error is not None:
            log.warning(
                "Batch chat_message dual-write failed (%s); reconciling",
                type(normalized_error).__name__,
            )
            try:
                await Chats.reconcile_messages_by_chat_id(
                    self.chat_id,
                    self.user_id,
                    clean_chat.get("history", {}).get("messages", {}),
                )
            except Exception as exc:
                log.warning(
                    "Batch chat_message reconciliation failed (%s)",
                    type(exc).__name__,
                )

        log.info(
            "turtle_chat_write_batch row_lock_ms=%d chat_commit_ms=%d "
            "index_ms=0 index_mode=lazy normalized_ms=%d total_ms=%d changed=%d",
            row_lock_ms,
            chat_commit_ms,
            normalized_ms,
            int((time.perf_counter() - batch_started) * 1000),
            len(changed),
        )
