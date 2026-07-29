"""Dynamic local/Tencent COS storage provider for Open WebUI."""

from __future__ import annotations

import hashlib
import mimetypes
import secrets
import shutil
import time
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

import boto3
from botocore.config import Config

from open_webui.config import UPLOAD_DIR
from open_webui.constants import ERROR_MESSAGES

from .core import (
    CONFIG_STORE,
    MAIN_OBJECT_NAMESPACE,
    THUMBNAIL_OBJECT_NAMESPACE,
    USER_OBJECT_NAMESPACE,
    ConfigStore,
    StorageConfigurationError,
    object_key,
)
from .pump import strict_media_mode


class TurtleStorageProvider:
    """A provider whose active backend is read from the persisted admin config.

    Cloud paths remain readable even if the active upload provider is later
    switched back to local storage, provided the saved COS credentials remain.
    """

    THUMBNAIL_SUFFIX = ".thumbnail.webp"

    def __init__(self, store: ConfigStore | None = None, upload_dir: str | Path | None = None):
        self.store = store or CONFIG_STORE
        self.upload_dir = Path(upload_dir or UPLOAD_DIR)

    def _client(self, config: dict | None = None):
        config = config or self.store.load()
        cos = config["cos"]
        secret_id, secret_key = self.store.credentials(config)
        if not self.store.cos_ready(config):
            raise StorageConfigurationError("COS 尚未完整配置")
        boto_config = Config(
            signature_version="s3v4",
            s3={"addressing_style": cos.get("addressing_style", "virtual")},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 3, "mode": "standard"},
        )
        return boto3.client(
            "s3",
            region_name=cos["region"],
            endpoint_url=cos["endpoint_url"],
            aws_access_key_id=secret_id,
            aws_secret_access_key=secret_key,
            config=boto_config,
        )

    @staticmethod
    def _split_cloud_path(file_path: str) -> tuple[str, str]:
        if not str(file_path or "").startswith("s3://"):
            raise StorageConfigurationError("不是受支持的 COS 对象路径")
        remainder = str(file_path)[5:]
        bucket, separator, key = remainder.partition("/")
        if not separator or not bucket or not key:
            raise StorageConfigurationError("COS 对象路径无效")
        return bucket, key

    @staticmethod
    def is_cloud_path(file_path: str | None) -> bool:
        return bool(file_path and str(file_path).startswith("s3://"))

    def build_cloud_path(self, user_id: str, file_id: str, filename: str) -> str:
        config = self.store.load()
        if not self.store.cos_ready(config):
            raise StorageConfigurationError("COS 尚未完整配置")
        key = object_key(config["cos"].get("prefix", ""), user_id, file_id, filename)
        return f"s3://{config['cos']['bucket']}/{key}"

    @classmethod
    def thumbnail_path(cls, file_path: str) -> str:
        """Return the dedicated thumbnail object for a main object.

        New objects use separate ``files`` and ``thumbnails`` namespaces so
        each class can have an independent CDN origin authorization. Legacy
        objects retain their adjacent-thumbnail mapping until an explicit
        server-side migration updates the stored main-object path.
        """
        if not cls.is_cloud_path(file_path):
            raise StorageConfigurationError("静态缩略图只支持对象存储文件")
        bucket, key = cls._split_cloud_path(file_path)
        parts = key.split("/")
        if (
            len(parts) >= 4
            and parts[-4] == MAIN_OBJECT_NAMESPACE
            and parts[-3] == USER_OBJECT_NAMESPACE
        ):
            parts[-4] = THUMBNAIL_OBJECT_NAMESPACE
            key = "/".join(parts)
        return f"s3://{bucket}/{key}{cls.THUMBNAIL_SUFFIX}"

    def upload_file(self, file: BinaryIO, filename: str, tags: dict[str, str]):
        if strict_media_mode():
            raise StorageConfigurationError(
                "严格媒体隔离已启用，服务端文件上传被拒绝；请使用预签名直传或外部 Media Pump"
            )
        contents = file.read()
        if not contents:
            raise ValueError(ERROR_MESSAGES.EMPTY_CONTENT)

        config = self.store.load()
        if config["provider"] != "cos":
            self.upload_dir.mkdir(parents=True, exist_ok=True)
            path = self.upload_dir / Path(filename).name
            path.write_bytes(contents)
            return contents, str(path)

        user_id = tags.get("OpenWebUI-User-Id", "")
        file_id = tags.get("OpenWebUI-File-Id", "")
        if not user_id or not file_id:
            raise StorageConfigurationError("对象存储上传缺少不可变用户 ID 或文件 ID")
        file_path = self.build_cloud_path(user_id, file_id, filename)
        bucket, key = self._split_cloud_path(file_path)
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        self._client(config).put_object(
            Bucket=bucket,
            Key=key,
            Body=contents,
            ContentType=content_type,
        )
        return contents, file_path

    def get_file(self, file_path: str) -> str:
        if strict_media_mode():
            raise StorageConfigurationError(
                "严格媒体隔离已启用，服务端文件下载被拒绝；请使用预签名 GET"
            )
        if not self.is_cloud_path(file_path):
            return str(file_path)
        bucket, key = self._split_cloud_path(file_path)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        local_path = self.upload_dir / Path(key).name
        self._client().download_file(bucket, key, str(local_path))
        return str(local_path)

    def delete_file(self, file_path: str) -> None:
        if self.is_cloud_path(file_path):
            bucket, key = self._split_cloud_path(file_path)
            client = self._client()
            # Remove the derivative first. If permissions are broken, retain
            # the original and database row instead of leaving a dead record.
            thumbnail_bucket, thumbnail_key = self._split_cloud_path(
                self.thumbnail_path(file_path)
            )
            if thumbnail_bucket != bucket:
                raise StorageConfigurationError("主对象与缩略图不在同一存储桶")
            client.delete_object(Bucket=thumbnail_bucket, Key=thumbnail_key)
            client.delete_object(Bucket=bucket, Key=key)
            local_path = self.upload_dir / Path(key).name
            if local_path.is_file():
                local_path.unlink()
            return

        local_path = self.upload_dir / Path(str(file_path or "")).name
        if local_path.is_file():
            local_path.unlink()

    def delete_thumbnail(self, file_path: str) -> None:
        thumbnail_path = self.thumbnail_path(file_path)
        bucket, key = self._split_cloud_path(thumbnail_path)
        self._client().delete_object(Bucket=bucket, Key=key)

    def delete_all_files(self) -> None:
        config = self.store.load()
        if config["provider"] == "cos" and self.store.cos_ready(config):
            client = self._client(config)
            bucket = config["cos"]["bucket"]
            prefix = str(config["cos"].get("prefix", "")).strip("/")
            if not prefix:
                raise StorageConfigurationError("拒绝在空对象前缀下批量删除 COS 文件")
            prefix = f"{prefix}/"
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
                if objects:
                    client.delete_objects(Bucket=bucket, Delete={"Objects": objects, "Quiet": True})
        if self.upload_dir.exists():
            for child in self.upload_dir.iterdir():
                if child.is_file() or child.is_symlink():
                    child.unlink()
                elif child.is_dir():
                    shutil.rmtree(child)

    def direct_upload_available(self) -> bool:
        config = self.store.load()
        return bool(
            config["provider"] == "cos"
            and self.store.cos_ready(config)
            and config["cos"].get("direct_upload_enabled", True)
        )

    def presign_upload(self, file_path: str, content_type: str) -> dict:
        config = self.store.load()
        if not self.direct_upload_available():
            raise StorageConfigurationError("COS 浏览器直传尚未启用")
        bucket, key = self._split_cloud_path(file_path)
        ttl = int(config["cos"]["put_ttl_seconds"])
        url = self._client(config).generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=ttl,
        )
        return {"url": url, "headers": {"Content-Type": content_type}, "expires_in": ttl}

    def head_file(self, file_path: str) -> dict:
        bucket, key = self._split_cloud_path(file_path)
        return self._client().head_object(Bucket=bucket, Key=key)

    def presign_download(
        self,
        file_path: str,
        *,
        filename: str | None = None,
        attachment: bool = False,
        variant: str = "original",
        use_cdn: bool = True,
    ) -> str | None:
        if not self.is_cloud_path(file_path):
            return None
        config = self.store.load()
        bucket, key = self._split_cloud_path(file_path)
        if variant != "original":
            raise StorageConfigurationError("不支持的图片版本")
        cdn_kind = self._cdn_kind(key)
        if (
            use_cdn
            and not attachment
            and bucket == config["cos"]["bucket"]
            and cdn_kind
            and self.store.cdn_ready(config, cdn_kind)
        ):
            return self._presign_cdn_download(key, cdn_kind, config)
        params: dict[str, str] = {"Bucket": bucket, "Key": key}
        if attachment and filename:
            encoded = quote(filename)
            params["ResponseContentDisposition"] = f"attachment; filename*=UTF-8''{encoded}"
        return self._client(config).generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=int(config["cos"]["get_ttl_seconds"]),
        )

    @staticmethod
    def _cdn_kind(key: str) -> str | None:
        parts = str(key or "").split("/")
        if (
            len(parts) >= 4
            and parts[-3] == USER_OBJECT_NAMESPACE
            and parts[-4] == MAIN_OBJECT_NAMESPACE
        ):
            return "files"
        if (
            len(parts) >= 4
            and parts[-3] == USER_OBJECT_NAMESPACE
            and parts[-4] == THUMBNAIL_OBJECT_NAMESPACE
        ):
            return "images"
        return None

    def _presign_cdn_download(self, key: str, kind: str, config: dict) -> str:
        """Generate a Tencent CDN Type A URL without exposing the private key."""

        cdn = config["cdn"]
        if cdn.get("auth_type") != "type_a" or cdn.get("auth_parameter") != "sign":
            raise StorageConfigurationError("只支持腾讯 CDN Type A 鉴权")
        uri = f"/{quote(key, safe='/-._~')}"
        timestamp = int(time.time())
        random_value = secrets.token_hex(8)
        uid = "0"
        auth_key = self.store.cdn_auth_key(kind, config)
        if not auth_key:
            raise StorageConfigurationError("CDN 鉴权密钥尚未配置")
        digest = hashlib.md5(
            f"{uri}-{timestamp}-{random_value}-{uid}-{auth_key}".encode("utf-8"),
            usedforsecurity=False,
        ).hexdigest()
        signature = f"{timestamp}-{random_value}-{uid}-{digest}"
        base_url = str(cdn[f"{kind}_base_url"]).rstrip("/")
        return f"{base_url}{uri}?sign={signature}"

    def download_url_ttl(
        self,
        file_path: str,
        *,
        attachment: bool = False,
        use_cdn: bool = True,
    ) -> int:
        config = self.store.load()
        if not self.is_cloud_path(file_path):
            return 0
        bucket, key = self._split_cloud_path(file_path)
        kind = self._cdn_kind(key)
        if (
            use_cdn
            and not attachment
            and bucket == config["cos"]["bucket"]
            and kind
            and self.store.cdn_ready(config, kind)
        ):
            return int(config["cdn"]["auth_ttl_seconds"])
        return int(config["cos"]["get_ttl_seconds"])

    def test_connection(self) -> None:
        config = self.store.load()
        if config["provider"] != "cos":
            return
        prefix = str(config["cos"].get("prefix", "")).strip("/")
        self._client(config).list_objects_v2(
            Bucket=config["cos"]["bucket"],
            Prefix=f"{prefix}/" if prefix else "",
            MaxKeys=1,
        )


Storage = TurtleStorageProvider()
