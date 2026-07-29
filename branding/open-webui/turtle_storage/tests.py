"""Focused unit tests runnable inside the pinned Open WebUI image."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.engine import make_url

from .core import ConfigStore, GIB, StorageConfigurationError, object_key
from .media import (
    BASE64_MEDIA_REFERENCE_RE,
    FILE_REFERENCE_RE,
    MEDIA_REFERENCE_RE,
    _cloud_path_from_files_cdn_url,
    bind_message_image_file_ids,
    carry_forward_message_images,
    get_presigned_model_image_source,
    requested_image_count,
    visible_stream_output,
)
from .provider import TurtleStorageProvider
from .pump import MediaPumpClient, MediaPumpError
from ..turtle_database import (
    connect_postgres,
    dispose_postgres_engine,
    normalized_postgres_url,
    quote_identifier,
    runtime_database_url,
)


class ConfigStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "storage.json"
        self.store = ConfigStore(self.path, master_secret="unit-test-master-secret")

    def tearDown(self):
        self.temp.cleanup()

    def test_credentials_are_encrypted_and_redacted(self):
        self.store.update_admin(
            {
                "provider": "cos",
                "cos": {
                    "endpoint_url": "https://cos.ap-tokyo.myqcloud.com",
                    "region": "ap-tokyo",
                    "bucket": "example-1250000000",
                    "secret_id": "unit-secret-id",
                    "secret_key": "unit-secret-key",
                },
            }
        )
        persisted = self.path.read_text(encoding="utf-8")
        self.assertNotIn("unit-secret-id", persisted)
        self.assertNotIn("unit-secret-key", persisted)
        self.assertEqual(self.store.credentials(), ("unit-secret-id", "unit-secret-key"))
        public = self.store.public(admin=True)
        self.assertNotIn("secret_id", public["cos"])
        self.assertNotIn("secret_key", public["cos"])
        self.assertTrue(public["cos"]["configured"])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_cdn_type_a_keys_are_encrypted_redacted_and_require_two_domains(self):
        self.store.update_admin(
            {
                "cdn": {
                    "enabled": True,
                    "files_base_url": "https://files.chat.totools.cn",
                    "images_base_url": "https://img.chat.totools.cn",
                    "files_auth_key": "FilesKey123",
                    "images_auth_key": "ImagesKey456",
                    "auth_ttl_seconds": 600,
                }
            }
        )
        persisted = self.path.read_text(encoding="utf-8")
        self.assertNotIn("FilesKey123", persisted)
        self.assertNotIn("ImagesKey456", persisted)
        public = self.store.public(admin=True)["cdn"]
        self.assertTrue(public["enabled"])
        self.assertTrue(public["files_ready"])
        self.assertTrue(public["images_ready"])
        self.assertNotIn("files_auth_key", public)
        self.assertNotIn("images_auth_key", public)
        self.assertEqual(self.store.cdn_auth_key("files"), "FilesKey123")
        self.assertEqual(self.store.cdn_auth_key("images"), "ImagesKey456")

        with self.assertRaises(StorageConfigurationError):
            self.store.update_admin(
                {
                    "cdn": {
                        "files_base_url": "https://img.chat.totools.cn",
                    }
                }
            )

    def test_user_quota_persists(self):
        self.store.set_user_quota("immutable-user", "friend", 7 * GIB)
        self.assertEqual(self.store.quota_for_user("immutable-user")["quota_bytes"], 7 * GIB)
        reloaded = ConfigStore(self.path, master_secret="unit-test-master-secret")
        self.assertEqual(reloaded.quota_for_user("immutable-user")["tier"], "friend")

    def test_custom_membership_tiers_survive_reload(self):
        self.store.update_admin(
            {"quota": {"default_bytes": 3 * GIB, "tiers": {"free": 3 * GIB, "family": 12 * GIB}}}
        )
        self.store.set_user_quota("family-user", "family", None)
        reloaded = ConfigStore(self.path, master_secret="unit-test-master-secret")
        self.assertEqual(reloaded.quota_for_user("family-user")["quota_bytes"], 12 * GIB)

    def test_insecure_public_cos_endpoint_is_rejected(self):
        with self.assertRaises(StorageConfigurationError):
            self.store.update_admin(
                {"provider": "local", "cos": {"endpoint_url": "http://cos.example.com"}}
            )
        with self.assertRaises(StorageConfigurationError):
            self.store.update_admin(
                {
                    "provider": "cos",
                    "cos": {
                        "endpoint_url": "https://cos.ap-tokyo.myqcloud.com",
                        "region": "ap-tokyo",
                        "bucket": "example-1250000000",
                        "prefix": "",
                        "secret_id": "unit-secret-id",
                        "secret_key": "unit-secret-key",
                    },
                }
            )


@unittest.skipUnless(
    os.getenv("TURTLE_RUN_POSTGRES_TESTS") == "1",
    "PostgreSQL integration test is opt-in",
)
class PostgresConfigStoreIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.database_url = runtime_database_url()
        self.schema = f"turtle_storage_test_{uuid.uuid4().hex}"
        with connect_postgres(self.database_url) as connection:
            connection.execute(f"CREATE SCHEMA {quote_identifier(self.schema)}")
        parsed_url = make_url(normalized_postgres_url(self.database_url)).update_query_dict(
            {"options": f"-csearch_path={self.schema}"}
        )
        self.test_database_url = parsed_url.render_as_string(hide_password=False)
        self.user_id = f"postgres-storage-test-{uuid.uuid4()}"
        try:
            self.store = ConfigStore(
                master_secret="postgres-integration-test-master-secret",
                database_url=self.test_database_url,
            )
        except Exception:
            with connect_postgres(self.database_url) as connection:
                connection.execute(f"DROP SCHEMA {quote_identifier(self.schema)} CASCADE")
            raise

    def tearDown(self):
        dispose_postgres_engine(self.test_database_url)
        with connect_postgres(self.database_url) as connection:
            connection.execute(f"DROP SCHEMA {quote_identifier(self.schema)} CASCADE")

    def test_config_and_user_quota_are_normalized_in_postgres(self):
        self.store.update_admin(
            {
                "quota": {
                    "default_bytes": 3 * GIB,
                    "tiers": {"free": 3 * GIB, "family": 12 * GIB},
                }
            }
        )
        self.store.set_user_quota(self.user_id, "family", 9 * GIB)

        reloaded = ConfigStore(
            master_secret="postgres-integration-test-master-secret",
            database_url=self.test_database_url,
        )
        self.assertEqual(reloaded.quota_for_user(self.user_id)["quota_bytes"], 9 * GIB)
        with reloaded._connect() as connection:
            payload = connection.execute(
                "SELECT payload FROM turtle_storage_config WHERE id = 1"
            ).fetchone()[0]
            assignment = connection.execute(
                """
                SELECT tier, quota_bytes
                  FROM turtle_storage_user_quota
                 WHERE user_id = ?
                """,
                (self.user_id,),
            ).fetchone()
        self.assertEqual(payload["quota"]["users"], {})
        self.assertEqual(assignment["tier"], "family")
        self.assertEqual(assignment["quota_bytes"], 9 * GIB)


class ProviderTests(unittest.TestCase):
    def test_object_key_is_user_isolated(self):
        first = object_key("turtle", "user-a", "file-1", "photo.png")
        second = object_key("turtle", "user-b", "file-1", "photo.png")
        self.assertEqual(first, "turtle/files/users/user-a/file-1_photo.png")
        self.assertNotEqual(first, second)

    def test_local_provider_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ConfigStore(Path(temp) / "config.json", master_secret="test-secret")
            provider = TurtleStorageProvider(store=store, upload_dir=Path(temp) / "uploads")
            with patch("open_webui.turtle_storage.provider.strict_media_mode", return_value=False):
                contents, path = provider.upload_file(
                    io.BytesIO(b"turtle"),
                    "id_file.txt",
                    {"OpenWebUI-User-Id": "user", "OpenWebUI-File-Id": "id"},
                )
                self.assertEqual(contents, b"turtle")
                self.assertEqual(Path(provider.get_file(path)).read_bytes(), b"turtle")
                provider.delete_file(path)
            self.assertFalse(Path(path).exists())

    def test_strict_mode_rejects_provider_body_io_before_reading(self):
        class ExplodingFile:
            def read(self):
                raise AssertionError("strict mode must reject before reading bytes")

        provider = TurtleStorageProvider()
        with self.assertRaises(StorageConfigurationError):
            provider.upload_file(ExplodingFile(), "blocked.png", {})
        with self.assertRaises(StorageConfigurationError):
            provider.get_file("s3://bucket/blocked.png")

    def test_connection_checks_only_the_configured_prefix(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ConfigStore(Path(temp) / "config.json", master_secret="test-secret")
            store.update_admin(
                {
                    "provider": "cos",
                    "cos": {
                        "endpoint_url": "https://cos.ap-shanghai.myqcloud.com",
                        "region": "ap-shanghai",
                        "bucket": "example-1250000000",
                        "prefix": "turtle-gpt",
                        "secret_id": "unit-secret-id",
                        "secret_key": "unit-secret-key",
                    },
                }
            )
            provider = TurtleStorageProvider(store=store, upload_dir=Path(temp) / "uploads")
            calls = []
            client = SimpleNamespace(list_objects_v2=lambda **kwargs: calls.append(kwargs))
            with patch.object(provider, "_client", return_value=client):
                provider.test_connection()
            self.assertEqual(
                calls,
                [
                    {
                        "Bucket": "example-1250000000",
                        "Prefix": "turtle-gpt/",
                        "MaxKeys": 1,
                    }
                ],
            )

    def test_static_thumbnail_uses_a_separate_cdn_namespace(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ConfigStore(Path(temp) / "config.json", master_secret="test-secret")
            store.update_admin(
                {
                    "provider": "cos",
                    "cos": {
                        "endpoint_url": "https://cos.ap-shanghai.myqcloud.com",
                        "region": "ap-shanghai",
                        "bucket": "example-1250000000",
                        "prefix": "turtle",
                        "secret_id": "unit-secret-id",
                        "secret_key": "unit-secret-key",
                    },
                }
            )
            provider = TurtleStorageProvider(store=store, upload_dir=Path(temp) / "uploads")
            self.assertEqual(
                provider.thumbnail_path(
                    "s3://example-1250000000/turtle/files/users/user-a/file_photo.png"
                ),
                "s3://example-1250000000/turtle/thumbnails/users/user-a/"
                "file_photo.png.thumbnail.webp",
            )

            calls = []
            client = SimpleNamespace(delete_object=lambda **kwargs: calls.append(kwargs))
            with patch.object(provider, "_client", return_value=client):
                provider.delete_file(
                    "s3://example-1250000000/turtle/files/users/user-a/file_photo.png"
                )
            self.assertEqual(
                [call["Key"] for call in calls],
                [
                    "turtle/thumbnails/users/user-a/file_photo.png.thumbnail.webp",
                    "turtle/files/users/user-a/file_photo.png",
                ],
            )

    def test_type_a_download_urls_use_separate_domains_and_legacy_falls_back(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ConfigStore(Path(temp) / "config.json", master_secret="test-secret")
            store.update_admin(
                {
                    "provider": "cos",
                    "cos": {
                        "endpoint_url": "https://cos.ap-shanghai.myqcloud.com",
                        "region": "ap-shanghai",
                        "bucket": "example-1250000000",
                        "prefix": "turtle",
                        "secret_id": "unit-secret-id",
                        "secret_key": "unit-secret-key",
                    },
                    "cdn": {
                        "enabled": True,
                        "files_base_url": "https://files.chat.totools.cn",
                        "images_base_url": "https://img.chat.totools.cn",
                        "files_auth_key": "FilesKey123",
                        "images_auth_key": "ImagesKey456",
                        "auth_ttl_seconds": 600,
                    },
                }
            )
            provider = TurtleStorageProvider(store=store, upload_dir=Path(temp) / "uploads")
            with (
                patch("open_webui.turtle_storage.provider.time.time", return_value=1_700_000_000),
                patch(
                    "open_webui.turtle_storage.provider.secrets.token_hex",
                    return_value="0123456789abcdef",
                ),
            ):
                files_url = provider.presign_download(
                    "s3://example-1250000000/turtle/files/users/user-a/file_photo.png"
                )
                images_url = provider.presign_download(
                    "s3://example-1250000000/turtle/thumbnails/users/user-a/"
                    "file_photo.png.thumbnail.webp"
                )
            self.assertTrue(
                files_url.startswith(
                    "https://files.chat.totools.cn/turtle/files/users/user-a/file_photo.png?sign="
                )
            )
            self.assertTrue(
                images_url.startswith(
                    "https://img.chat.totools.cn/turtle/thumbnails/users/user-a/"
                    "file_photo.png.thumbnail.webp?sign="
                )
            )
            files_digest = hashlib.md5(
                (
                    "/turtle/files/users/user-a/file_photo.png-1700000000-"
                    "0123456789abcdef-0-FilesKey123"
                ).encode("utf-8")
            ).hexdigest()
            self.assertEqual(
                files_url.rsplit("=", 1)[-1],
                f"1700000000-0123456789abcdef-0-{files_digest}",
            )
            self.assertEqual(
                provider.download_url_ttl(
                    "s3://example-1250000000/turtle/files/users/user-a/file_photo.png"
                ),
                600,
            )

            client = SimpleNamespace(
                generate_presigned_url=lambda *args, **kwargs: "https://cos.example/signed"
            )
            with patch.object(provider, "_client", return_value=client):
                legacy_url = provider.presign_download(
                    "s3://example-1250000000/turtle/users/user-a/legacy_photo.png"
                )
                attachment_url = provider.presign_download(
                    "s3://example-1250000000/turtle/files/users/user-a/file_photo.png",
                    filename="photo.png",
                    attachment=True,
                )
                model_input_url = provider.presign_download(
                    "s3://example-1250000000/turtle/files/users/user-a/file_photo.png",
                    use_cdn=False,
                )
            self.assertEqual(legacy_url, "https://cos.example/signed")
            self.assertEqual(attachment_url, "https://cos.example/signed")
            self.assertEqual(model_input_url, "https://cos.example/signed")

    def test_legacy_static_thumbnail_path_remains_readable_and_deletable(self):
        provider = TurtleStorageProvider()
        legacy = "s3://example-1250000000/turtle/users/user-a/file_photo.png"
        self.assertEqual(
            provider.thumbnail_path(legacy),
            f"{legacy}.thumbnail.webp",
        )

        calls = []
        client = SimpleNamespace(delete_object=lambda **kwargs: calls.append(kwargs))
        with patch.object(provider, "_client", return_value=client):
            provider.delete_file(legacy)
        self.assertEqual(
            [call["Key"] for call in calls],
            [
                "turtle/users/user-a/file_photo.png.thumbnail.webp",
                "turtle/users/user-a/file_photo.png",
            ],
        )


class CursorTests(unittest.TestCase):
    def test_cursor_round_trip_and_invalid_input(self):
        from fastapi import HTTPException

        from .router import _decode_cursor, _encode_cursor

        cursor = _encode_cursor(SimpleNamespace(created_at=1_700_000_000, id="file-id"))
        self.assertEqual(_decode_cursor(cursor), (1_700_000_000, "file-id"))
        with self.assertRaises(HTTPException) as invalid:
            _decode_cursor("not-a-valid-cursor!")
        self.assertEqual(invalid.exception.status_code, 400)


class MediaPumpClientTests(unittest.TestCase):
    def test_control_requests_use_an_explicit_non_browser_identity(self):
        client = MediaPumpClient()
        client.secret = "unit-test-pump-secret-that-is-at-least-thirty-two-characters"

        headers = client._headers("/v1/probe", b"{}")

        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(headers["User-Agent"], "turtle-media-pump-control/1.0")
        self.assertNotIn("Authorization", headers)

    def test_model_source_envelope_is_short_lived_and_uses_stable_opaque_id(self):
        client = MediaPumpClient()
        client.base_url = "https://pump.example"
        client.secret = "unit-test-pump-secret-that-is-at-least-thirty-two-characters"

        with patch("open_webui.turtle_storage.pump.time.time", return_value=1_700_000_000):
            first = client.seal_model_source(
                primary_url="https://files.chat.totools.cn/turtle/files/users/u/a.png?sign=one",
                fallback_url="https://bucket.cos.ap-shanghai.myqcloud.com/turtle/files/users/u/a.png?q=one",
                source_key="file-id\0s3://bucket/turtle/files/users/u/a.png",
                ttl_seconds=600,
            )
            second = client.seal_model_source(
                primary_url="https://files.chat.totools.cn/turtle/files/users/u/a.png?sign=two",
                fallback_url="https://bucket.cos.ap-shanghai.myqcloud.com/turtle/files/users/u/a.png?q=two",
                source_key="file-id\0s3://bucket/turtle/files/users/u/a.png",
                ttl_seconds=600,
            )

        def payload(token):
            encoded = token.split(".", 1)[0]
            return json.loads(
                base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            )

        first_payload = payload(first)
        second_payload = payload(second)
        self.assertEqual(first_payload["exp"], 1_700_000_600)
        self.assertEqual(first_payload["id"], second_payload["id"])
        self.assertEqual(len(first_payload["id"]), 64)
        self.assertNotIn("file-id", first)
        self.assertTrue(first_payload["primary_url"].startswith("https://files.chat.totools.cn/"))
        self.assertTrue(first_payload["fallback_url"].startswith("https://bucket.cos."))

        with self.assertRaises(MediaPumpError):
            client.seal_model_source(
                primary_url="https://files.chat.totools.cn/a.png",
                fallback_url=None,
                source_key="file-id",
                ttl_seconds=59,
            )


class ModelImageSourceTests(unittest.IsolatedAsyncioTestCase):
    def test_cdn_path_resolution_accepts_only_configured_main_objects(self):
        from .core import CONFIG_STORE

        config = CONFIG_STORE.load()
        config["cos"]["bucket"] = "example-bucket"
        config["cos"]["prefix"] = "turtle-gpt"
        config["cdn"]["files_base_url"] = "https://files.chat.totools.cn"
        with patch(
            "open_webui.turtle_storage.media.CONFIG_STORE.load",
            return_value=config,
        ):
            self.assertEqual(
                _cloud_path_from_files_cdn_url(
                    "https://files.chat.totools.cn/"
                    "turtle-gpt/files/users/user-a/photo.webp?sign=test"
                ),
                "s3://example-bucket/turtle-gpt/files/users/user-a/photo.webp",
            )
            self.assertIsNone(
                _cloud_path_from_files_cdn_url(
                    "https://img.chat.totools.cn/"
                    "turtle-gpt/files/users/user-a/photo.webp?sign=test"
                )
            )
            self.assertIsNone(
                _cloud_path_from_files_cdn_url(
                    "https://files.chat.totools.cn/"
                    "turtle-gpt/thumbnails/users/user-a/photo.webp?sign=test"
                )
            )
            self.assertIsNone(
                _cloud_path_from_files_cdn_url(
                    "https://files.chat.totools.cn/"
                    "turtle-gpt/files/users/../user-b/photo.webp?sign=test"
                )
            )

    async def test_owned_main_object_is_cdn_first_with_a_signed_cos_fallback(self):
        file_id = "11111111-1111-1111-1111-111111111111"
        file_item = SimpleNamespace(
            id=file_id,
            user_id="user-a",
            path="s3://bucket/turtle/files/users/user-a/image.png",
            filename="image.png",
            meta={"name": "image.png", "size": 1024},
        )
        user = SimpleNamespace(id="user-a", role="user")
        calls = []

        def presign(_path, **kwargs):
            calls.append(kwargs["use_cdn"])
            return (
                "https://files.chat.totools.cn/turtle/files/users/user-a/image.png?sign=test"
                if kwargs["use_cdn"]
                else "https://bucket.cos.ap-shanghai.myqcloud.com/turtle/files/users/user-a/image.png?q=test"
            )

        with (
            patch(
                "open_webui.turtle_storage.media.Files.get_file_by_id",
                new=AsyncMock(return_value=file_item),
            ),
            patch(
                "open_webui.turtle_storage.media.Storage.presign_download",
                side_effect=presign,
            ),
            patch(
                "open_webui.turtle_storage.media.Storage.download_url_ttl",
                return_value=600,
            ),
            patch(
                "open_webui.turtle_storage.media.MEDIA_PUMP.seal_model_source",
                return_value="signed-source-token",
            ) as seal,
        ):
            result = await get_presigned_model_image_source(
                f"/api/v1/files/{file_id}/content",
                user,
            )

        self.assertEqual(calls, [True, False])
        self.assertTrue(result["url"].startswith("https://files.chat.totools.cn/"))
        self.assertEqual(result["turtle_source"], "signed-source-token")
        self.assertEqual(seal.call_args.kwargs["ttl_seconds"], 600)
        self.assertIn(file_id, seal.call_args.kwargs["source_key"])

    async def test_foreign_file_never_gets_model_source_urls(self):
        file_id = "22222222-2222-2222-2222-222222222222"
        file_item = SimpleNamespace(
            id=file_id,
            user_id="user-b",
            path="s3://bucket/turtle/files/users/user-b/image.png",
            filename="image.png",
            meta={},
        )
        with (
            patch(
                "open_webui.turtle_storage.media.Files.get_file_by_id",
                new=AsyncMock(return_value=file_item),
            ),
            patch(
                "open_webui.turtle_storage.media.Storage.presign_download"
            ) as presign,
        ):
            result = await get_presigned_model_image_source(
                f"/api/v1/files/{file_id}/content",
                SimpleNamespace(id="user-a", role="user"),
            )

        self.assertIsNone(result)
        presign.assert_not_called()


class GeneratedMediaPatternTests(unittest.TestCase):
    def test_explicit_multi_image_counts_are_bounded_and_localizable(self):
        self.assertEqual(requested_image_count("请生成两张不同的图片"), 2)
        self.assertEqual(requested_image_count("生成 4 幅海龟图像"), 4)
        self.assertEqual(requested_image_count("Create three distinct images"), 3)
        self.assertEqual(requested_image_count("请修改第二张图"), 1)
        self.assertEqual(requested_image_count("请生成五张图片"), 1)

    def test_remote_image_and_video_urls_are_detected(self):
        text = (
            "![image](https://cdn.example.com/a.png) "
            '<video src="https://cdn.example.com/b.mp4"></video>'
        )
        self.assertEqual(
            [match.group("url") for match in MEDIA_REFERENCE_RE.finditer(text)],
            ["https://cdn.example.com/a.png", "https://cdn.example.com/b.mp4"],
        )

    def test_base64_markdown_image_is_detected(self):
        text = "![image](data:image/png;base64,aGVsbG8=)"
        match = BASE64_MEDIA_REFERENCE_RE.search(text)
        self.assertIsNotNone(match)
        self.assertEqual(match.group("url"), "data:image/png;base64,aGVsbG8=")

    def test_generated_zip_markdown_link_is_detected_without_matching_images(self):
        text = (
            "[下载 result.zip](https://pump.example/v1/source/token) "
            "![image](https://pump.example/v1/source/image-token)"
        )
        matches = list(FILE_REFERENCE_RE.finditer(text))
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].group("label"), "下载 result.zip")
        self.assertEqual(matches[0].group("url"), "https://pump.example/v1/source/token")

    def test_stream_output_hides_pump_capabilities_without_mutating_source(self):
        from .media import MEDIA_PUMP, STREAM_MEDIA_PLACEHOLDER

        source_url = "https://pump.example/v1/source/opaque-capability"
        output = [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": (
                            "图片已经完成：\n"
                            f"[![风景]({source_url})]({source_url})\n"
                            f"[下载 result.zip]({source_url})"
                        ),
                    }
                ],
            }
        ]

        with patch.object(MEDIA_PUMP, "base_url", "https://pump.example"):
            visible = visible_stream_output(output)

        source_text = output[0]["content"][0]["text"]
        visible_text = visible[0]["content"][0]["text"]
        self.assertIn(source_url, source_text)
        self.assertNotIn("pump.example", visible_text)
        self.assertIn("图片已经完成", visible_text)
        self.assertEqual(visible_text.count(STREAM_MEDIA_PLACEHOLDER), 1)

    def test_stream_output_replaces_media_only_response_with_status(self):
        from .media import MEDIA_PUMP, STREAM_MEDIA_PLACEHOLDER

        output = [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "![image](https://pump.example/v1/source/partial",
                    }
                ],
            }
        ]
        with patch.object(MEDIA_PUMP, "base_url", "https://pump.example"):
            visible = visible_stream_output(output)

        self.assertEqual(visible[0]["content"][0]["text"], STREAM_MEDIA_PLACEHOLDER)

    def test_historical_images_are_deduplicated_and_moved_to_latest_user_turn(self):
        first_image = {
            "type": "image_url",
            "image_url": {"url": "/api/v1/files/11111111-1111-1111-1111-111111111111/content"},
        }
        second_image = {
            "type": "image_url",
            "image_url": {"url": "/api/v1/files/22222222-2222-2222-2222-222222222222/content"},
        }
        messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "先看这张图"}, first_image],
            },
            {"role": "assistant", "content": "看到了"},
            {
                "role": "user",
                "content": [{"type": "text", "text": "再看一张"}, first_image, second_image],
            },
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "请分析第一张图片"},
        ]

        forwarded = carry_forward_message_images(messages)

        self.assertEqual(messages[0]["content"][-1], first_image)
        self.assertEqual(forwarded[0]["content"], [{"type": "text", "text": "先看这张图"}])
        self.assertEqual(forwarded[2]["content"], [{"type": "text", "text": "再看一张"}])
        self.assertEqual(
            forwarded[-1]["content"],
            [
                {"type": "text", "text": "请分析第一张图片"},
                first_image,
                second_image,
            ],
        )

    def test_first_turn_preview_url_is_bound_to_managed_file_id(self):
        file_id = "11111111-1111-1111-1111-111111111111"
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "识别图片"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "https://files.chat.totools.cn/transient?sign=old",
                            "detail": "auto",
                        },
                    },
                ],
                "files": [
                    {
                        "id": file_id,
                        "type": "image",
                        "content_type": "image/png",
                    }
                ],
            }
        ]

        bound = bind_message_image_file_ids(messages)

        self.assertEqual(
            bound[0]["content"][1]["image_url"],
            {"url": file_id, "detail": "auto"},
        )
        self.assertTrue(
            messages[0]["content"][1]["image_url"]["url"].startswith("https://")
        )

    def test_first_db_backed_turn_builds_image_parts_from_managed_file_ids(self):
        file_id = "22222222-2222-2222-2222-222222222222"
        messages = [
            {
                "role": "user",
                "content": "识别图片",
                "files": [
                    {
                        "id": file_id,
                        "url": "https://files.chat.totools.cn/transient?sign=old",
                        "type": "image",
                        "content_type": "image/png",
                    }
                ],
            }
        ]

        bound = bind_message_image_file_ids(messages)

        self.assertEqual(
            bound[0]["content"],
            [
                {"type": "text", "text": "识别图片"},
                {"type": "image_url", "image_url": {"url": file_id}},
            ],
        )
        self.assertEqual(messages[0]["content"], "识别图片")

    def test_first_turn_nested_upload_file_is_bound_to_managed_file_id(self):
        file_id = "33333333-3333-3333-3333-333333333333"
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "识别图片"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "https://files.chat.totools.cn/transient?sign=old",
                        },
                    },
                ],
                "files": [
                    {
                        "url": "https://files.chat.totools.cn/transient?sign=old",
                        "file": {
                            "id": file_id,
                            "content_type": "image/png",
                        },
                    }
                ],
            }
        ]

        bound = bind_message_image_file_ids(messages)

        self.assertEqual(
            bound[0]["content"][1]["image_url"],
            {"url": file_id},
        )

    def test_first_turn_conflicting_nested_file_ids_fail_safe(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/remote.png"},
                    },
                ],
                "files": [
                    {
                        "id": "44444444-4444-4444-4444-444444444444",
                        "type": "image",
                        "file": {
                            "id": "55555555-5555-5555-5555-555555555555",
                            "content_type": "image/png",
                        },
                    }
                ],
            }
        ]

        self.assertEqual(bind_message_image_file_ids(messages), messages)

    def test_first_turn_binding_fails_safe_when_image_counts_do_not_match(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/remote.png"},
                    },
                ],
                "files": [],
            }
        ]

        self.assertEqual(bind_message_image_file_ids(messages), messages)


class QuotaIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from open_webui.internal.db import get_async_db_context

        self.db_context = get_async_db_context()
        self.db = await self.db_context.__aenter__()
        self.user_id = "turtle-storage-unit-user"

    async def asyncTearDown(self):
        from open_webui.models.files import Files

        for file in await Files.get_files_by_user_id(self.user_id, db=self.db):
            await Files.delete_file_by_id(file.id, db=self.db)
        await self.db_context.__aexit__(None, None, None)

    async def test_quota_counts_reserved_files_and_rejects_overage(self):
        from open_webui.models.files import FileForm, Files

        from .quota import QuotaExceededError, ensure_upload_capacity, quota_summary

        await Files.insert_new_file(
            self.user_id,
            FileForm(
                id="quota-reservation",
                filename="reserved.webp",
                path="/tmp/reserved.webp",
                data={"status": "uploading"},
                meta={"size": 1024, "content_type": "image/webp"},
            ),
            db=self.db,
        )
        summary = await quota_summary(self.user_id, self.db)
        self.assertEqual(summary["used_bytes"], 1024)
        with self.assertRaises(QuotaExceededError):
            await ensure_upload_capacity(self.user_id, summary["quota_bytes"], self.db)

    async def test_first_turn_files_cdn_url_resolves_owned_file_identity(self):
        from open_webui.models.files import FileForm, Files

        from .core import CONFIG_STORE

        file_id = "66666666-6666-6666-6666-666666666666"
        cloud_path = (
            "s3://example-bucket/turtle-gpt/files/users/"
            f"{self.user_id}/{file_id}/photo.webp"
        )
        await Files.insert_new_file(
            self.user_id,
            FileForm(
                id=file_id,
                filename="photo.webp",
                path=cloud_path,
                data={"status": "completed"},
                meta={"size": 1024, "content_type": "image/webp"},
            ),
            db=self.db,
        )
        config = CONFIG_STORE.load()
        config["cos"]["bucket"] = "example-bucket"
        config["cos"]["prefix"] = "turtle-gpt"
        config["cdn"]["files_base_url"] = "https://files.chat.totools.cn"
        primary = (
            "https://files.chat.totools.cn/turtle-gpt/files/users/"
            f"{self.user_id}/{file_id}/photo.webp?sign=first-turn"
        )

        def presign(_path, **kwargs):
            return (
                primary
                if kwargs["use_cdn"]
                else "https://example-bucket.cos.ap-shanghai.myqcloud.com/"
                "turtle-gpt/files/users/user/photo.webp?q=fallback"
            )

        with (
            patch(
                "open_webui.turtle_storage.media.CONFIG_STORE.load",
                return_value=config,
            ),
            patch(
                "open_webui.turtle_storage.media.Storage.presign_download",
                side_effect=presign,
            ),
            patch(
                "open_webui.turtle_storage.media.Storage.download_url_ttl",
                return_value=600,
            ),
            patch(
                "open_webui.turtle_storage.media.MEDIA_PUMP.seal_model_source",
                return_value="signed-source-token",
            ) as seal,
        ):
            result = await get_presigned_model_image_source(
                primary,
                SimpleNamespace(id=self.user_id, role="user"),
            )

        self.assertEqual(result["url"], primary)
        self.assertEqual(result["turtle_source"], "signed-source-token")
        self.assertEqual(
            seal.call_args.kwargs["source_key"],
            f"{file_id}\0{cloud_path}",
        )

    async def test_quota_aggregate_ignores_failed_cancelled_and_excluded_files(self):
        from open_webui.models.files import FileForm, Files

        from .quota import usage_bytes

        for file_id, size, storage_size, status_value in (
            ("quota-complete", 2048, 2304, "completed"),
            ("quota-uploading", 1024, None, "uploading"),
            ("quota-failed", 8192, 8448, "failed"),
            ("quota-cancelled", 16384, 16640, "cancelled"),
        ):
            await Files.insert_new_file(
                self.user_id,
                FileForm(
                    id=file_id,
                    filename=f"{file_id}.webp",
                    path=f"/tmp/{file_id}.webp",
                    data={"status": status_value},
                    meta={
                        "size": size,
                        "content_type": "image/webp",
                        **({"storage_size": storage_size} if storage_size else {}),
                    },
                ),
                db=self.db,
            )

        self.assertEqual(await usage_bytes(self.user_id, self.db), 3328)
        self.assertEqual(
            await usage_bytes(self.user_id, self.db, exclude_file_id="quota-uploading"),
            2304,
        )

    async def test_media_limit_applies_in_local_mode(self):
        from .core import CONFIG_STORE
        from .quota import MediaTooLargeError, ensure_media_size

        limit = CONFIG_STORE.load()["media"]["max_image_bytes"]
        ensure_media_size("image/png", limit)
        with self.assertRaises(MediaTooLargeError):
            ensure_media_size("image/png", limit + 1)
        file_limit = CONFIG_STORE.load()["media"]["max_file_bytes"]
        ensure_media_size("application/zip", file_limit)
        with self.assertRaises(MediaTooLargeError):
            ensure_media_size("application/zip", file_limit + 1)

    async def test_direct_upload_reservation_completion_and_user_isolation(self):
        from fastapi import HTTPException
        from open_webui.models.files import Files

        from .router import (
            CompleteUploadForm,
            PresignUploadForm,
            ThumbnailUploadForm,
            cancel_upload,
            complete_upload,
            get_file_url,
            get_inline_thumbnail,
            presign_upload,
        )
        from .router import Storage as RouterStorage

        owner = SimpleNamespace(id=self.user_id, role="user")
        outsider = SimpleNamespace(id="different-user", role="user")
        form = PresignUploadForm(
            filename="photo.png",
            content_type="image/png",
            size=2048,
            thumbnail=ThumbnailUploadForm(
                content_type="image/webp",
                size=128,
                width=480,
                height=320,
            ),
        )
        with (
            patch.object(RouterStorage, "direct_upload_available", return_value=True),
            patch.object(
                RouterStorage,
                "build_cloud_path",
                side_effect=lambda user_id, file_id, filename: f"s3://bucket/users/{user_id}/{file_id}_{filename}",
            ),
            patch.object(
                RouterStorage,
                "presign_upload",
                return_value={
                    "url": "https://example.invalid/signed-put",
                    "headers": {"Content-Type": "image/png"},
                    "expires_in": 900,
                },
            ),
        ):
            ticket = await presign_upload(form, user=owner, db=self.db)

        record = await Files.get_file_by_id(ticket["file_id"], db=self.db)
        self.assertEqual(record.user_id, self.user_id)
        self.assertEqual(record.data["status"], "uploading")
        self.assertEqual(record.meta["storage_size"], 2176)
        self.assertTrue(ticket["thumbnail_upload"]["upload_url"].startswith("https://example.invalid/"))
        with self.assertRaises(HTTPException) as denied:
            await get_file_url(ticket["file_id"], user=outsider, db=self.db)
        self.assertEqual(denied.exception.status_code, 404)

        with patch.object(
            RouterStorage,
            "presign_download",
            side_effect=StorageConfigurationError("missing test configuration"),
        ):
            with self.assertRaises(HTTPException) as unavailable:
                await get_file_url(ticket["file_id"], user=owner, db=self.db)
        self.assertEqual(unavailable.exception.status_code, 503)

        with patch.object(
            RouterStorage,
            "head_file",
            side_effect=lambda path: (
                {"ContentLength": 128, "ContentType": "image/webp"}
                if str(path).endswith(".thumbnail.webp")
                else {"ContentLength": 2048, "ContentType": "image/png"}
            ),
        ):
            completed = await complete_upload(
                CompleteUploadForm(file_id=ticket["file_id"]),
                user=owner,
                db=self.db,
            )
        self.assertEqual(completed.data["status"], "completed")
        self.assertEqual(completed.meta.size, 2048)
        record = await Files.get_file_by_id(ticket["file_id"], db=self.db)
        self.assertEqual(record.meta["storage_size"], 2176)
        self.assertEqual(record.meta["thumbnail"]["status"], "completed")

        with patch.object(RouterStorage, "delete_file") as delete_file:
            late_cancel = await cancel_upload(ticket["file_id"], user=owner, db=self.db)
        self.assertTrue(late_cancel["completed"])
        delete_file.assert_not_called()
        self.assertIsNotNone(await Files.get_file_by_id(ticket["file_id"], db=self.db))

        with patch.object(
            RouterStorage,
            "presign_download",
            return_value="https://cos.example/static-thumbnail?signature=get",
        ) as presign_download:
            thumbnail_url = await get_file_url(
                ticket["file_id"],
                variant="thumbnail",
                user=owner,
                db=self.db,
            )
        self.assertEqual(thumbnail_url["variant"], "thumbnail")
        self.assertTrue(presign_download.call_args.args[0].endswith(".thumbnail.webp"))
        self.assertEqual(presign_download.call_args.kwargs["variant"], "original")

        with patch.object(
            RouterStorage,
            "presign_download",
            return_value="https://cos.example/static-thumbnail?signature=get",
        ):
            inline = await get_inline_thumbnail(
                ticket["file_id"],
                user=owner,
                db=self.db,
            )
        self.assertEqual(inline.status_code, 307)
        self.assertEqual(
            inline.headers["location"],
            "https://cos.example/static-thumbnail?signature=get",
        )
        self.assertIn("private, max-age=", inline.headers["cache-control"])

    async def test_my_space_cursor_is_stable_for_equal_timestamps(self):
        from open_webui.models.files import File, FileForm, Files
        from sqlalchemy import update

        from .router import get_my_space

        for file_id in ("cursor-a", "cursor-b", "cursor-c"):
            await Files.insert_new_file(
                self.user_id,
                FileForm(
                    id=file_id,
                    filename=f"{file_id}.webp",
                    path=f"/tmp/{file_id}.webp",
                    data={"status": "completed"},
                    meta={"size": 64, "content_type": "image/webp"},
                ),
                db=self.db,
            )
        await self.db.execute(
            update(File)
            .where(File.id.in_(["cursor-a", "cursor-b", "cursor-c"]))
            .values(created_at=1_700_000_000)
        )
        await self.db.commit()
        user = SimpleNamespace(id=self.user_id, role="user")

        first = await get_my_space(
            kind="image",
            page=1,
            page_size=60,
            cursor=None,
            limit=2,
            include_summary=True,
            user=user,
            db=self.db,
        )
        self.assertEqual([item["id"] for item in first["items"]], ["cursor-c", "cursor-b"])
        self.assertEqual(first["total"], 3)
        self.assertTrue(first["has_more"])
        self.assertIsNotNone(first["next_cursor"])

        second = await get_my_space(
            kind="image",
            page=1,
            page_size=60,
            cursor=first["next_cursor"],
            limit=2,
            include_summary=False,
            user=user,
            db=self.db,
        )
        self.assertEqual([item["id"] for item in second["items"]], ["cursor-a"])
        self.assertIsNone(second["total"])
        self.assertIsNone(second["quota"])
        self.assertFalse(second["has_more"])
        self.assertIsNone(second["next_cursor"])

    async def test_completed_output_rewrites_image_and_video_urls(self):
        from .media import persist_output_media

        output = [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "![image](https://cdn.example/image.png)\n<video src=\"https://cdn.example/video.mp4\"></video>",
                    }
                ],
            }
        ]

        async def fake_persist(_request, url, _metadata, _user):
            return "/managed/" + url.rsplit("/", 1)[-1]

        with patch("open_webui.turtle_storage.media.persist_generated_media_url", side_effect=fake_persist):
            rewritten = await persist_output_media(None, output, {}, SimpleNamespace(id=self.user_id))
        text = rewritten[0]["content"][0]["text"]
        self.assertIn("/managed/image.png", text)
        self.assertIn("/managed/video.mp4", text)
        self.assertNotIn("https://cdn.example", text)

    async def test_completed_output_rewrites_generated_zip_capability(self):
        from .media import MEDIA_PUMP, persist_output_media

        source_url = "https://pump.example/v1/source/opaque-capability"
        output = [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": f"[下载 turtle-media-audit.zip]({source_url})",
                    }
                ],
            }
        ]

        async def fake_persist(_request, url, _metadata, _user, *, filename_hint=None):
            self.assertEqual(url, source_url)
            self.assertEqual(filename_hint, "下载 turtle-media-audit.zip")
            return "/api/v1/files/managed-zip/content"

        with (
            patch.object(MEDIA_PUMP, "base_url", "https://pump.example"),
            patch("open_webui.turtle_storage.media.persist_generated_media_url", side_effect=fake_persist),
        ):
            rewritten = await persist_output_media(None, output, {}, SimpleNamespace(id=self.user_id))

        self.assertEqual(
            rewritten[0]["content"][0]["text"],
            "[下载 turtle-media-audit.zip](/api/v1/files/managed-zip/content)",
        )

    async def test_generated_media_is_copied_by_pump_without_server_download(self):
        from open_webui.models.files import Files

        from .media import MEDIA_PUMP, persist_generated_media_url
        from .media import Storage as MediaStorage

        request = SimpleNamespace(state=SimpleNamespace())
        user = SimpleNamespace(id=self.user_id, role="user")
        source_url = "https://chatgpt-cdn.example/generated.png?signature=short-lived"
        transfer = AsyncMock(return_value={"ok": True, "size": 5, "sha256": "a" * 64})
        with (
            patch.object(MEDIA_PUMP, "configured", return_value=True),
            patch.object(MEDIA_PUMP, "probe", AsyncMock(return_value={"size": 5, "content_type": "image/png"})),
            patch.object(MEDIA_PUMP, "transfer", transfer),
            patch.object(MediaStorage, "direct_upload_available", return_value=True),
            patch.object(
                MediaStorage,
                "build_cloud_path",
                side_effect=lambda user_id, file_id, name: f"s3://bucket/users/{user_id}/{file_id}_{name}",
            ),
            patch.object(
                MediaStorage,
                "presign_upload",
                return_value={
                    "url": "https://cos.example/object?signature=put",
                    "headers": {"Content-Type": "image/png"},
                    "expires_in": 900,
                },
            ),
            patch.object(
                MediaStorage,
                "head_file",
                return_value={"ContentLength": 5, "ContentType": "image/png"},
            ),
        ):
            managed = await persist_generated_media_url(request, source_url, {}, user)

        self.assertRegex(managed, r"^/api/v1/files/[0-9a-f-]+/content$")
        transfer.assert_awaited_once()
        call = transfer.await_args.kwargs
        self.assertEqual(call["source_url"], source_url)
        self.assertTrue(call["destination_url"].startswith("https://cos.example/"))
        file_id = managed.split("/")[4]
        record = await Files.get_file_by_id(file_id, db=self.db)
        self.assertEqual(record.data["status"], "completed")
        self.assertTrue(record.meta["turtle_pump_transfer"])
        self.assertEqual(record.meta["thumbnail"]["status"], "pending")
        self.assertEqual(record.meta["storage_size"], 5)

    async def test_generated_zip_is_copied_to_managed_space_without_server_download(self):
        from open_webui.models.files import Files

        from .media import MEDIA_PUMP, persist_generated_media_url
        from .media import Storage as MediaStorage

        request = SimpleNamespace(state=SimpleNamespace())
        user = SimpleNamespace(id=self.user_id, role="user")
        source_url = "https://pump.example/v1/source/opaque-capability"
        transfer = AsyncMock(return_value={"ok": True, "size": 7, "sha256": "b" * 64})
        with (
            patch.object(MEDIA_PUMP, "configured", return_value=True),
            patch.object(
                MEDIA_PUMP,
                "probe",
                AsyncMock(return_value={"size": 7, "content_type": "application/octet-stream"}),
            ),
            patch.object(MEDIA_PUMP, "transfer", transfer),
            patch.object(MediaStorage, "direct_upload_available", return_value=True),
            patch.object(
                MediaStorage,
                "build_cloud_path",
                side_effect=lambda user_id, file_id, name: f"s3://bucket/users/{user_id}/{file_id}_{name}",
            ),
            patch.object(
                MediaStorage,
                "presign_upload",
                return_value={
                    "url": "https://cos.example/object?signature=put",
                    "headers": {"Content-Type": "application/zip"},
                    "expires_in": 900,
                },
            ),
            patch.object(
                MediaStorage,
                "head_file",
                return_value={"ContentLength": 7, "ContentType": "application/zip"},
            ),
        ):
            managed = await persist_generated_media_url(
                request,
                source_url,
                {},
                user,
                filename_hint="下载 turtle-media-audit.zip",
            )

        self.assertRegex(managed, r"^/api/v1/files/[0-9a-f-]+/content$")
        call = transfer.await_args.kwargs
        self.assertEqual(call["expected_content_type"], "application/zip")
        self.assertEqual(call["max_bytes"], 200 * 1024**2)
        record = await Files.get_file_by_id(managed.split("/")[4], db=self.db)
        self.assertEqual(record.filename, "turtle-media-audit.zip")
        self.assertEqual(record.meta["content_type"], "application/zip")
        self.assertEqual(record.meta["origin"], "chatgpt-generated")
        self.assertNotIn("thumbnail", record.meta)

    async def test_generated_static_thumbnail_is_reserved_verified_and_counted(self):
        from fastapi import HTTPException
        from open_webui.models.files import FileForm, Files

        from .router import (
            CompleteUploadForm,
            ThumbnailPresignForm,
            cancel_thumbnail,
            complete_thumbnail,
            get_thumbnail_status,
            presign_thumbnail,
        )
        from .router import Storage as RouterStorage

        file_id = "generated-static-thumbnail"
        await Files.insert_new_file(
            self.user_id,
            FileForm(
                id=file_id,
                filename="generated.png",
                path=f"s3://bucket/users/{self.user_id}/generated.png",
                data={"status": "completed"},
                meta={
                    "name": "generated.png",
                    "content_type": "image/png",
                    "size": 500,
                    "storage_size": 500,
                    "origin": "chatgpt-generated",
                    "thumbnail": {"status": "pending"},
                },
            ),
            db=self.db,
        )
        owner = SimpleNamespace(id=self.user_id, role="user")
        before = await get_thumbnail_status(file_id, user=owner, db=self.db)
        self.assertTrue(before["eligible"])
        self.assertFalse(before["ready"])

        with (
            patch.object(RouterStorage, "direct_upload_available", return_value=True),
            patch.object(
                RouterStorage,
                "presign_upload",
                return_value={
                    "url": "https://cos.example/thumbnail?signature=put",
                    "headers": {"Content-Type": "image/webp"},
                    "expires_in": 900,
                },
            ),
            patch.object(
                RouterStorage,
                "head_file",
                return_value={"ContentLength": 120, "ContentType": "image/webp"},
            ),
        ):
            ticket = await presign_thumbnail(
                ThumbnailPresignForm(
                    file_id=file_id,
                    content_type="image/webp",
                    size=120,
                    width=480,
                    height=320,
                ),
                user=owner,
                db=self.db,
            )
            self.assertFalse(ticket["ready"])
            with self.assertRaises(HTTPException) as duplicate:
                await presign_thumbnail(
                    ThumbnailPresignForm(
                        file_id=file_id,
                        content_type="image/webp",
                        size=120,
                        width=480,
                        height=320,
                    ),
                    user=owner,
                    db=self.db,
                )
            self.assertEqual(duplicate.exception.status_code, 409)
            completed = await complete_thumbnail(
                CompleteUploadForm(file_id=file_id),
                user=owner,
                db=self.db,
            )

        self.assertTrue(completed["ready"])
        record = await Files.get_file_by_id(file_id, db=self.db)
        self.assertEqual(record.meta["thumbnail"]["status"], "completed")
        self.assertEqual(record.meta["storage_size"], 620)
        with patch.object(RouterStorage, "delete_thumbnail") as delete_thumbnail:
            late_cancel = await cancel_thumbnail(file_id, user=owner, db=self.db)
        self.assertTrue(late_cancel["ready"])
        delete_thumbnail.assert_not_called()

    async def test_strict_upload_middleware_rejects_before_reading_body(self):
        from .pump import RejectServerFileUploadMiddleware

        downstream_called = False
        receive_called = False

        async def downstream(scope, receive, send):
            nonlocal downstream_called
            downstream_called = True

        async def receive():
            nonlocal receive_called
            receive_called = True
            raise AssertionError("multipart body must not be read")

        sent = []

        async def send(message):
            sent.append(message)

        middleware = RejectServerFileUploadMiddleware(downstream)
        await middleware(
            {"type": "http", "method": "POST", "path": "/api/v1/files/"},
            receive,
            send,
        )

        self.assertFalse(downstream_called)
        self.assertFalse(receive_called)
        self.assertEqual(sent[0]["status"], 409)


if __name__ == "__main__":
    unittest.main()
