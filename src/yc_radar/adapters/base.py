from __future__ import annotations

from typing import Protocol

from yc_radar.domain.job_sources import SourceSnapshot


class JobSourceAdapter(Protocol):
    """Read-only adapter contract for a complete provider source snapshot."""

    provider: str
    adapter_version: str

    def extract_board_token(self, url: str) -> str | None: ...

    async def fetch_snapshot(self, external_source_id: str) -> SourceSnapshot: ...
