from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).parents[1]


def test_compose_defaults_are_safe_for_scheduler():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["metafusion"]
    environment = service["environment"]

    assert "version" not in compose
    assert service["init"] is True
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert "healthcheck" in service
    assert environment["METAFUSION_RUN"] == "${METAFUSION_RUN-}"
    assert environment["RUN_PROCESS"] == "${RUN_PROCESS-}"
    assert environment["PLEX_TOKEN"] == "${PLEX_TOKEN-}"
    assert environment["PLEX_TOKEN_FILE"] == "${PLEX_TOKEN_FILE-}"
    assert environment["TMDB_API_KEY"] == "${TMDB_API_KEY-}"
    assert environment["TMDB_API_KEY_FILE"] == "${TMDB_API_KEY_FILE-}"
    assert environment["PLEX_TIMEOUT"] == "${PLEX_TIMEOUT-}"
    assert environment["SHUTDOWN_TIMEOUT"] == "${SHUTDOWN_TIMEOUT-}"
    assert environment["INCREMENTAL"] == "${INCREMENTAL-}"
    assert environment["TMDB_CACHE_ENABLED"] == "${TMDB_CACHE_ENABLED-}"
    assert environment["VALIDATE_OUTPUT"] == "${VALIDATE_OUTPUT-}"
    assert environment["HEALTH_FAIL_ON_JOB_ERROR"] == "${HEALTH_FAIL_ON_JOB_ERROR:-False}"
    assert service["stop_grace_period"] == "${STOP_GRACE_PERIOD:-20s}"
    assert service["healthcheck"]["start_period"] == "20s"


def test_dockerfile_uses_stable_python_minor_without_os_upgrade():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.startswith("FROM python:3.13-slim-bookworm@sha256:")
    assert "apt-get upgrade" not in dockerfile
    assert "PIP_NO_CACHE_DIR=1" in dockerfile
    assert "--require-hashes -r requirements.lock" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "HEALTHCHECK" in dockerfile
