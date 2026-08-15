from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).parents[1]


def test_compose_defaults_are_safe_for_scheduler():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["metafusion"]
    environment = service["environment"]

    assert "version" not in compose
    assert service["init"] is True
    assert environment["METAFUSION_RUN"].endswith(":-False}")
    assert environment["RUN_PROCESS"].endswith(":-False}")
    assert ":?Set PLEX_TOKEN" in environment["PLEX_TOKEN"]
    assert ":?Set TMDB_API_KEY" in environment["TMDB_API_KEY"]


def test_dockerfile_uses_stable_python_minor_without_os_upgrade():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.startswith("FROM python:3.13-slim-bookworm")
    assert "apt-get upgrade" not in dockerfile
    assert "PIP_NO_CACHE_DIR=1" in dockerfile
