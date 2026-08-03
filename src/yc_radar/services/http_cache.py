"""Bounded disk-backed HTTP cache shared by local source scripts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

CACHE_SCHEMA_VERSION = 1
_LOCK_STRIPES = 64


class DiskHttpCache:
    """Content-addressed response cache with atomic per-entry publication."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.entries_dir = root / "entries"
        self.bodies_dir = root / "bodies"
        self._locks = [threading.Lock() for _ in range(_LOCK_STRIPES)]
        self.metrics: dict[str, int] = {
            "corrupt_entries": 0,
            "hits": 0,
            "misses": 0,
            "stores": 0,
        }

    def load(self, url: str, *, allow_retryable: bool = False) -> dict[str, Any] | None:
        """Load one usable response; malformed/incomplete entries are cache misses."""
        key = self.key_for_url(url)
        entry = self._load_entry(key)
        if entry is None:
            self.metrics["misses"] += 1
            return None
        if bool(entry.get("retryable")) and not allow_retryable:
            self.metrics["misses"] += 1
            return None
        self.metrics["hits"] += 1
        return entry

    def store(self, url: str, *, metadata: Mapping[str, Any], text: str) -> None:
        """Atomically publish a body blob then its small URL metadata entry."""
        key = self.key_for_url(url)
        body = text.encode("utf-8", errors="replace")
        body_hash = hashlib.sha256(body).hexdigest()
        body_path = self.bodies_dir / body_hash[:2] / f"{body_hash}.txt"
        entry_path = self.entries_dir / key[:2] / f"{key}.json"
        lock = self._locks[int(key[:2], 16) % len(self._locks)]
        with lock:
            if not body_path.exists():
                self._atomic_write_bytes(body_path, body)
            entry = {
                "schema_version": CACHE_SCHEMA_VERSION,
                "requested_url": url,
                "content_hash": body_hash,
                **{
                    name: value
                    for name, value in dict(metadata).items()
                    if name not in {"text", "content_hash", "schema_version", "requested_url"}
                },
            }
            self._atomic_write_json(entry_path, entry)
        self.metrics["stores"] += 1

    @staticmethod
    def key_for_url(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8", errors="surrogatepass")).hexdigest()

    def _load_entry(self, key: str) -> dict[str, Any] | None:
        path = self.entries_dir / key[:2] / f"{key}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("schema_version") != CACHE_SCHEMA_VERSION:
                raise ValueError("unknown cache entry schema")
            content_hash = raw.get("content_hash")
            if (
                not isinstance(content_hash, str)
                or len(content_hash) != 64
                or any(character not in "0123456789abcdef" for character in content_hash)
            ):
                raise ValueError("missing content hash")
            body_path = self.bodies_dir / content_hash[:2] / f"{content_hash}.txt"
            body = body_path.read_bytes()
            if hashlib.sha256(body).hexdigest() != content_hash:
                raise ValueError("body hash mismatch")
            return {**raw, "text": body.decode("utf-8")}
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            self.metrics["corrupt_entries"] += 1
            return None

    @staticmethod
    def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
        DiskHttpCache._atomic_write_bytes(
            path,
            (json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str) + "\n").encode(),
        )

    @staticmethod
    def _atomic_write_bytes(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
            try:
                directory = os.open(path.parent, os.O_DIRECTORY)
            except (AttributeError, OSError):
                return
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
