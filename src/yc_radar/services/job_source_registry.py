from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.engine import Engine

from yc_radar.adapters.ashby import AshbyAdapter
from yc_radar.adapters.base import JobSourceAdapter
from yc_radar.adapters.greenhouse import GreenhouseAdapter
from yc_radar.services.database import companies_table
from yc_radar.services.job_repository import JobRepository


class UnknownJobSourceProvider(ValueError):
    """Raised when no configured adapter owns a provider or URL."""


class AmbiguousJobSourceProvider(ValueError):
    """Raised when multiple adapters claim the same public URL."""


@dataclass(frozen=True)
class DetectedJobSource:
    provider: str
    source_kind: str
    external_id: str
    canonical_url: str
    observed_url: str


@dataclass(frozen=True)
class JobSourceRegistrationResult:
    company_source_id: int
    company_id: int
    provider: str
    external_id: str
    created: bool


class JobSourceProviderRegistry:
    """Small in-process catalog of public job-source adapters."""

    def __init__(self, adapters: Iterable[JobSourceAdapter] = ()) -> None:
        self._adapters: dict[str, JobSourceAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def register(self, adapter: JobSourceAdapter) -> None:
        provider = adapter.provider.strip().lower()
        if not provider:
            raise ValueError("job-source adapter provider is required")
        if provider in self._adapters:
            raise ValueError(f"job-source provider already registered: {provider}")
        self._adapters[provider] = adapter

    def adapter_for(self, provider: str) -> JobSourceAdapter:
        normalized = provider.strip().lower()
        try:
            return self._adapters[normalized]
        except KeyError as exc:
            raise UnknownJobSourceProvider(
                f"unsupported job-source provider: {normalized or provider}"
            ) from exc

    def detect(self, url: str, *, provider: str | None = None) -> DetectedJobSource | None:
        adapters = (
            [self.adapter_for(provider)] if provider is not None else list(self._adapters.values())
        )
        matches: list[DetectedJobSource] = []
        for adapter in adapters:
            external_id = adapter.extract_source_id(url)
            if external_id is None:
                continue
            matches.append(
                DetectedJobSource(
                    provider=adapter.provider,
                    source_kind=adapter.source_kind,
                    external_id=external_id,
                    canonical_url=adapter.canonical_source_url(external_id),
                    observed_url=url,
                )
            )
        if len(matches) > 1:
            providers = ", ".join(sorted(match.provider for match in matches))
            raise AmbiguousJobSourceProvider(f"multiple providers matched URL: {providers}")
        return matches[0] if matches else None


def default_job_source_providers() -> JobSourceProviderRegistry:
    return JobSourceProviderRegistry([GreenhouseAdapter(), AshbyAdapter()])


class JobSourceRegistry:
    """Attach supported job sources directly to canonical companies."""

    def __init__(
        self,
        engine: Engine,
        *,
        providers: JobSourceProviderRegistry | None = None,
    ) -> None:
        self.engine = engine
        self.providers = providers or default_job_source_providers()
        self.repository = JobRepository(engine)

    def register_url(
        self,
        *,
        company_id: int,
        source_url: str,
        provider: str | None = None,
        discovered_from_url: str | None = None,
        evidence: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> JobSourceRegistrationResult:
        detected = self.providers.detect(source_url, provider=provider)
        if detected is None:
            qualifier = f" for provider {provider}" if provider else ""
            raise UnknownJobSourceProvider(f"unsupported job-source URL{qualifier}: {source_url}")
        with self.engine.connect() as connection:
            company_exists = connection.scalar(
                select(companies_table.c.id).where(companies_table.c.id == company_id)
            )
        if company_exists is None:
            raise ValueError(f"unknown company_id: {company_id}")

        source, allowed, created = self.repository.register_source(
            company_id=company_id,
            provider=detected.provider,
            source_kind=detected.source_kind,
            external_id=detected.external_id,
            source_url=detected.canonical_url,
            sync_mode="complete_snapshot",
            now=now or datetime.now(UTC),
            metadata={
                "observed_url": detected.observed_url,
                "discovered_from_url": discovered_from_url or detected.observed_url,
                "evidence": evidence or {},
            },
        )
        if not allowed:
            raise ValueError(
                f"{detected.provider} source {detected.external_id} already belongs "
                f"to company_id={source['company_id']}"
            )
        return JobSourceRegistrationResult(
            company_source_id=int(source["id"]),
            company_id=company_id,
            provider=detected.provider,
            external_id=detected.external_id,
            created=created,
        )
