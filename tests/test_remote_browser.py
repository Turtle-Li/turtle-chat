from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

import httpx

from chatgpt_web_gateway.remote_browser_broker import (
    PUBLIC_PREFIX,
    RemoteBrowserBrokerSettings,
    create_remote_browser_broker_app,
)
from chatgpt_web_gateway.remote_browser_sessions import (
    RemoteBrowserSessionError,
    consume_connection,
    exchange_pending_session,
    issue_pending_session,
    revoke_account_sessions,
)


class RemoteBrowserSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root / ".runtime"
        self.runtime.mkdir(mode=0o700)
        self.store = self.runtime / "remote-browser-sessions.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_pending_and_connection_capabilities_are_one_time_and_hashed(self) -> None:
        token, expires_at = issue_pending_session(
            self.store,
            account_id="acct-one",
            ttl_seconds=600,
        )
        self.assertGreater(expires_at, int(time.time()))
        self.assertNotIn(token, self.store.read_text(encoding="utf-8"))
        connection, account_id, exchanged_expiry = exchange_pending_session(
            self.store,
            raw_token=token,
        )
        self.assertEqual(account_id, "acct-one")
        self.assertEqual(exchanged_expiry, expires_at)
        self.assertNotIn(connection, self.store.read_text(encoding="utf-8"))
        with self.assertRaises(RemoteBrowserSessionError):
            exchange_pending_session(self.store, raw_token=token)
        consumed_account, consumed_expiry = consume_connection(
            self.store,
            raw_cookie=connection,
        )
        self.assertEqual(consumed_account, "acct-one")
        self.assertEqual(consumed_expiry, expires_at)
        with self.assertRaises(RemoteBrowserSessionError):
            consume_connection(self.store, raw_cookie=connection)
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o600)

    def test_revoke_removes_pending_account_session(self) -> None:
        token, _ = issue_pending_session(
            self.store,
            account_id="acct-one",
            ttl_seconds=600,
        )
        revoke_account_sessions(self.store, account_id="acct-one")
        with self.assertRaises(RemoteBrowserSessionError):
            exchange_pending_session(self.store, raw_token=token)

    async def test_broker_exchanges_fragment_token_without_putting_it_in_html(self) -> None:
        novnc = self.root / "novnc"
        novnc.mkdir()
        (novnc / "vnc.html").write_text("<!doctype html><title>noVNC</title>", encoding="utf-8")
        settings = RemoteBrowserBrokerSettings(
            bind_host="127.0.0.1",
            port=36080,
            session_store=self.store,
            novnc_root=novnc,
            vnc_host="127.0.0.1",
            vnc_port=35900,
        )
        token, _ = issue_pending_session(
            self.store,
            account_id="acct-one",
            ttl_seconds=600,
        )
        app = create_remote_browser_broker_app(settings)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://chat.example.test",
        ) as client:
            connect = await client.get(f"{PUBLIC_PREFIX}/connect")
            self.assertEqual(connect.status_code, 200)
            self.assertNotIn(token, connect.text)
            exchanged = await client.post(
                f"{PUBLIC_PREFIX}/session",
                json={"token": token},
            )
            self.assertEqual(exchanged.status_code, 200)
            self.assertIn("turtle_remote_login=", exchanged.headers["set-cookie"])
            repeated = await client.post(
                f"{PUBLIC_PREFIX}/session",
                json={"token": token},
            )
            self.assertEqual(repeated.status_code, 410)


if __name__ == "__main__":
    unittest.main()
