from pathlib import Path


def test_python_scripts_do_not_live_at_repo_root() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    assert list(repo_root.glob("*.py")) == []
