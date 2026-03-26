"""S3 blob store for production."""

from __future__ import annotations

import asyncio
from functools import partial

import boto3
from botocore.exceptions import ClientError


class S3BlobStore:
    """Stores blobs in S3 (or S3-compatible storage)."""

    def __init__(
        self,
        bucket: str,
        region: str = "us-east-1",
        endpoint_url: str | None = None,
        prefix: str = "",
    ):
        # Handle bucket names containing a path prefix (e.g. "my-bucket/path")
        if "/" in bucket:
            bucket, extra_prefix = bucket.split("/", 1)
            prefix = extra_prefix.rstrip("/") + "/" + prefix if prefix else extra_prefix.rstrip("/") + "/"

        self.bucket = bucket
        self.prefix = prefix
        kwargs: dict = {"region_name": region}
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        self._client = boto3.client("s3", **kwargs)

    def _key(self, key: str) -> str:
        """Prepend the configured prefix to a storage key."""
        return f"{self.prefix}{key}" if self.prefix else key

    async def _run(self, func, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, partial(func, *args, **kwargs))

    async def put(self, key: str, data: bytes) -> None:
        await self._run(self._client.put_object, Bucket=self.bucket, Key=self._key(key), Body=data)

    async def get(self, key: str) -> bytes | None:
        try:
            resp = await self._run(self._client.get_object, Bucket=self.bucket, Key=self._key(key))
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, resp["Body"].read)
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise

    async def delete(self, key: str) -> None:
        await self._run(self._client.delete_object, Bucket=self.bucket, Key=self._key(key))

    async def exists(self, key: str) -> bool:
        try:
            await self._run(self._client.head_object, Bucket=self.bucket, Key=self._key(key))
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            raise
