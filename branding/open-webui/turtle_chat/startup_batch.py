"""Atomic, batched persistence for one existing-chat foreground turn."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections.abc import Callable
from typing import Any


log = logging.getLogger(__name__)


async def _sync_changed_history_rows(
    *,
    session: Any,
    chat_id: str,
    user_id: str,
    changed_order: list[str],
    changed_messages: dict[str, dict[str, Any]],
    chat: dict[str, Any],
    chat_updated_at: int,
) -> bool:
    """Update all changed range-index rows under one state lock/commit."""

    from open_webui.turtle_chat.history import (
        TurtleChatHistoryMessage,
        TurtleChatHistoryState,
        _chat_shell,
        _safe_timestamp,
        ensure_history_schema,
    )
    from sqlalchemy import select

    await ensure_history_schema(session)
    state_result = await session.execute(
        select(TurtleChatHistoryState)
        .where(TurtleChatHistoryState.chat_id == chat_id)
        .with_for_update()
    )
    state = state_result.scalar_one_or_none()
    if state is None or int(state.chat_updated_at) < 0:
        return False

    rows: dict[str, Any] = {}
    for message_id in changed_order:
        rows[message_id] = await session.get(
            TurtleChatHistoryMessage,
            {"chat_id": chat_id, "message_id": message_id},
        )

    history = chat.get("history") if isinstance(chat.get("history"), dict) else {}
    all_messages = (
        history.get("messages")
        if isinstance(history.get("messages"), dict)
        else {}
    )
    final_count = len(all_messages)
    new_count = sum(1 for row in rows.values() if row is None)
    if int(state.message_count) != final_count - new_count:
        state.chat_updated_at = -1
        await session.commit()
        return False

    for message_id in changed_order:
        payload = copy.deepcopy(changed_messages[message_id])
        parent_value = (
            payload.get("parentId")
            if "parentId" in payload
            else payload.get("parent_id")
        )
        parent_id = (
            str(parent_value)
            if parent_value not in {None, ""}
            else None
        )
        row = rows[message_id]
        if row is not None and (row.parent_id or None) != parent_id:
            state.chat_updated_at = -1
            await session.commit()
            return False

        if row is None:
            parent_depth = -1
            if parent_id is not None:
                parent = rows.get(parent_id)
                if parent is None:
                    parent = await session.get(
                        TurtleChatHistoryMessage,
                        {"chat_id": chat_id, "message_id": parent_id},
                    )
                if parent is None:
                    state.chat_updated_at = -1
                    await session.commit()
                    return False
                parent_depth = int(parent.depth)
            row = TurtleChatHistoryMessage(
                chat_id=chat_id,
                message_id=message_id,
                user_id=user_id,
                depth=parent_depth + 1,
                parent_id=parent_id,
                created_at=_safe_timestamp(payload.get("timestamp")),
                payload={},
            )
            session.add(row)
            rows[message_id] = row

        payload["id"] = message_id
        payload["parentId"] = parent_id
        payload["childrenIds"] = (
            payload.get("childrenIds")
            if isinstance(payload.get("childrenIds"), list)
            else []
        )
        payload.setdefault("content", "")
        row.user_id = user_id
        row.parent_id = parent_id
        row.created_at = _safe_timestamp(payload.get("timestamp"))
        row.payload = payload

    current_id_value = history.get("currentId")
    current_id = str(current_id_value) if current_id_value else None
    current_row = rows.get(current_id) if current_id else None
    if current_id and current_row is None:
        current_row = await session.get(
            TurtleChatHistoryMessage,
            {"chat_id": chat_id, "message_id": current_id},
        )
    if current_id and current_row is None:
        state.chat_updated_at = -1
        await session.commit()
        return False

    all_depths = [int(row.depth) for row in rows.values() if row is not None]
    state.user_id = user_id
    state.chat_updated_at = int(chat_updated_at or 0)
    state.current_message_id = current_id
    state.current_depth = int(current_row.depth) if current_row is not None else 0
    state.min_depth = min([int(state.min_depth or 0), *all_depths])
    state.max_depth = max([int(state.max_depth or 0), *all_depths])
    state.message_count = final_count
    state.shell = _chat_shell(chat, current_message_id=current_id)
    state.indexed_at = int(time.time())
    await session.commit()
    return True


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
        from open_webui.models.chat_messages import ChatMessages
        from open_webui.models.chats import Chat, Chats
        from open_webui.turtle_chat.history import sync_chat_history_index
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
            chat_commit_started = time.perf_counter()
            await session.commit()
            chat_commit_ms = int(
                (time.perf_counter() - chat_commit_started) * 1000
            )

            index_started = time.perf_counter()
            try:
                incremental_indexed = await _sync_changed_history_rows(
                    session=session,
                    chat_id=self.chat_id,
                    user_id=self.user_id,
                    changed_order=self._changed_message_order,
                    changed_messages=saved_messages,
                    chat=clean_chat,
                    chat_updated_at=chat_item.updated_at,
                )
                if not incremental_indexed:
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
                await session.rollback()
                await sync_chat_history_index(
                    self.chat_id,
                    self.user_id,
                    clean_chat,
                    chat_item.updated_at,
                    db=session,
                )
            index_ms = int((time.perf_counter() - index_started) * 1000)

        changed = {
            message_id: saved_messages[message_id]
            for message_id in self._changed_message_ids
            if message_id in saved_messages
        }
        if not changed:
            log.info(
                "turtle_chat_write_batch row_lock_ms=%d chat_commit_ms=%d "
                "index_ms=%d normalized_ms=0 total_ms=%d changed=0",
                row_lock_ms,
                chat_commit_ms,
                index_ms,
                int((time.perf_counter() - batch_started) * 1000),
            )
            return

        normalized_started = time.perf_counter()
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
        normalized_ms = int((time.perf_counter() - normalized_started) * 1000)
        log.info(
            "turtle_chat_write_batch row_lock_ms=%d chat_commit_ms=%d "
            "index_ms=%d normalized_ms=%d total_ms=%d changed=%d",
            row_lock_ms,
            chat_commit_ms,
            index_ms,
            normalized_ms,
            int((time.perf_counter() - batch_started) * 1000),
            len(changed),
        )
