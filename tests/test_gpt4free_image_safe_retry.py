from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
G4F_ROOT = ROOT / ".runtime" / "gpt4free-src"
sys.path.insert(0, str(G4F_ROOT))
RUNTIME_IMPORT_ERROR = None
try:
    client_module = importlib.import_module("g4f.client")
    Images = client_module.Images
    MediaResponse = client_module.MediaResponse
    PreTaskMediaError = importlib.import_module(
        "g4f.errors"
    ).PreTaskMediaError
except ModuleNotFoundError as exc:
    client_module = None
    Images = None
    MediaResponse = None
    PreTaskMediaError = None
    RUNTIME_IMPORT_ERROR = exc


requires_runtime = pytest.mark.skipif(
    RUNTIME_IMPORT_ERROR is not None,
    reason="gpt4free runtime dependencies are not installed",
)


class FakeOpenaiAccount:
    quotas: list[dict | None] = []
    attempts = 0
    fail_attempts = 1
    error_message = "Error in message stream"
    error_type: type[Exception] = RuntimeError
    task_lock: asyncio.Lock | None = None

    @classmethod
    def reset(
        cls,
        quotas: list[dict | None],
        *,
        fail_attempts: int = 1,
        error_message: str = "Error in message stream",
        error_type: type[Exception] = RuntimeError,
    ) -> None:
        cls.quotas = list(quotas)
        cls.attempts = 0
        cls.fail_attempts = fail_attempts
        cls.error_message = error_message
        cls.error_type = error_type
        cls.task_lock = None

    @classmethod
    def _image_task_lock(cls) -> asyncio.Lock:
        if cls.task_lock is None:
            cls.task_lock = asyncio.Lock()
        return cls.task_lock

    @classmethod
    async def get_image_quota(cls, **_kwargs) -> dict | None:
        return cls.quotas.pop(0)

    @classmethod
    async def create_async_generator(cls, *_args, **_kwargs):
        cls.attempts += 1
        if cls.attempts <= cls.fail_attempts:
            raise cls.error_type(cls.error_message)
        yield MediaResponse(
            ["https://example.invalid/generated.png"],
            "generated",
            {},
        )


async def generate() -> MediaResponse:
    images = Images(SimpleNamespace())
    return await images._generate_image_response(
        FakeOpenaiAccount,
        "OpenaiAccount",
        "gpt-image",
        "generate one image",
    )


@requires_runtime
def test_retries_once_only_after_two_unchanged_official_quota_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeOpenaiAccount.reset(
        [
            {"remaining": 10, "blocked": False},
            {"remaining": 10, "blocked": False},
            {"remaining": 10, "blocked": False},
            {"remaining": 9, "blocked": False},
        ]
    )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(client_module.asyncio, "sleep", no_sleep)
    response = asyncio.run(generate())

    assert FakeOpenaiAccount.attempts == 2
    assert response.urls == ["https://example.invalid/generated.png"]
    assert response.options["turtle_usage"] == {
        "image_units": 1,
        "source": "official_remaining_delta",
        "remaining": 9,
        "reset_after": None,
    }


@requires_runtime
def test_does_not_retry_when_failed_stream_already_consumed_allowance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeOpenaiAccount.reset(
        [
            {"remaining": 10, "blocked": False},
            {"remaining": 9, "blocked": False},
        ]
    )

    async def unexpected_sleep(_seconds: float) -> None:
        pytest.fail("consumed image task must not enter retry grace period")

    monkeypatch.setattr(client_module.asyncio, "sleep", unexpected_sleep)
    response = asyncio.run(generate())

    assert FakeOpenaiAccount.attempts == 1
    assert response.urls == []
    assert response.options["turtle_usage"]["source"] == "official_remaining_delta"
    assert response.options["turtle_usage"]["image_units"] == 1


@requires_runtime
def test_fails_closed_when_retry_confirmation_quota_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeOpenaiAccount.reset(
        [
            {"remaining": 10, "blocked": False},
            {"remaining": 10, "blocked": False},
            None,
        ]
    )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(client_module.asyncio, "sleep", no_sleep)
    with pytest.raises(RuntimeError, match="Error in message stream"):
        asyncio.run(generate())

    assert FakeOpenaiAccount.attempts == 1


@requires_runtime
def test_pre_task_upload_failure_skips_post_task_quota_and_generation_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeOpenaiAccount.reset(
        [{"remaining": 10, "blocked": False}],
        error_message="image input upload failed before task submission",
        error_type=PreTaskMediaError,
    )

    async def unexpected_sleep(_seconds: float) -> None:
        pytest.fail("pre-task upload failure must not enter task retry grace")

    monkeypatch.setattr(client_module.asyncio, "sleep", unexpected_sleep)
    with pytest.raises(
        PreTaskMediaError,
        match="image input upload failed before task submission",
    ):
        asyncio.run(generate())

    assert FakeOpenaiAccount.attempts == 1
    assert FakeOpenaiAccount.quotas == []
