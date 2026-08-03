from pathlib import Path

from yc_radar.services.http_cache import DiskHttpCache


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
