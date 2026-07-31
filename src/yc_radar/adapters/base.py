from __future__ import annotations

from typing import Protocol

from yc_radar.domain.job_sources import SourceSnapshot


class JobSourceAdapter(Protocol):
    """Read-only adapter contract for a complete provider source snapshot."""

    provider: str
    adapter_version: str
    source_kind: str

    def extract_source_id(self, url: str) -> str | None: ...

    def canonical_source_url(self, external_source_id: str) -> str: ...

    async def fetch_snapshot(self, external_source_id: str) -> SourceSnapshot: ...
