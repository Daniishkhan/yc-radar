from pathlib import Path


def test_python_scripts_do_not_live_at_repo_root() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    assert list(repo_root.glob("*.py")) == []


def test_repo_does_not_expose_a_served_api_surface() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    assert not (repo_root / "src" / "yc_radar" / "api").exists()
    assert not (repo_root / "src" / "yc_radar" / "main.py").exists()

    dockerfile = (repo_root / "Dockerfile").read_text(encoding="utf-8")
    production_compose = (repo_root / "compose.prod.yml").read_text(encoding="utf-8")
    assert "\nEXPOSE " not in dockerfile
    assert "\n    ports:" not in production_compose


def test_distribution_and_container_do_not_expose_the_retired_company_search_cli() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    project = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    dockerfile = (repo_root / "Dockerfile").read_text(encoding="utf-8")

    assert "yc-radar-targets" not in project
    assert "yc_radar.cli" not in project
    assert "yc_radar.cli" not in dockerfile
    assert 'CMD ["python", "scripts/sync_job_sources.py", "--help"]' in dockerfile
