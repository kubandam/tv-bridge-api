from __future__ import annotations

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

_client = None


def _get_client():
    global _client
    if _client is None:
        from app.settings import settings
        _client = boto3.client(
            "s3",
            endpoint_url=f"https://{settings.account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=settings.access_key,
            aws_secret_access_key=settings.secret_access_key,
            config=Config(signature_version="s3v4"),
            region_name="auto",
        )
    return _client


def _bucket() -> str:
    from app.settings import settings
    return settings.r2_bucket_name


def upload_frame(image_bytes: bytes, key: str) -> None:
    _get_client().put_object(
        Bucket=_bucket(),
        Key=key,
        Body=image_bytes,
        ContentType="image/jpeg",
    )


def download_frame(key: str) -> bytes:
    resp = _get_client().get_object(Bucket=_bucket(), Key=key)
    return resp["Body"].read()


def delete_frame(key: str) -> None:
    try:
        _get_client().delete_object(Bucket=_bucket(), Key=key)
    except ClientError:
        pass


def delete_frames_batch(keys: list[str]) -> None:
    if not keys:
        return
    client = _get_client()
    bucket = _bucket()
    for i in range(0, len(keys), 1000):
        batch = keys[i : i + 1000]
        client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
        )
