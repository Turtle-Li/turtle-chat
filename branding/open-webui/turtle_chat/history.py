"""Indexed, bounded-range chat history reads for Turtle's Chat.

Open WebUI keeps a complete compatibility JSON document on ``chat.chat``.
That document remains a rollback/interoperability source, but sending it over
the network for every conversation open makes long chats progressively slower.
This module maintains a complete message payload table plus a lightweight chat
shell. History pages are selected by an indexed depth range; no offset or row
count truncation is used for conversation history.
"""

from __future__ import annotations

import asyncio
import copy
import time
from contextlib import asynccontextmanager
from typing import Any

from open_webui.internal.db import Base, get_async_db
from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    delete,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession


HISTORY_INITIAL_DEPTH_SPAN = 16
HISTORY_DEPTH_SPAN = 8


class TurtleChatHistoryMessage(Base):
    __tablename__ = "turtle_chat_history_message"

    chat_id = Column(
        String,
        ForeignKey("chat.id", ondelete="CASCADE"),
        primary_key=True,
    )
    message_id = Column(Text, primary_key=True)
    user_id = Column(String, nullable=False)
    depth = Column(Integer, nullable=False)
    parent_id = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    payload = Column(JSON, nullable=False)

    __table_args__ = (
        Index(
            "turtle_chat_history_range_idx",
            "chat_id",
            "depth",
            "message_id",
        ),
    )


class TurtleChatHistoryState(Base):
    __tablename__ = "turtle_chat_history_state"

    chat_id = Column(
        String,
        ForeignKey("chat.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id = Column(String, nullable=False)
    chat_updated_at = Column(BigInteger, nullable=False)
    current_message_id = Column(Text, nullable=True)
    current_depth = Column(Integer, nullable=False, default=0)
    min_depth = Column(Integer, nullable=False, default=0)
    max_depth = Column(Integer, nullable=False, default=0)
    message_count = Column(Integer, nullable=False, default=0)
    shell = Column(JSON, nullable=False)
    indexed_at = Column(BigInteger, nullable=False)


_SCHEMA_LOCK = asyncio.Lock()
_SCHEMA_READY_DATABASES: set[str] = set()


@asynccontextmanager
async def _history_session(db: AsyncSession | None = None):
    """Always honor an explicitly supplied transactional session."""

    if isinstance(db, AsyncSession):
        yield db
        return
    async with get_async_db() as session:
        yield session


async def ensure_history_schema(db: AsyncSession) -> None:
    """Create only Turtle's two additive history tables when first needed."""

    connection = await db.connection()
    schema_key = str(connection.engine.url)
    if schema_key in _SCHEMA_READY_DATABASES:
        return

    async with _SCHEMA_LOCK:
        if schema_key in _SCHEMA_READY_DATABASES:
            return
        if connection.dialect.name == "postgresql":
            # ``checkfirst`` alone can race when several Open WebUI workers
            # receive their first history request together. The transaction-
            # scoped lock makes the additive DDL single-flight across processes.
            await db.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtext('turtle-chat-history-schema-v1'))"
                )
            )

        def create_tables(sync_connection) -> None:
            Base.metadata.create_all(
                sync_connection,
                tables=[
                    TurtleChatHistoryMessage.__table__,
                    TurtleChatHistoryState.__table__,
                ],
                checkfirst=True,
            )

        await connection.run_sync(create_tables)
        await db.commit()
        _SCHEMA_READY_DATABASES.add(schema_key)


def _message_map(chat: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    value = chat if isinstance(chat, dict) else {}
    history = value.get("history") if isinstance(value.get("history"), dict) else {}
    messages = history.get("messages")
    if isinstance(messages, dict):
        return {
            str(message_id): copy.deepcopy(message)
            for message_id, message in messages.items()
            if message_id and isinstance(message, dict)
        }

    legacy = value.get("messages")
    if not isinstance(legacy, list):
        return {}
    return {
        str(message["id"]): copy.deepcopy(message)
        for message in legacy
        if isinstance(message, dict) and message.get("id")
    }


def _normalized_messages(
    chat: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, int], str | None]:
    messages = _message_map(chat)
    parent_by_id: dict[str, str | None] = {}
    for message_id, message in messages.items():
        parent = (
            message.get("parentId")
            if "parentId" in message
            else message.get("parent_id")
        )
        parent_by_id[message_id] = str(parent) if parent not in {None, ""} else None

    depths: dict[str, int] = {}
    for message_id in messages:
        if message_id in depths:
            continue
        trail: list[str] = []
        positions: dict[str, int] = {}
        current: str | None = message_id
        while current in messages and current not in depths:
            if current in positions:
                # Malformed legacy cycles must never hang indexing. Treat the
                # cycle entry as a synthetic root and keep deterministic depth.
                cycle_at = positions[current]
                for offset, cycle_id in enumerate(trail[cycle_at:]):
                    depths[cycle_id] = offset
                trail = trail[:cycle_at]
                current = None
                break
            positions[current] = len(trail)
            trail.append(current)
            current = parent_by_id.get(current)

        base_depth = depths.get(current, -1) if current is not None else -1
        for pending_id in reversed(trail):
            base_depth += 1
            depths[pending_id] = base_depth

    for message_id, message in messages.items():
        message["id"] = message_id
        message["parentId"] = parent_by_id[message_id]
        message["childrenIds"] = []
        message.setdefault("content", "")

    child_order = sorted(
        messages,
        key=lambda message_id: (
            depths.get(message_id, 0),
            _safe_timestamp(messages[message_id].get("timestamp")),
            message_id,
        ),
    )
    for message_id in child_order:
        parent_id = parent_by_id[message_id]
        if parent_id in messages:
            messages[parent_id]["childrenIds"].append(message_id)

    value = chat if isinstance(chat, dict) else {}
    history = value.get("history") if isinstance(value.get("history"), dict) else {}
    current_id = history.get("currentId") or value.get("currentId")
    current_id = str(current_id) if current_id in messages else None
    if current_id is None and messages:
        leaves = [
            message_id
            for message_id, message in messages.items()
            if not message["childrenIds"]
        ]
        candidates = leaves or list(messages)
        current_id = max(
            candidates,
            key=lambda message_id: (
                _safe_timestamp(messages[message_id].get("timestamp")),
                depths.get(message_id, 0),
                message_id,
            ),
        )
    return messages, depths, current_id


def _safe_timestamp(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _chat_shell(
    chat: dict[str, Any] | None,
    *,
    current_message_id: str | None,
) -> dict[str, Any]:
    shell = copy.deepcopy(chat) if isinstance(chat, dict) else {}
    history = shell.get("history") if isinstance(shell.get("history"), dict) else {}
    history = {
        key: value
        for key, value in history.items()
        if key not in {"messages", "turtlePage"}
    }
    history["currentId"] = current_message_id
    shell["history"] = history
    # Older exports can duplicate the entire history as a top-level list.
    shell.pop("messages", None)
    return shell


async def sync_chat_history_index(
    chat_id: str,
    user_id: str,
    chat: dict[str, Any] | None,
    chat_updated_at: int,
    *,
    db: AsyncSession,
) -> TurtleChatHistoryState:
    """Atomically replace one chat's range index from its committed JSON."""

    await ensure_history_schema(db)
    messages, depths, current_id = _normalized_messages(chat)
    min_depth = min(depths.values(), default=0)
    max_depth = max(depths.values(), default=0)
    current_depth = depths.get(current_id, max_depth if messages else 0)

    await db.execute(
        delete(TurtleChatHistoryMessage).where(
            TurtleChatHistoryMessage.chat_id == chat_id
        )
    )
    db.add_all(
        [
            TurtleChatHistoryMessage(
                chat_id=chat_id,
                message_id=message_id,
                user_id=user_id,
                depth=depths[message_id],
                parent_id=message.get("parentId"),
                created_at=_safe_timestamp(message.get("timestamp")),
                payload=message,
            )
            for message_id, message in messages.items()
        ]
    )

    state = await db.get(TurtleChatHistoryState, chat_id)
    values = {
        "user_id": user_id,
        "chat_updated_at": int(chat_updated_at or 0),
        "current_message_id": current_id,
        "current_depth": current_depth,
        "min_depth": min_depth,
        "max_depth": max_depth,
        "message_count": len(messages),
        "shell": _chat_shell(chat, current_message_id=current_id),
        "indexed_at": int(time.time()),
    }
    if state is None:
        state = TurtleChatHistoryState(chat_id=chat_id, **values)
        db.add(state)
    else:
        for key, value in values.items():
            setattr(state, key, value)
    await db.commit()
    return state


async def sync_indexed_chat_message(
    chat_id: str,
    user_id: str,
    message_id: str,
    message: dict[str, Any],
    chat_updated_at: int,
    current_message_id: str | None,
    message_count: int,
    *,
    db: AsyncSession | None = None,
) -> bool:
    """Update one already-built history index after a message mutation.

    Streaming completion code may persist the same assistant row several
    times. Replacing the complete payload in place avoids rebuilding every
    prior message on each update. Any graph/count ambiguity marks the index
    stale so the next bounded read safely rebuilds from the compatibility JSON.
    """

    async with _history_session(db) as session:
        await ensure_history_schema(session)
        state_result = await session.execute(
            select(TurtleChatHistoryState)
            .where(TurtleChatHistoryState.chat_id == chat_id)
            .with_for_update()
        )
        state = state_result.scalar_one_or_none()
        if state is None or int(state.chat_updated_at) < 0:
            return False

        row = await session.get(
            TurtleChatHistoryMessage,
            {"chat_id": chat_id, "message_id": message_id},
        )
        is_new = row is None
        expected_existing_count = int(message_count) - (1 if is_new else 0)
        if (
            expected_existing_count < 0
            or int(state.message_count) != expected_existing_count
        ):
            state.chat_updated_at = -1
            await session.commit()
            return False

        payload = copy.deepcopy(message) if isinstance(message, dict) else {}
        parent_value = (
            payload.get("parentId")
            if "parentId" in payload
            else payload.get("parent_id")
        )
        parent_id = str(parent_value) if parent_value not in {None, ""} else None
        if row is not None and (row.parent_id or None) != parent_id:
            state.chat_updated_at = -1
            await session.commit()
            return False

        if row is None:
            parent_depth = -1
            if parent_id is not None:
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

        state.user_id = user_id
        state.chat_updated_at = int(chat_updated_at or 0)
        state.message_count = int(message_count)
        state.min_depth = min(int(state.min_depth), int(row.depth))
        state.max_depth = max(int(state.max_depth), int(row.depth))
        if current_message_id:
            current_id = str(current_message_id)
            state.current_message_id = current_id
            if current_id == message_id:
                state.current_depth = int(row.depth)
            history = (
                state.shell.get("history")
                if isinstance(state.shell, dict)
                and isinstance(state.shell.get("history"), dict)
                else {}
            )
            history["currentId"] = current_id
            state.shell = {**(state.shell or {}), "history": history}
        state.indexed_at = int(time.time())
        await session.commit()
        return True


async def invalidate_chat_history_index(
    chat_id: str,
    *,
    db: AsyncSession | None = None,
) -> None:
    """Force a safe lazy rebuild after a graph-changing mutation."""

    async with _history_session(db) as session:
        await ensure_history_schema(session)
        state = await session.get(TurtleChatHistoryState, chat_id)
        if state is not None:
            state.chat_updated_at = -1
            await session.commit()


async def get_chat_envelope(
    chat_id: str,
    *,
    db: AsyncSession,
) -> dict[str, Any] | None:
    """Fetch response/access fields without selecting the large chat JSON."""

    from open_webui.models.chats import Chat

    result = await db.execute(
        select(
            Chat.id,
            Chat.user_id,
            Chat.title,
            Chat.created_at,
            Chat.updated_at,
            Chat.share_id,
            Chat.archived,
            Chat.pinned,
            Chat.meta,
            Chat.variables,
            Chat.folder_id,
            Chat.tasks,
            Chat.summary,
            Chat.current_message_id,
            Chat.last_read_at,
        ).where(Chat.id == chat_id)
    )
    row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


async def _fresh_state(
    envelope: dict[str, Any],
    *,
    db: AsyncSession,
) -> TurtleChatHistoryState:
    from open_webui.models.chats import Chat

    await ensure_history_schema(db)
    chat_id = str(envelope["id"])
    state = await db.get(TurtleChatHistoryState, chat_id)
    expected_current = envelope.get("current_message_id")
    if (
        state is not None
        and int(state.chat_updated_at) == int(envelope.get("updated_at") or 0)
        and (state.current_message_id or None) == (expected_current or None)
    ):
        return state

    result = await db.execute(select(Chat.chat).where(Chat.id == chat_id))
    chat = result.scalar_one_or_none()
    if chat is None:
        raise LookupError("chat disappeared while indexing")
    return await sync_chat_history_index(
        chat_id,
        str(envelope["user_id"]),
        chat,
        int(envelope.get("updated_at") or 0),
        db=db,
    )


def _page_metadata(
    state: TurtleChatHistoryState,
    *,
    range_start: int,
    range_end: int,
    span: int = HISTORY_DEPTH_SPAN,
) -> dict[str, Any]:
    return {
        "rangeStart": range_start,
        "rangeEnd": range_end,
        "hasMore": range_start > int(state.min_depth),
        "span": span,
        "revision": int(state.chat_updated_at),
        "messageCount": int(state.message_count),
    }


async def _range_messages(
    chat_id: str,
    *,
    range_start: int,
    range_end: int,
    db: AsyncSession,
) -> dict[str, dict[str, Any]]:
    """Read every row in one indexed half-open depth range."""

    result = await db.execute(
        select(TurtleChatHistoryMessage)
        .where(
            TurtleChatHistoryMessage.chat_id == chat_id,
            TurtleChatHistoryMessage.depth >= range_start,
            TurtleChatHistoryMessage.depth < range_end,
        )
        .order_by(
            TurtleChatHistoryMessage.depth.asc(),
            TurtleChatHistoryMessage.message_id.asc(),
        )
    )
    rows = list(result.scalars().all())
    messages = {
        row.message_id: copy.deepcopy(row.payload)
        for row in rows
        if isinstance(row.payload, dict)
    }
    loaded_ids = set(messages)
    for message_id, message in messages.items():
        message["id"] = message_id
        message["childrenIds"] = [
            child_id
            for child_id in message.get("childrenIds", [])
            if child_id in loaded_ids
        ]
    return messages


async def initial_chat_page(
    envelope: dict[str, Any],
    *,
    db: AsyncSession,
) -> dict[str, Any]:
    state = await _fresh_state(envelope, db=db)
    range_end = int(state.current_depth) + 1 if state.message_count else 0
    range_start = max(
        int(state.min_depth),
        range_end - HISTORY_INITIAL_DEPTH_SPAN,
    )
    messages = await _range_messages(
        str(envelope["id"]),
        range_start=range_start,
        range_end=range_end,
        db=db,
    )

    chat = copy.deepcopy(state.shell) if isinstance(state.shell, dict) else {}
    chat["title"] = envelope.get("title") or chat.get("title") or "New Chat"
    history = chat.get("history") if isinstance(chat.get("history"), dict) else {}
    history["messages"] = messages
    history["currentId"] = state.current_message_id
    history["turtlePage"] = _page_metadata(
        state,
        range_start=range_start,
        range_end=range_end,
        span=HISTORY_INITIAL_DEPTH_SPAN,
    )
    chat["history"] = history

    return {
        "id": envelope["id"],
        "user_id": envelope["user_id"],
        "title": envelope.get("title") or "New Chat",
        "chat": chat,
        "updated_at": int(envelope.get("updated_at") or 0),
        "created_at": int(envelope.get("created_at") or 0),
        "share_id": envelope.get("share_id"),
        "archived": bool(envelope.get("archived")),
        "pinned": envelope.get("pinned"),
        "meta": envelope.get("meta") or {},
        "variables": envelope.get("variables") or {},
        "folder_id": envelope.get("folder_id"),
        "tasks": envelope.get("tasks"),
        "summary": envelope.get("summary"),
        "current_message_id": state.current_message_id,
        "context_usage": None,
    }


async def older_chat_range(
    envelope: dict[str, Any],
    *,
    before_depth: int,
    db: AsyncSession,
) -> dict[str, Any]:
    state = await _fresh_state(envelope, db=db)
    range_end = max(
        int(state.min_depth),
        min(int(before_depth), int(state.max_depth) + 1),
    )
    range_start = max(int(state.min_depth), range_end - HISTORY_DEPTH_SPAN)
    messages = await _range_messages(
        str(envelope["id"]),
        range_start=range_start,
        range_end=range_end,
        db=db,
    )
    return {
        "messages": messages,
        "page": _page_metadata(
            state,
            range_start=range_start,
            range_end=range_end,
        ),
    }


def history_index_contract() -> dict[str, Any]:
    """Small test/admin diagnostic without exposing conversation content."""

    return {
        "page_depth_span": HISTORY_DEPTH_SPAN,
        "initial_depth_span": HISTORY_INITIAL_DEPTH_SPAN,
        "range_index": [
            "chat_id",
            "depth",
            "message_id",
        ],
        "pagination": "indexed_half_open_depth_range",
        "uses_offset": False,
        "uses_row_limit": False,
    }
