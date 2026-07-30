"""Focused integration tests runnable inside the pinned Open WebUI image."""

from __future__ import annotations

import json
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from starlette.requests import Request


class ProjectFileScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from open_webui.internal.db import get_async_db_context
        from open_webui.models.files import FileForm, Files

        from .router import ProjectActor

        self.db_context = get_async_db_context()
        self.db = await self.db_context.__aenter__()
        self.user_id = f"project-media-user-{uuid.uuid4()}"
        self.actor = ProjectActor(
            user=SimpleNamespace(id=self.user_id, role="user"),
            key_id="project-key-a",
            owner_user_id=self.user_id,
        )
        self.created_ids: list[str] = []
        self.file_ids = [
            "11111111-2222-4333-8444-555555555555",
            "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "99999999-8888-4777-8666-555555555555",
        ]
        for file_id, key_id in zip(
            self.file_ids,
            ("project-key-a", "project-key-b", None),
            strict=True,
        ):
            await Files.insert_new_file(
                self.user_id,
                FileForm(
                    id=file_id,
                    filename=f"{file_id}.webp",
                    path=(
                        "s3://bucket/turtle/files/users/"
                        f"{self.user_id}/{file_id}/photo.webp"
                    ),
                    data={"status": "completed"},
                    meta={
                        "name": "photo.webp",
                        "size": 1024,
                        "content_type": "image/webp",
                        "data": (
                            {
                                "project_api_key_id": key_id,
                                "project_api_purpose": "vision",
                            }
                            if key_id
                            else {}
                        ),
                    },
                ),
                db=self.db,
            )

    async def asyncTearDown(self):
        from open_webui.models.files import Files

        for file in await Files.get_files_by_user_id(self.user_id, db=self.db):
            await Files.delete_file_by_id(file.id, db=self.db)
        await self.db_context.__aexit__(None, None, None)

    @staticmethod
    def request(
        method: str = "GET",
        *,
        body: bytes = b"",
        content_type: str | None = None,
    ) -> Request:
        delivered = False

        async def receive():
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        headers = []
        if content_type:
            headers.append((b"content-type", content_type.encode("ascii")))
        if body:
            headers.append((b"content-length", str(len(body)).encode("ascii")))
        return Request(
            {
                "type": "http",
                "method": method,
                "path": "/api/project/v1/files",
                "headers": headers,
                "query_string": b"",
                "server": ("testserver", 80),
                "client": ("127.0.0.1", 12345),
                "scheme": "http",
            },
            receive=receive,
        )

    async def test_list_and_lookup_are_project_key_scoped(self):
        from .router import (
            _project_file_for_actor,
            list_project_files,
        )

        owned = await _project_file_for_actor(
            f"file-{self.file_ids[0]}",
            self.actor,
            self.db,
        )
        foreign_key = await _project_file_for_actor(
            f"file-{self.file_ids[1]}",
            self.actor,
            self.db,
        )
        browser_file = await _project_file_for_actor(
            f"file-{self.file_ids[2]}",
            self.actor,
            self.db,
        )
        self.assertIsNotNone(owned)
        self.assertIsNone(foreign_key)
        self.assertIsNone(browser_file)

        with patch(
            "open_webui.turtle_project_api.router._project_actor",
            new=AsyncMock(return_value=(self.actor, None)),
        ):
            payload = await list_project_files(
                self.request(),
                purpose=None,
                limit=100,
                order="desc",
                after=None,
                db=self.db,
            )
        self.assertEqual(
            [item["id"] for item in payload["data"]],
            [f"file-{self.file_ids[0]}"],
        )

    async def test_content_is_a_cdn_redirect_without_media_body(self):
        from .router import retrieve_project_file_content

        with (
            patch(
                "open_webui.turtle_project_api.router._project_actor",
                new=AsyncMock(return_value=(self.actor, None)),
            ),
            patch(
                "open_webui.turtle_project_api.router.Storage.presign_download",
                return_value="https://files.chat.totools.cn/turtle/photo.webp?sign=test",
            ),
        ):
            response = await retrieve_project_file_content(
                f"file-{self.file_ids[0]}",
                self.request(),
                db=self.db,
            )
        self.assertEqual(response.status_code, 307)
        self.assertTrue(response.headers["location"].startswith("https://files.chat.totools.cn/"))
        self.assertEqual(response.headers["cache-control"], "private, no-store")

    async def test_reserve_complete_and_delete_use_control_plane_only(self):
        from open_webui.models.files import Files

        from .router import (
            complete_project_file,
            create_project_file,
            delete_project_file,
        )

        body = json.dumps(
            {
                "filename": "api-photo.webp",
                "bytes": 2048,
                "purpose": "vision",
                "content_type": "image/webp",
            }
        ).encode()
        actor_patch = patch(
            "open_webui.turtle_project_api.router._project_actor",
            new=AsyncMock(return_value=(self.actor, None)),
        )
        with (
            actor_patch,
            patch(
                "open_webui.turtle_storage.router.Storage.direct_upload_available",
                return_value=True,
            ),
            patch(
                "open_webui.turtle_storage.router.Storage.build_cloud_path",
                side_effect=lambda user_id, file_id, filename: (
                    f"s3://bucket/turtle/files/users/{user_id}/{file_id}/{filename}"
                ),
            ),
            patch(
                "open_webui.turtle_storage.router.Storage.presign_upload",
                return_value={
                    "url": "https://bucket.cos.example/direct-put?signature=test",
                    "headers": {"Content-Type": "image/webp"},
                    "expires_in": 900,
                },
            ),
        ):
            reserved = await create_project_file(
                self.request(
                    "POST",
                    body=body,
                    content_type="application/json",
                ),
                db=self.db,
            )
        reservation = json.loads(reserved.body)
        self.assertTrue(reservation["id"].startswith("file-"))
        self.assertEqual(reservation["object"], "file")
        self.assertEqual(reservation["bytes"], 2048)
        self.assertEqual(reservation["purpose"], "vision")
        self.assertEqual(reservation["status"], "uploading")
        self.assertTrue(reservation["upload"]["url"].startswith("https://bucket.cos.example/"))
        file_id = reservation["id"].removeprefix("file-")
        self.created_ids.append(file_id)
        record = await Files.get_file_by_id(file_id, db=self.db)
        self.assertEqual(record.meta["data"]["project_api_key_id"], self.actor.key_id)

        with (
            patch(
                "open_webui.turtle_project_api.router._project_actor",
                new=AsyncMock(return_value=(self.actor, None)),
            ),
            patch(
                "open_webui.turtle_storage.router.Storage.head_file",
                return_value={"ContentLength": 2048, "ContentType": "image/webp"},
            ),
        ):
            completed = await complete_project_file(
                reservation["id"],
                self.request("POST"),
                db=self.db,
            )
        completed_payload = json.loads(completed.body)
        self.assertEqual(completed_payload["status"], "completed")

        with (
            patch(
                "open_webui.turtle_project_api.router._project_actor",
                new=AsyncMock(return_value=(self.actor, None)),
            ),
            patch(
                "open_webui.turtle_project_api.router.Storage.delete_file",
            ) as delete_object,
        ):
            deleted = await delete_project_file(
                reservation["id"],
                self.request("DELETE"),
                db=self.db,
            )
        self.assertTrue(deleted["deleted"])
        delete_object.assert_called_once()
        self.assertIsNone(await Files.get_file_by_id(file_id, db=self.db))


if __name__ == "__main__":
    unittest.main()
