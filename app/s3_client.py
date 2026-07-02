"""Tải file gốc và upload ảnh đã tách lên S3 (tương thích S3, ví dụ Vietnix).

- Tải file: ưu tiên HTTP từ `fileUrl` (public); fallback boto3 get_object theo key.
- Upload: dùng boto3 put_object, trả về (url, key).
"""

from __future__ import annotations

import logging
import mimetypes
import uuid
from typing import Optional, Tuple

import boto3
import requests
from botocore.client import Config as BotoConfig

from .config import config

logger = logging.getLogger("ocr.s3")


class S3Client:
    def __init__(self) -> None:
        self._client = boto3.client(
            "s3",
            endpoint_url=config.s3_endpoint or None,
            region_name=config.s3_region or None,
            aws_access_key_id=config.s3_access_key or None,
            aws_secret_access_key=config.s3_secret_key or None,
            config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        self._bucket = config.s3_bucket

    # ── Download ──────────────────────────────────────────────────────────
    def download(self, file_url: str, file_key: Optional[str], dest_path: str) -> None:
        """Tải file gốc về `dest_path`. Thử HTTP trước, fallback boto3."""
        if file_url:
            try:
                self._download_http(file_url, dest_path)
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("Tải qua HTTP thất bại (%s), thử boto3...", exc)
        if file_key:
            self._client.download_file(self._bucket, file_key, dest_path)
            return
        raise RuntimeError("Không có cách nào tải file: thiếu fileUrl hợp lệ và fileKey")

    @staticmethod
    def _download_http(url: str, dest_path: str) -> None:
        with requests.get(url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(dest_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    if chunk:
                        fh.write(chunk)

    # ── Upload ────────────────────────────────────────────────────────────
    def upload_bytes(
        self,
        data: bytes,
        ext: str = "png",
        content_type: Optional[str] = None,
        prefix: Optional[str] = None,
    ) -> Tuple[str, str]:
        """Upload bytes lên S3. Trả về (public_url, key)."""
        base = (prefix or config.s3_prefix).rstrip("/")
        key = f"{base}/{uuid.uuid4().hex}.{ext.lstrip('.')}"
        ctype = content_type or mimetypes.guess_type(f"x.{ext}")[0] or "image/png"
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=ctype,
            ACL="public-read",
        )
        return self._public_url(key), key

    def _public_url(self, key: str) -> str:
        if config.s3_public_base:
            return f"{config.s3_public_base.rstrip('/')}/{key}"
        endpoint = (config.s3_endpoint or "").rstrip("/")
        return f"{endpoint}/{self._bucket}/{key}"
