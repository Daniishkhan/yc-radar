"""Bounded disk-backed HTTP cache shared by local pipeline scripts.

The old JSON files are retained as read-only compatibility sources.  Migration scans
one top-level entry at a time and never deserializes the whole legacy object.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

CACHE_SCHEMA_VERSION = 1
LEGACY_CHUNK_BYTES = 64 * 1024
# A legacy response can contain the historical 500 KiB text cap plus metadata. Keep
# a generous fixed ceiling so malformed legacy JSON cannot grow the working buffer
# with the remainder of a multi-gigabyte cache file.
LEGACY_ENTRY_MAX_CHARS = 2 * 1024 * 1024
_LOCK_STRIPES = 64


class DiskHttpCache:
    """Content-addressed response cache with atomic per-entry publication."""

    def __init__(self, root: Path, *, legacy_path: Path | None = None) -> None:
        self.root = root
        self.entries_dir = root / "entries"
        self.bodies_dir = root / "bodies"
        self.legacy_path = legacy_path
        self._locks = [threading.Lock() for _ in range(_LOCK_STRIPES)]
        self._legacy_migration_lock = threading.Lock()
        self._legacy_migration_attempted = False
        self.metrics: dict[str, int] = {
            "corrupt_entries": 0,
            "hits": 0,
            "legacy_hits": 0,
            "legacy_migrated": 0,
            "misses": 0,
            "stores": 0,
        }

    def load(self, url: str, *, allow_retryable: bool = False) -> dict[str, Any] | None:
        """Load one usable response; malformed/incomplete entries are cache misses."""
        key = self.key_for_url(url)
        entry = self._load_entry(key)
        if entry is None and self.legacy_path and self.legacy_path.exists():
            self.migrate_legacy_cache()
            entry = self._load_entry(key)
            if entry is not None:
                self.metrics["legacy_hits"] += 1
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

    def migrate_legacy_cache(self) -> int:
        """Stream every legacy entry to disk at most once per legacy file revision.

        Migrating the full file once avoids an O(requests × legacy-size) lazy scan for
        new URLs while retaining bounded memory. Interrupted or malformed legacy data
        leaves already published entries usable and is retried only by a later process.
        """
        if not self.legacy_path or not self.legacy_path.exists():
            return 0
        if self._legacy_migration_attempted:
            return 0
        with self._legacy_migration_lock:
            if self._legacy_migration_attempted:
                return 0
            self._legacy_migration_attempted = True
            signature = self._legacy_signature()
            marker_path = self.root / "legacy-migration.json"
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
                marker = None
            if marker == signature or (
                isinstance(marker, dict) and marker.get("legacy") == signature
            ):
                return 0

            migrated_count = 0
            try:
                for url, value in stream_legacy_cache(self.legacy_path):
                    if not isinstance(value, dict):
                        continue
                    migrated = dict(value)
                    status = migrated.get("status_code")
                    migrated.setdefault(
                        "retryable",
                        bool(migrated.get("error"))
                        or status in {408, 425, 429, 500, 502, 503, 504},
                    )
                    migrated.setdefault("attempt_count", 1)
                    key = self.key_for_url(url)
                    entry_path = self.entries_dir / key[:2] / f"{key}.json"
                    if not entry_path.exists():
                        self.store(url, metadata=migrated, text=str(migrated.get("text") or ""))
                    migrated_count += 1
            except OSError:
                self.metrics["corrupt_entries"] += 1
                self.metrics["legacy_migrated"] += migrated_count
                return migrated_count
            except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
                # Preserve every valid prefix entry and mark this exact corrupt file
                # revision as exhausted. A changed legacy file gets a new signature.
                self.metrics["corrupt_entries"] += 1
                self.metrics["legacy_migrated"] += migrated_count
                self._atomic_write_json(
                    marker_path,
                    {
                        "schema_version": CACHE_SCHEMA_VERSION,
                        "legacy": signature,
                        "complete": False,
                        "migrated_count": migrated_count,
                        "error_class": type(exc).__name__,
                        "error": str(exc)[:500],
                    },
                )
                return migrated_count

            self.metrics["legacy_migrated"] += migrated_count
            self._atomic_write_json(
                marker_path,
                {
                    "schema_version": CACHE_SCHEMA_VERSION,
                    "legacy": signature,
                    "complete": True,
                    "migrated_count": migrated_count,
                },
            )
            return migrated_count

    def _legacy_signature(self) -> dict[str, Any]:
        assert self.legacy_path is not None
        stat = self.legacy_path.stat()
        return {
            "path": str(self.legacy_path.resolve()),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }

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


def stream_legacy_cache(
    path: Path,
    *,
    max_entry_chars: int = LEGACY_ENTRY_MAX_CHARS,
) -> Iterator[tuple[str, Any]]:
    """Yield legacy top-level object members without loading the JSON file at once.

    Old cache entries are capped by the callers, so the working buffer is bounded by
    a single entry plus one read chunk rather than the number of cached URLs.
    """
    decoder = json.JSONDecoder()
    with path.open("r", encoding="utf-8") as source:
        buffer = ""
        position = 0
        eof = False

        def refill() -> bool:
            nonlocal buffer, eof, position
            if eof:
                return False
            # Once the parser needs more input, only the incomplete current token
            # is relevant. Compacting here bounds memory to one token plus a chunk.
            if position:
                buffer = buffer[position:]
                position = 0
            chunk = source.read(LEGACY_CHUNK_BYTES)
            if not chunk:
                eof = True
                return False
            if len(buffer) + len(chunk) > max_entry_chars:
                raise ValueError("legacy cache entry exceeds bounded migration limit")
            buffer += chunk
            return True

        def skip_space() -> None:
            nonlocal position
            while True:
                while position < len(buffer) and buffer[position].isspace():
                    position += 1
                if position < len(buffer) or not refill():
                    return

        def decode_next() -> Any:
            nonlocal position
            while True:
                try:
                    value, position = decoder.raw_decode(buffer, position)
                    return value
                except json.JSONDecodeError:
                    if not refill():
                        raise

        skip_space()
        if position >= len(buffer) or buffer[position] != "{":
            raise ValueError("legacy cache is not a JSON object")
        position += 1
        while True:
            skip_space()
            if position >= len(buffer):
                raise ValueError("unexpected EOF in legacy cache")
            if buffer[position] == "}":
                position += 1
                break
            key = decode_next()
            if not isinstance(key, str):
                raise ValueError("legacy cache key is not a string")
            skip_space()
            if position >= len(buffer) or buffer[position] != ":":
                raise ValueError("legacy cache key has no value separator")
            position += 1
            skip_space()
            value = decode_next()
            yield key, value
            skip_space()
            if position >= len(buffer):
                raise ValueError("unexpected EOF after legacy cache value")
            separator = buffer[position]
            position += 1
            if separator == "}":
                break
            if separator != ",":
                raise ValueError("legacy cache value has no item separator")
            if position > LEGACY_CHUNK_BYTES:
                buffer = buffer[position:]
                position = 0
        skip_space()
        if position < len(buffer):
            raise ValueError("trailing data in legacy cache")
