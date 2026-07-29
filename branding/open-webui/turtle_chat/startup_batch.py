"""Atomic, batched persistence for one existing-chat foreground turn."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections.abc import Callable
from typing import Any


log = logging.getLogger(__name__)


class ExistingChatWriteBatch:
    """Merge related message-graph writes into one locked chat-row update.

    Open WebUI's ordinary path updates the embedded chat JSON, normalized
    ``chat_message`` row, and Turtle range index for every small graph change.
    A foreground turn changes the user message, its two adjacent links, and
    the assistant placeholder together.  Rewriting the same JSON four times
    delays upstream dispatch without adding durability.  This batch keeps the
    same three stores in sync while committing the authoritative chat JSON
    only once.
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
        # These imports deliberately stay lazy so the helper remains testable
        # outside the pinned Open WebUI image.
        from open_webui.internal.db import get_async_db_context
        from open_webui.models.chat_messages import ChatMessages
        from open_webui.models.chats import Chat, Chats
        from open_webui.turtle_chat.history import (
            invalidate_chat_history_index,
            sync_chat_history_index,
        )
        from open_webui.turtle_chat.provider import meta_with_provider
        from sqlalchemy import select
        from sqlalchemy.orm.attributes import flag_modified

        async with get_async_db_context() as session:
            result = await session.execute(
                select(Chat)
                .where(Chat.id == self.chat_id)
                .with_for_update()
            )
            chat_item = result.scalar_one_or_none()
            if chat_item is None or str(chat_item.user_id) != self.user_id:
                raise RuntimeError("chat write target is no longer available")

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
            await session.commit()

            try:
                await sync_chat_history_index(
                    self.chat_id,
                    self.user_id,
                    clean_chat,
                    chat_item.updated_at,
                    db=session,
                )
            except Exception as exc:
                log.warning(
                    "Failed to batch-update indexed chat history for %s: %s",
                    self.chat_id,
                    type(exc).__name__,
                )
                try:
                    await invalidate_chat_history_index(self.chat_id, db=session)
                except Exception:
                    pass

        changed = {
            message_id: saved_messages[message_id]
            for message_id in self._changed_message_ids
            if message_id in saved_messages
        }
        if not changed:
            return

        results = await asyncio.gather(
            *(
                ChatMessages.upsert_message(
                    message_id=message_id,
                    chat_id=self.chat_id,
                    user_id=self.user_id,
                    data=message,
                )
                for message_id, message in changed.items()
            ),
            return_exceptions=True,
        )
        if any(isinstance(result, BaseException) for result in results):
            # The embedded JSON is authoritative.  Repair the normalized rows
            # on this exceptional path instead of making every normal request
            # pay for a full-history reconciliation.
            log.warning(
                "Batch chat_message dual-write failed for %s; reconciling",
                self.chat_id,
            )
            await Chats.reconcile_messages_by_chat_id(
                self.chat_id,
                self.user_id,
                clean_chat.get("history", {}).get("messages", {}),
            )
