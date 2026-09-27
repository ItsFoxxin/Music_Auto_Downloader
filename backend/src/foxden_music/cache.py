from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .database import Database
from .models import MetadataCache


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class DatabaseJsonCache:
    def __init__(self, database: Database):
        self.database = database

    def get_json(self, namespace: str, key: str) -> Mapping[str, Any] | None:
        cache_key = f"{namespace}:{key}"
        with self.database.session() as session:
            entry = session.get(MetadataCache, cache_key)
            if entry is None:
                return None
            if _aware(entry.expires_at) <= datetime.now(timezone.utc):
                session.delete(entry)
                return None
            try:
                payload = json.loads(entry.payload_json)
            except json.JSONDecodeError:
                session.delete(entry)
                return None
            return payload if isinstance(payload, Mapping) else None

    def put_json(
        self,
        namespace: str,
        key: str,
        payload: Mapping[str, Any],
        ttl: timedelta,
    ) -> None:
        cache_key = f"{namespace}:{key}"
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.database.session() as session:
            entry = session.get(MetadataCache, cache_key)
            if entry is None:
                entry = MetadataCache(
                    cache_key=cache_key,
                    namespace=namespace,
                    payload_json=encoded,
                    expires_at=datetime.now(timezone.utc) + ttl,
                )
                session.add(entry)
            else:
                entry.payload_json = encoded
                entry.expires_at = datetime.now(timezone.utc) + ttl


class ArtworkFileCache:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, namespace: str, key: str) -> Path:
        safe_key = "".join(character for character in key.lower() if character in "0123456789abcdef-")
        if not safe_key or safe_key != key.lower():
            raise ValueError("Unsafe artwork cache key")
        return self.root / namespace / f"{safe_key}.jpg"

    def get_bytes(self, namespace: str, key: str) -> bytes | None:
        path = self._path(namespace, key)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def put_bytes(self, namespace: str, key: str, payload: bytes, ttl: timedelta) -> None:
        del ttl
        path = self._path(namespace, key)
        path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}-{os.getpid()}.part")
        try:
            with temporary.open("xb") as output:
                os.chmod(temporary, 0o600)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.rename(temporary, path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)

