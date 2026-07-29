"""Focused tests for durable registration and Turnstile configuration."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from .core import AuthSecurityStore
from .service import _validate_verification_payload


class AuthSecurityStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "auth-security.json"
        self.store = AuthSecurityStore(
            self.path,
            master_secret="unit-test-auth-master-secret",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_turnstile_secret_is_encrypted_and_redacted(self):
        self.store.update_admin(
            {
                "registration_enabled": True,
                "turnstile_enabled": True,
                "turnstile_site_key": "1x00000000000000000000AA",
                "turnstile_secret_key": "1x0000000000000000000000000000000AA",
            }
        )
        persisted = self.path.read_text(encoding="utf-8")
        self.assertNotIn("1x0000000000000000000000000000000AA", persisted)
        self.assertEqual(
            self.store.turnstile_secret(),
            "1x0000000000000000000000000000000AA",
        )
        public = self.store.public(admin=True)
        self.assertNotIn("turnstile_secret_key", public)
        self.assertNotIn("secret_key_ciphertext", public)
        self.assertTrue(public["turnstile_secret_key_configured"])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_blank_secret_preserves_existing_ciphertext(self):
        self.store.update_admin(
            {
                "turnstile_site_key": "1x00000000000000000000AA",
                "turnstile_secret_key": "1x0000000000000000000000000000000AA",
            }
        )
        self.store.update_admin(
            {
                "registration_enabled": False,
                "turnstile_enabled": True,
                "turnstile_site_key": "1x00000000000000000000AA",
                "turnstile_secret_key": "",
            }
        )
        self.assertFalse(self.store.public()["registration_enabled"])
        self.assertEqual(
            self.store.turnstile_secret(),
            "1x0000000000000000000000000000000AA",
        )

    def test_registration_switch_survives_reload(self):
        self.store.set_registration_enabled(False)
        reloaded = AuthSecurityStore(
            self.path,
            master_secret="unit-test-auth-master-secret",
        )
        self.assertFalse(reloaded.public()["registration_enabled"])

    def test_maintenance_mode_and_message_survive_reload(self):
        self.store.update_admin(
            {
                "maintenance_enabled": True,
                "maintenance_message": "正在升级模型服务，预计很快恢复。",
            }
        )
        reloaded = AuthSecurityStore(
            self.path,
            master_secret="unit-test-auth-master-secret",
        )
        public = reloaded.public()
        self.assertTrue(public["maintenance_enabled"])
        self.assertEqual(
            public["maintenance_message"],
            "正在升级模型服务，预计很快恢复。",
        )

    def test_maintenance_message_cannot_be_blank(self):
        with self.assertRaisesRegex(ValueError, "维护提示"):
            self.store.update_admin(
                {
                    "maintenance_enabled": True,
                    "maintenance_message": "   ",
                }
            )


class TurnstileResultTests(unittest.TestCase):
    def test_valid_payload_requires_matching_hostname_and_action(self):
        _validate_verification_payload(
            {
                "success": True,
                "hostname": "chat.turtleligpt.com",
                "action": "turtle_signup",
            },
            expected_hostname="chat.turtleligpt.com",
        )

    def test_wrong_hostname_is_rejected(self):
        with self.assertRaises(HTTPException) as denied:
            _validate_verification_payload(
                {
                    "success": True,
                    "hostname": "other.example",
                    "action": "turtle_signup",
                },
                expected_hostname="chat.turtleligpt.com",
            )
        self.assertEqual(denied.exception.status_code, 400)

    def test_wrong_action_is_rejected(self):
        with self.assertRaises(HTTPException) as denied:
            _validate_verification_payload(
                {
                    "success": True,
                    "hostname": "chat.turtleligpt.com",
                    "action": "other_action",
                },
                expected_hostname="chat.turtleligpt.com",
            )
        self.assertEqual(denied.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
