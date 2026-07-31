import pytest

from yc_radar.adapters.ashby import AshbyAdapter
from yc_radar.adapters.greenhouse import GreenhouseAdapter
from yc_radar.services.job_source_registry import (
    JobSourceProviderRegistry,
    UnknownJobSourceProvider,
)


def test_provider_registry_detects_supported_sources_without_yc_context() -> None:
    registry = JobSourceProviderRegistry([GreenhouseAdapter(), AshbyAdapter()])

    greenhouse = registry.detect("https://job-boards.greenhouse.io/acme/jobs/42")
    ashby = registry.detect("https://jobs.ashbyhq.com/other/jobs/42")

    assert registry.providers == ("ashby", "greenhouse")
    assert greenhouse is not None
    assert greenhouse.provider == "greenhouse"
    assert greenhouse.external_source_id == "acme"
    assert greenhouse.canonical_url == "https://job-boards.greenhouse.io/acme"
    assert ashby is not None
    assert ashby.provider == "ashby"
    assert ashby.external_source_id == "other"
    assert ashby.canonical_url == "https://jobs.ashbyhq.com/other"


def test_provider_registry_rejects_unknown_provider_and_url() -> None:
    registry = JobSourceProviderRegistry([GreenhouseAdapter(), AshbyAdapter()])

    assert registry.detect("https://jobs.example.com/acme") is None
    with pytest.raises(UnknownJobSourceProvider, match="unsupported"):
        registry.adapter_for("lever")


def test_provider_registry_refuses_duplicate_provider_registration() -> None:
    registry = JobSourceProviderRegistry([GreenhouseAdapter()])

    with pytest.raises(ValueError, match="already registered"):
        registry.register(GreenhouseAdapter())
