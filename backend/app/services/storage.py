"""
Storage service — local filesystem (default) or AWS S3.

Set USE_LOCAL_STORAGE=false and provide AWS credentials to switch to S3.
In local mode files are saved under LOCAL_STORAGE_PATH and served via
FastAPI StaticFiles at /uploads/...
"""

import logging
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


def _local_path(key: str) -> Path:
    return Path(settings.LOCAL_STORAGE_PATH) / key


def _local_url(key: str) -> str:
    return f"{settings.BACKEND_URL}/uploads/{key}"


# ── Local storage ─────────────────────────────────────────────────────────────

class LocalStorageService:
    """Store files on the local filesystem and serve them via /uploads/."""

    async def initialize(self):
        Path(settings.LOCAL_STORAGE_PATH).mkdir(parents=True, exist_ok=True)
        logger.info(f"Local storage initialised at {settings.LOCAL_STORAGE_PATH}")

    async def upload_file(
        self,
        file_data: bytes,
        key: str,
        content_type: str = "application/octet-stream",
        metadata: Optional[dict] = None,
    ) -> str:
        dest = _local_path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(file_data)
        url = _local_url(key)
        logger.debug(f"Saved {key} → {url}")
        return url

    async def download_file(self, key: str) -> bytes:
        p = _local_path(key)
        if not p.exists():
            raise FileNotFoundError(f"Local file not found: {key}")
        return p.read_bytes()

    def get_local_path(self, key: str) -> str:
        return str(_local_path(key).resolve())

    async def delete_file(self, key: str):
        _local_path(key).unlink(missing_ok=True)

    def get_url(self, key: str) -> str:
        return _local_url(key)

    async def cleanup(self):
        logger.info("Local storage cleanup complete")


# ── S3 storage ────────────────────────────────────────────────────────────────

class S3StorageService:
    """AWS S3 storage (used when USE_LOCAL_STORAGE=false)."""

    def __init__(self):
        import aioboto3
        import boto3

        self.bucket_name = settings.S3_BUCKET_NAME
        self.region = settings.AWS_REGION
        self.cloudfront_domain = settings.CLOUDFRONT_DOMAIN

        self.s3_client = boto3.client(
            "s3",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=self.region,
        )
        self.session = aioboto3.Session(
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=self.region,
        )

    async def initialize(self):
        from botocore.exceptions import ClientError
        try:
            async with self.session.client("s3") as s3:
                try:
                    await s3.head_bucket(Bucket=self.bucket_name)
                    logger.info(f"S3 bucket {self.bucket_name} exists")
                except ClientError:
                    logger.info(f"Creating S3 bucket {self.bucket_name}")
                    await s3.create_bucket(
                        Bucket=self.bucket_name,
                        CreateBucketConfiguration={"LocationConstraint": self.region},
                    )
        except Exception as e:
            logger.error(f"Failed to initialise S3: {e}")
            raise

    async def upload_file(self, file_data: bytes, key: str,
                          content_type: str = "application/octet-stream",
                          metadata: Optional[dict] = None) -> str:
        async with self.session.client("s3") as s3:
            extra: dict = {"ContentType": content_type, "ACL": "public-read"}
            if metadata:
                extra["Metadata"] = metadata
            await s3.put_object(Bucket=self.bucket_name, Key=key, Body=file_data, **extra)
        return self.get_url(key)

    async def download_file(self, key: str) -> bytes:
        async with self.session.client("s3") as s3:
            resp = await s3.get_object(Bucket=self.bucket_name, Key=key)
            async with resp["Body"] as stream:
                return await stream.read()

    def get_local_path(self, key: str) -> str:
        raise NotImplementedError("S3 has no local path; use download_file()")

    async def delete_file(self, key: str):
        async with self.session.client("s3") as s3:
            await s3.delete_object(Bucket=self.bucket_name, Key=key)

    def get_url(self, key: str) -> str:
        if self.cloudfront_domain:
            return f"https://{self.cloudfront_domain}/{key}"
        return f"https://{self.bucket_name}.s3.{self.region}.amazonaws.com/{key}"

    async def cleanup(self):
        logger.info("S3 storage cleanup complete")


# ── Factory ───────────────────────────────────────────────────────────────────

def _build_storage_service():
    if getattr(settings, "USE_LOCAL_STORAGE", True):
        logger.info("Using LOCAL filesystem storage")
        return LocalStorageService()
    logger.info("Using AWS S3 storage")
    return S3StorageService()


storage_service = _build_storage_service()
