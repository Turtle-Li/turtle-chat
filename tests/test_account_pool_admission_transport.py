import importlib.util
import asyncio
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ACCOUNT_POOL_PATH = (
    PROJECT_ROOT
    / "branding"
    / "open-webui"
    / "turtle_chat"
    / "account_pool.py"
)


class FakeResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return {
            "admission_capacity": 8,
            "provider_admission_capacity": 24,
            "global_admission_capacity": 32,
        }


class FakeSession:
    def __init__(self):
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return FakeResponse()


def load_account_pool_module(monkeypatch, session):
    aiohttp = types.ModuleType("aiohttp")

    class ClientError(Exception):
        pass

    class ClientTimeout:
        def __init__(self, **kwargs):
            self.options = kwargs

    aiohttp.ClientError = ClientError
    aiohttp.ClientTimeout = ClientTimeout
    open_webui = types.ModuleType("open_webui")
    open_webui.__path__ = []
    utils = types.ModuleType("open_webui.utils")
    utils.__path__ = []
    session_pool = types.ModuleType("open_webui.utils.session_pool")

    async def get_session():
        return session

    session_pool.get_session = get_session
    monkeypatch.setitem(sys.modules, "aiohttp", aiohttp)
    monkeypatch.setitem(sys.modules, "open_webui", open_webui)
    monkeypatch.setitem(sys.modules, "open_webui.utils", utils)
    monkeypatch.setitem(sys.modules, "open_webui.utils.session_pool", session_pool)

    spec = importlib.util.spec_from_file_location(
        "turtle_account_pool_transport_test",
        ACCOUNT_POOL_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_capacity_uses_shared_session_and_short_cache(monkeypatch) -> None:
    session = FakeSession()
    module = load_account_pool_module(monkeypatch, session)
    admission = module.AccountPoolAdmission()

    monkeypatch.setenv("OPENAI_API_BASE_URLS", "http://gateway:8000/v1")
    monkeypatch.setenv("OPENAI_API_KEYS", "gateway-key")

    async def scenario():
        first = await admission.limits("gpt-default", "gpt-5-5:instant")
        second = await admission.limits("gpt-default", "gpt-5-5:instant")
        return first, second

    first, second = asyncio.run(scenario())

    assert first == {
        "account_pool": 8,
        "provider": 24,
        "global": 32,
    }
    assert second == first
    assert session.calls == 1
    assert admission.cache_seconds == 5.0
