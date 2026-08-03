from __future__ import annotations

import asyncio
import contextlib
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
    openai_chat_module = importlib.import_module(
        "g4f.Provider.needs_auth.OpenaiChat"
    )
    OpenaiChat = openai_chat_module.OpenaiChat
    OpenaiAccount = importlib.import_module(
        "g4f.Provider.needs_auth.OpenaiAccount"
    ).OpenaiAccount
    RequestConfig = importlib.import_module(
        "g4f.Provider.openai.har_file"
    ).RequestConfig
    payload_has_explicit_rate_limit = (
        openai_chat_module._payload_has_explicit_rate_limit
    )
except ModuleNotFoundError as exc:
    openai_chat_module = None
    OpenaiChat = None
    OpenaiAccount = None
    RequestConfig = None
    payload_has_explicit_rate_limit = None
    RUNTIME_IMPORT_ERROR = exc


requires_runtime = pytest.mark.skipif(
    RUNTIME_IMPORT_ERROR is not None,
    reason="gpt4free runtime dependencies are not installed",
)


class FakeStreamSession:
    instances: list["FakeStreamSession"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.close_count = 0
        self.cookies = FakeCookies()
        self.instances.append(self)

    async def close(self) -> None:
        self.close_count += 1


class FakeCookies:
    def __init__(self) -> None:
        self.clear_count = 0

    def clear(self) -> None:
        self.clear_count += 1


@requires_runtime
def test_openai_account_capture_discards_stale_process_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_config = RequestConfig()
    stale_config.access_token = "expired-token"
    stale_config.cookies = {"stale": "cookie"}
    stale_config.headers = {"authorization": "Bearer expired-token"}
    monkeypatch.setattr(OpenaiAccount, "request_config", stale_config)
    monkeypatch.setattr(OpenaiAccount, "_api_key", "expired-token")
    monkeypatch.setattr(OpenaiAccount, "_headers", stale_config.headers)
    monkeypatch.setattr(OpenaiAccount, "_cookies", stale_config.cookies)
    monkeypatch.setattr(OpenaiAccount, "_expires", 1)
    monkeypatch.setattr(OpenaiAccount, "_turtle_home_warmed_at", 123.0)

    OpenaiAccount.reset_auth_state_for_capture()

    assert OpenaiAccount._api_key is None
    assert OpenaiAccount._headers is None
    assert OpenaiAccount._cookies is None
    assert OpenaiAccount._expires is None
    assert OpenaiAccount.request_config is not stale_config
    assert OpenaiAccount.request_config.access_token is None
    assert OpenaiAccount._turtle_home_warmed_at == 0.0


@requires_runtime
def test_openai_account_forced_browser_capture_bypasses_stale_har(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_config = RequestConfig()
    stale_config.access_token = "revoked-but-unexpired-token"
    monkeypatch.setattr(OpenaiAccount, "request_config", stale_config)
    monkeypatch.setattr(openai_chat_module, "has_nodriver", True)
    browser_calls: list[str | None] = []
    cache_writes: list[tuple[Path, object]] = []

    async def reject_har(*_args, **_kwargs):
        raise AssertionError("forced browser capture must not inspect HAR credentials")

    async def fake_nodriver_auth(cls, proxy: str | None = None) -> None:
        browser_calls.append(proxy)

    def fake_cache_file(cls) -> Path:
        return Path("synthetic-auth.json")

    def fake_write_cache_file(cls, cache_file: Path, auth_result: object) -> None:
        cache_writes.append((cache_file, auth_result))

    monkeypatch.setattr(openai_chat_module, "get_request_config", reject_har)
    monkeypatch.setattr(
        OpenaiAccount,
        "nodriver_auth",
        classmethod(fake_nodriver_auth),
    )
    monkeypatch.setattr(
        OpenaiAccount,
        "get_cache_file",
        classmethod(fake_cache_file),
    )
    monkeypatch.setattr(
        OpenaiAccount,
        "write_cache_file",
        classmethod(fake_write_cache_file),
    )

    async def exercise() -> None:
        await OpenaiAccount.login(
            proxy="http://127.0.0.1:17897",
            force_browser=True,
        )

    asyncio.run(exercise())

    assert browser_calls == ["http://127.0.0.1:17897"]
    assert len(cache_writes) == 1
    assert cache_writes[0][0] == Path("synthetic-auth.json")


@requires_runtime
def test_openai_account_browser_capture_prefers_fresh_session_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePage:
        def __init__(self) -> None:
            self.handler = None
            self.remix_reads = 0

        async def send(self, command):
            if command == "get-cookies":
                return {}
            return None

        def add_handler(self, _event_type, handler) -> None:
            self.handler = handler

        async def reload(self) -> None:
            return None

        async def evaluate(self, expression: str, **_kwargs):
            if expression == "window.navigator.userAgent":
                return "Synthetic Browser"
            if "/api/auth/session" in expression:
                return "fresh-session-token"
            if expression == "JSON.stringify(window.__remixContext)":
                self.remix_reads += 1
                return '{"accessToken":"stale-remix-token"}'
            if "data-build" in expression:
                assert self.handler is not None
                self.handler(
                    SimpleNamespace(
                        request=SimpleNamespace(
                            url=openai_chat_module.backend_url,
                            headers={
                                "Authorization": "Bearer stale-network-token"
                            },
                        )
                    )
                )
                return "synthetic-build"
            raise AssertionError(f"unexpected browser evaluation: {expression}")

        async def close(self) -> None:
            return None

    class FakeBrowser:
        def __init__(self, page: FakePage) -> None:
            self.page = page

        async def get(self, _url: str, *, new_tab: bool):
            assert new_tab is True
            return self.page

    page = FakePage()

    @contextlib.asynccontextmanager
    async def fake_nodriver_session(*, proxy: str | None = None):
        assert proxy == "http://127.0.0.1:17897"
        yield FakeBrowser(page)

    async def no_sleep(_seconds: float) -> None:
        return None

    selected_tokens: list[str | None] = []

    def fake_set_api_key(cls, api_key: str | None) -> bool:
        selected_tokens.append(api_key)
        cls._api_key = api_key
        return bool(api_key)

    fake_nodriver = SimpleNamespace(
        cdp=SimpleNamespace(
            network=SimpleNamespace(
                RequestWillBeSent=object,
                enable=lambda: "network-enable",
            )
        )
    )
    monkeypatch.setattr(
        openai_chat_module,
        "get_nodriver_session",
        fake_nodriver_session,
    )
    monkeypatch.setattr(openai_chat_module, "nodriver", fake_nodriver)
    monkeypatch.setattr(
        openai_chat_module,
        "get_cookies",
        lambda _urls: "get-cookies",
    )
    monkeypatch.setattr(openai_chat_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        OpenaiAccount,
        "_set_api_key",
        classmethod(fake_set_api_key),
    )
    monkeypatch.setattr(OpenaiAccount, "request_config", RequestConfig())
    monkeypatch.setattr(OpenaiAccount, "_api_key", None)
    monkeypatch.setattr(OpenaiAccount, "_headers", None)
    monkeypatch.setattr(OpenaiAccount, "_cookies", None)

    asyncio.run(
        OpenaiAccount.nodriver_auth(proxy="http://127.0.0.1:17897")
    )

    assert page.remix_reads == 0
    assert selected_tokens[-1] == "fresh-session-token"
    assert OpenaiAccount._api_key == "fresh-session-token"


@requires_runtime
def test_openai_account_leases_reusable_sessions_without_concurrent_sharing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openai_chat_module, "StreamSession", FakeStreamSession)
    OpenaiChat._turtle_http_session_pools = {}
    OpenaiChat._turtle_http_session_counts = {}
    OpenaiChat._turtle_http_session_lock = None
    FakeStreamSession.instances.clear()

    async def exercise() -> None:
        async with OpenaiChat._persistent_session(
            proxy=None,
            timeout=360,
            curl_infos=[],
        ) as first:
            assert first.cookies.clear_count == 0
            async with OpenaiChat._persistent_session(
                proxy=None,
                timeout=360,
                curl_infos=[],
            ) as parallel:
                assert parallel is not first

        async with OpenaiChat._persistent_session(
            proxy=None,
            timeout=360,
            curl_infos=[],
        ) as reused:
            assert reused is first

        assert len(FakeStreamSession.instances) == 2
        assert all(
            session.kwargs["max_clients"] == 4
            for session in FakeStreamSession.instances
        )
        assert first.cookies.clear_count == 0
        assert first.close_count == 0

        with pytest.raises(RuntimeError, match="discard this lease"):
            async with OpenaiChat._persistent_session(
                proxy="http://127.0.0.1:17897",
                timeout=360,
                curl_infos=[],
            ) as failed:
                raise RuntimeError("discard this lease")

        assert failed.close_count == 1
        assert (
            OpenaiChat._turtle_http_session_counts[
                ("http://127.0.0.1:17897", 360)
            ]
            == 0
        )

    asyncio.run(exercise())
    OpenaiChat._turtle_http_session_pools = {}
    OpenaiChat._turtle_http_session_counts = {}
    OpenaiChat._turtle_http_session_lock = None


@requires_runtime
def test_openai_account_capture_quota_bypasses_stale_pooled_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeQuotaResponse:
        def __init__(self, status: int) -> None:
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
            return None

        async def json(self) -> dict[str, str]:
            return {"id": "synthetic", "name": "Synthetic Account"}

    class FakeQuotaSession:
        def __init__(self, status: int) -> None:
            self.status = status

        def get(self, _url: str, *, headers: dict):
            assert headers == {"authorization": "Bearer fresh-token"}
            return FakeQuotaResponse(self.status)

    class FreshQuotaSession(FakeQuotaSession):
        instances: list["FreshQuotaSession"] = []

        def __init__(self, **kwargs) -> None:
            super().__init__(200)
            self.kwargs = kwargs
            self.instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
            return None

    persistent_calls: list[dict] = []

    @contextlib.asynccontextmanager
    async def stale_persistent_session(cls, **kwargs):
        persistent_calls.append(kwargs)
        yield FakeQuotaSession(401)

    async def fake_warm_home(cls, _session, _auth) -> bool:
        return False

    async def fake_raise_for_status(response) -> None:
        if response.status == 401:
            raise openai_chat_module.MissingAuthError("synthetic stale session")

    monkeypatch.setattr(openai_chat_module, "StreamSession", FreshQuotaSession)
    monkeypatch.setattr(
        OpenaiAccount,
        "_persistent_session",
        classmethod(stale_persistent_session),
    )
    monkeypatch.setattr(
        OpenaiAccount,
        "get_auth_result",
        classmethod(
            lambda cls: SimpleNamespace(
                api_key="fresh-token",
                cookies={},
                headers={},
            )
        ),
    )
    monkeypatch.setattr(
        OpenaiAccount,
        "_set_api_key",
        classmethod(lambda cls, _api_key: True),
    )
    monkeypatch.setattr(
        OpenaiAccount,
        "_warm_home",
        classmethod(fake_warm_home),
    )
    monkeypatch.setattr(
        OpenaiAccount,
        "_update_request_args",
        classmethod(lambda cls, _auth, _session: None),
    )
    monkeypatch.setattr(openai_chat_module, "raise_for_status", fake_raise_for_status)
    monkeypatch.setattr(
        OpenaiAccount,
        "_headers",
        {"authorization": "Bearer fresh-token"},
    )
    FreshQuotaSession.instances.clear()

    async def exercise() -> None:
        fresh = await OpenaiAccount.get_quota(fresh_session=True)
        assert fresh == {"id": "synthetic", "name": "Synthetic Account"}
        with pytest.raises(
            openai_chat_module.MissingAuthError,
            match="synthetic stale session",
        ):
            await OpenaiAccount.get_quota()

    asyncio.run(exercise())

    assert len(FreshQuotaSession.instances) == 1
    assert FreshQuotaSession.instances[0].kwargs["max_clients"] == 1
    assert len(persistent_calls) == 1
    assert persistent_calls[0]["proxy"] is None
    assert persistent_calls[0]["timeout"] == 360
    assert (
        persistent_calls[0]["curl_infos"]
        == FreshQuotaSession.instances[0].kwargs["curl_infos"]
    )


@requires_runtime
def test_openai_account_capture_serializes_ordinary_quota(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import httpx
    from fastapi import FastAPI

    g4f_api_module = importlib.import_module("g4f.api")
    api_app = FastAPI()
    api = g4f_api_module.Api(api_app)
    api.register_routes()

    ordinary_started = asyncio.Event()
    ordinary_release = asyncio.Event()
    capture_login_started = asyncio.Event()
    capture_login_release = asyncio.Event()
    ordinary_calls = 0
    quota_modes: list[bool] = []
    cache_file = tmp_path / "auth_OpenaiChat.json"

    async def fake_quota(cls, fresh_session: bool = False, **_kwargs):
        nonlocal ordinary_calls
        quota_modes.append(fresh_session)
        if fresh_session:
            assert cls._api_key == "fresh-token"
            return {"id": "synthetic", "name": "Synthetic Account"}
        ordinary_calls += 1
        ordinary_started.set()
        await ordinary_release.wait()
        if ordinary_calls == 1:
            cls._api_key = "stale-token"
        return {"id": "stale", "name": "Stale Account"}

    async def fake_login(cls, **kwargs) -> None:
        assert kwargs == {"force_browser": True}
        capture_login_started.set()
        await capture_login_release.wait()
        cls._api_key = "fresh-token"
        cache_file.write_text("{}", encoding="utf-8")
        cache_file.chmod(0o600)

    def fake_reset(cls) -> None:
        cls._api_key = None

    monkeypatch.setenv("GPT4FREE_AUTH_DIR", str(tmp_path))
    monkeypatch.setattr(
        g4f_api_module.AbstractClientFactory,
        "create_provider",
        staticmethod(lambda *_args, **_kwargs: OpenaiAccount),
    )
    monkeypatch.setattr(
        importlib.import_module("g4f.cookies"),
        "set_cookies_dir",
        lambda configured: None,
    )
    monkeypatch.setattr(
        OpenaiAccount,
        "get_cache_file",
        classmethod(lambda cls: cache_file),
    )
    monkeypatch.setattr(
        OpenaiAccount,
        "get_auth_result",
        classmethod(lambda cls: SimpleNamespace()),
    )
    monkeypatch.setattr(
        OpenaiAccount,
        "reset_auth_state_for_capture",
        classmethod(fake_reset),
    )
    monkeypatch.setattr(
        OpenaiAccount,
        "login",
        classmethod(fake_login),
    )
    monkeypatch.setattr(
        OpenaiAccount,
        "get_quota",
        classmethod(fake_quota),
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=api_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            ordinary = asyncio.create_task(
                client.get("/api/OpenaiAccount/quota")
            )
            await ordinary_started.wait()

            capture = asyncio.create_task(
                client.post("/api/OpenaiAccount/auth/capture")
            )
            await asyncio.sleep(0)
            assert not capture_login_started.is_set()

            blocked_while_draining = await client.get(
                "/api/OpenaiAccount/quota"
            )
            assert blocked_while_draining.status_code == 409
            assert ordinary_calls == 1

            ordinary_release.set()
            assert (await ordinary).status_code == 200
            await capture_login_started.wait()

            blocked_during_capture = await client.get(
                "/api/OpenaiAccount/quota"
            )
            assert blocked_during_capture.status_code == 409
            assert ordinary_calls == 1

            capture_login_release.set()
            captured = await capture
            assert captured.status_code == 200
            assert captured.json() == {"ok": True}

            after_capture = await client.get("/api/OpenaiAccount/quota")
            assert after_capture.status_code == 200

    asyncio.run(exercise())

    assert quota_modes == [False, True, False]
    assert ordinary_calls == 2
    assert OpenaiAccount._api_key == "fresh-token"
    assert api.turtle_openai_auth_capture_active is False
    assert api.turtle_openai_auth_operation_lock.locked() is False


@requires_runtime
def test_openai_account_recognizes_wrapped_explicit_rate_limits() -> None:
    assert payload_has_explicit_rate_limit(
        {"error": {"message": "You've hit your limit. Try again later."}}
    )
    assert payload_has_explicit_rate_limit(
        {"detail": {"code": "rate_limit_exceeded"}}
    )
    assert payload_has_explicit_rate_limit(
        {"error": {"type": "quota_exhausted"}}
    )
    assert not payload_has_explicit_rate_limit(
        {"error": {"message": "Temporary upstream server error"}}
    )


@requires_runtime
def test_openai_account_normalizes_streamed_quota_errors() -> None:
    class DummyConversation:
        p = None

    async def exercise() -> None:
        with pytest.raises(
            openai_chat_module.RateLimitError,
            match="account rate limit reached",
        ):
            await anext(
                OpenaiChat.iter_messages_line(
                    None,
                    None,
                    b'data: {"error":"You have hit your limit. Try again later."}',
                    DummyConversation(),
                    None,
                    None,
                )
            )

    asyncio.run(exercise())


@requires_runtime
def test_openai_account_continues_handoff_after_generic_sse_error() -> None:
    class DummyConversation:
        p = None
        task = None
        handoff_topic = "conversation-turn-safe-topic"

    async def exercise() -> None:
        iterator = OpenaiChat.iter_messages_line(
            None,
            None,
            b'data: {"error":"Error in message stream"}',
            DummyConversation(),
            None,
            None,
        )
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)

    asyncio.run(exercise())
