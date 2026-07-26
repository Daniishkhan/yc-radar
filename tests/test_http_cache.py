import json
from pathlib import Path

import pytest

from yc_radar.services.http_cache import DiskHttpCache, stream_legacy_cache


def test_cache_uses_content_addressed_atomic_entries_and_treats_corruption_as_miss(tmp_path: Path) -> None:
    cache = DiskHttpCache(tmp_path / "cache")
    url_one = "https://example.com/careers"
    url_two = "https://example.com/jobs"
    cache.store(url_one, metadata={"status_code": 200, "final_url": url_one}, text="same body")
    cache.store(url_two, metadata={"status_code": 200, "final_url": url_two}, text="same body")

    bodies = list((tmp_path / "cache" / "bodies").glob("*/*.txt"))
    assert len(bodies) == 1
    assert cache.load(url_one)["text"] == "same body"

    entry_path = tmp_path / "cache" / "entries" / cache.key_for_url(url_one)[:2] / f"{cache.key_for_url(url_one)}.json"
    entry_path.write_text("{not-json", encoding="utf-8")
    assert cache.load(url_one) is None
    assert cache.metrics["corrupt_entries"] == 1


def test_cache_hashes_exact_body_bytes_without_newline_translation(tmp_path: Path) -> None:
    cache = DiskHttpCache(tmp_path / "cache")
    url = "https://example.com/robots.txt"

    cache.store(url, metadata={"status_code": 200}, text="one\r\ntwo\r\n")

    assert cache.load(url)["text"] == "one\r\ntwo\r\n"
    assert cache.metrics["corrupt_entries"] == 0


def test_legacy_cache_is_scanned_one_entry_at_a_time_and_retained(monkeypatch, tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.json"
    wanted = "https://example.com/careers"
    legacy.write_text(
        json.dumps(
            {
                "https://unused.example/": {"status_code": 200, "text": "unused"},
                wanted: {"status_code": 200, "final_url": wanted, "text": "wanted"},
            }
        ),
        encoding="utf-8",
    )
    original_read_text = Path.read_text

    def no_full_legacy_read(self: Path, *args, **kwargs):
        if self == legacy:
            raise AssertionError("legacy cache must be streamed, not read_text/json-loaded")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", no_full_legacy_read)
    cache = DiskHttpCache(tmp_path / "new-cache", legacy_path=legacy)

    cached = cache.load(wanted)

    assert cached is not None
    assert cached["text"] == "wanted"
    assert legacy.exists()
    assert cache.metrics["legacy_migrated"] == 2
    assert cache.load("https://unused.example/")["text"] == "unused"
    assert cache.metrics["legacy_migrated"] == 2
    assert list(stream_legacy_cache(legacy))[1][0] == wanted


def test_malformed_legacy_entry_is_rejected_at_the_per_entry_buffer_limit(tmp_path: Path) -> None:
    legacy = tmp_path / "truncated-legacy.json"
    legacy.write_text('{"broken":"' + ("x" * 4096), encoding="utf-8")

    with pytest.raises(ValueError, match="bounded migration limit"):
        list(stream_legacy_cache(legacy, max_entry_chars=512))

    cache = DiskHttpCache(tmp_path / "cache", legacy_path=legacy)
    assert cache.load("https://example.com/careers") is None
    assert cache.metrics["corrupt_entries"] == 1
    marker = json.loads((tmp_path / "cache" / "legacy-migration.json").read_text())
    assert marker["complete"] is False

    restarted = DiskHttpCache(tmp_path / "cache", legacy_path=legacy)
    assert restarted.load("https://example.com/careers") is None
    assert restarted.metrics["corrupt_entries"] == 0
