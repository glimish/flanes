"""
Remote Storage

Abstract backend + S3/GCS (optional deps, lazy import) + local cache.
Provides sync between a local ContentStore and a remote backend.
"""

import logging
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)


class RemoteBackend(ABC):
    """Abstract interface for remote storage backends."""

    @abstractmethod
    def upload(self, key: str, data: bytes) -> None:
        """Upload data to the remote backend."""

    @abstractmethod
    def download(self, key: str) -> bytes | None:
        """Download data from the remote backend. Returns None if not found."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Check if a key exists in the remote backend."""

    @abstractmethod
    def list_keys(self, prefix: str = "") -> list:
        """List all keys with the given prefix."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete a key from the remote backend."""


class InMemoryBackend(RemoteBackend):
    """In-memory backend for testing."""

    def __init__(self):
        self.data = {}

    def upload(self, key: str, data: bytes) -> None:
        self.data[key] = data

    def download(self, key: str) -> bytes | None:
        return self.data.get(key)

    def exists(self, key: str) -> bool:
        return key in self.data

    def list_keys(self, prefix: str = "") -> list:
        return sorted(k for k in self.data if k.startswith(prefix))

    def delete(self, key: str) -> None:
        self.data.pop(key, None)


class S3Backend(RemoteBackend):
    """S3 backend (requires boto3)."""

    def __init__(self, bucket: str, prefix: str = "", region: str = "us-east-1"):
        try:
            import boto3
            import botocore.exceptions  # noqa: F401
        except ImportError:
            raise ImportError(
                "boto3 is required for S3 remote storage. "
                "Install it with: pip install boto3"
            )
        self.bucket = bucket
        self.prefix = prefix
        self.s3 = boto3.client("s3", region_name=region)
        self._client_error = botocore.exceptions.ClientError

    def _key(self, key: str) -> str:
        return f"{self.prefix}{key}" if self.prefix else key

    def upload(self, key: str, data: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def download(self, key: str) -> bytes | None:
        try:
            resp = self.s3.get_object(Bucket=self.bucket, Key=self._key(key))
            return resp["Body"].read()
        except self._client_error as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise

    def exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except self._client_error as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return False
            raise

    def list_keys(self, prefix: str = "") -> list:
        full_prefix = self._key(prefix)
        keys = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                k = obj["Key"]
                if self.prefix:
                    k = k[len(self.prefix):]
                keys.append(k)
        return sorted(keys)

    def delete(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=self._key(key))


class GCSBackend(RemoteBackend):
    """Google Cloud Storage backend (requires google-cloud-storage)."""

    def __init__(self, bucket: str, prefix: str = ""):
        try:
            from google.cloud import storage
        except ImportError:
            raise ImportError(
                "google-cloud-storage is required for GCS remote storage. "
                "Install it with: pip install google-cloud-storage"
            )
        self.client = storage.Client()
        self.bucket_obj = self.client.bucket(bucket)
        self.prefix = prefix

    def _key(self, key: str) -> str:
        return f"{self.prefix}{key}" if self.prefix else key

    def upload(self, key: str, data: bytes) -> None:
        blob = self.bucket_obj.blob(self._key(key))
        blob.upload_from_string(data)

    def download(self, key: str) -> bytes | None:
        from google.cloud import exceptions as gcs_exceptions
        blob = self.bucket_obj.blob(self._key(key))
        try:
            return blob.download_as_bytes()
        except gcs_exceptions.NotFound:
            return None

    def exists(self, key: str) -> bool:
        blob = self.bucket_obj.blob(self._key(key))
        return blob.exists()

    def list_keys(self, prefix: str = "") -> list:
        full_prefix = self._key(prefix)
        keys = []
        for blob in self.bucket_obj.list_blobs(prefix=full_prefix):
            k = blob.name
            if self.prefix:
                k = k[len(self.prefix):]
            keys.append(k)
        return sorted(keys)

    def delete(self, key: str) -> None:
        blob = self.bucket_obj.blob(self._key(key))
        blob.delete()


class LocalCacheLayer:
    """Local disk cache in front of a remote backend."""

    def __init__(self, backend: RemoteBackend, cache_dir: Path, max_cache_bytes: int = 1_073_741_824):
        self.backend = backend
        self.cache_dir = cache_dir
        self.max_cache_bytes = max_cache_bytes
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, key: str) -> Path:
        # Use first 2 chars as directory prefix to avoid too many files in one dir
        return self.cache_dir / key[:2] / key

    def get(self, key: str) -> bytes | None:
        """Get data, checking cache first then remote."""
        cached = self._cache_path(key)
        if cached.exists():
            return cached.read_bytes()

        data = self.backend.download(key)
        if data is not None:
            self._cache_put(key, data)
        return data

    def put(self, key: str, data: bytes) -> None:
        """Put data to both remote and cache."""
        self.backend.upload(key, data)
        self._cache_put(key, data)

    def _cache_put(self, key: str, data: bytes):
        path = self._cache_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write via temp file + rename
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".cache.")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
            Path(tmp_path).replace(path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


class RemoteSyncManager:
    """Sync objects between a local ContentStore and a remote backend."""

    def __init__(self, store, backend: RemoteBackend, cache_dir: Path):
        self.store = store
        self.backend = backend
        self.cache = LocalCacheLayer(backend, cache_dir)

    def push(self, hashes: list | None = None) -> dict:
        """Push objects from local store to remote.

        Each remote object is stored as a type-prefixed payload:
        ``<type>\\n<data>`` so that pull can reconstruct the correct object type.
        """
        if hashes is None:
            hashes = self._all_local_hashes()

        pushed = 0
        skipped = 0
        for h in hashes:
            if self.backend.exists(h):
                skipped += 1
                continue
            obj = self.store.retrieve(h)
            if obj is not None:
                # Prefix data with object type so pull can reconstruct correctly
                payload = obj.type.value.encode("utf-8") + b"\n" + obj.data
                self.cache.put(h, payload)
                pushed += 1

        return {"pushed": pushed, "skipped": skipped, "total": len(hashes)}

    def pull(self, hashes: list | None = None) -> dict:
        """Pull objects from remote to local store.

        Fix #7 from audit: Verifies downloaded payload hash matches expected key
        before storing, preventing silent corruption from malicious/broken backends.
        """
        from .cas import ObjectType

        if hashes is None:
            hashes = self.backend.list_keys()

        pulled = 0
        skipped = 0
        errors = 0
        integrity_failures = 0

        for h in hashes:
            if self.store.exists(h):
                skipped += 1
                continue
            payload = self.cache.get(h)
            if payload is not None:
                # Parse type-prefixed payload: "<type>\n<data>"
                newline_idx = payload.find(b"\n")
                if newline_idx > 0:
                    type_str = payload[:newline_idx].decode("utf-8", errors="replace")
                    data = payload[newline_idx + 1:]
                    try:
                        obj_type = ObjectType(type_str)
                    except ValueError:
                        obj_type = ObjectType.BLOB
                else:
                    # Legacy format (no type prefix) — assume blob
                    data = payload
                    obj_type = ObjectType.BLOB

                # Fix #7: Verify hash before storing
                # Compute expected hash using same algorithm as ContentStore
                computed_hash = self.store.hash_content(data, obj_type)
                if computed_hash != h:
                    logger.warning(
                        "Integrity check failed for %s: expected hash %s, got %s. "
                        "Payload corrupted or malicious — skipping.",
                        h[:12], h[:12], computed_hash[:12]
                    )
                    integrity_failures += 1
                    continue

                self.store.store(data, obj_type)
                pulled += 1
            else:
                errors += 1

        return {
            "pulled": pulled,
            "skipped": skipped,
            "errors": errors,
            "integrity_failures": integrity_failures,
            "total": len(hashes),
        }

    def status(self) -> dict:
        """Compare local and remote objects."""
        local_hashes = set(self._all_local_hashes())
        remote_hashes = set(self.backend.list_keys())

        return {
            "local_only": sorted(local_hashes - remote_hashes),
            "remote_only": sorted(remote_hashes - local_hashes),
            "synced": sorted(local_hashes & remote_hashes),
        }

    def _all_local_hashes(self) -> list:
        """Get all object hashes from the local store."""
        rows = self.store.conn.execute("SELECT hash FROM objects").fetchall()
        return [r[0] for r in rows]


def create_backend(config: dict) -> RemoteBackend:
    """Create a remote backend from config."""
    remote_config = config.get("remote_storage", {})
    backend_type = remote_config.get("type", "")

    if backend_type == "s3":
        return S3Backend(
            bucket=remote_config["bucket"],
            prefix=remote_config.get("prefix", ""),
            region=remote_config.get("region", "us-east-1"),
        )
    elif backend_type == "gcs":
        return GCSBackend(
            bucket=remote_config["bucket"],
            prefix=remote_config.get("prefix", ""),
        )
    elif backend_type == "memory":
        return InMemoryBackend()
    else:
        raise ValueError(f"Unknown remote storage type: {backend_type!r}")
