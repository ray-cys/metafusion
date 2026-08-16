import copy
import os
import subprocess
import sys
from pathlib import Path

import pytest

from helper.config import (
    ConfigError,
    DEFAULT_CONFIG,
    config_source_report,
    load_config_file,
    validate_config,
)


REPO_ROOT = Path(__file__).parents[1]


def valid_config():
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["plex"]["token"] = "valid-plex-token"
    config["tmdb"]["api_key"] = "valid-tmdb-key"
    return config


def test_secret_files_are_supported_and_direct_environment_wins(tmp_path):
    plex_file = tmp_path / "plex-token"
    tmdb_file = tmp_path / "tmdb-key"
    plex_file.write_text("file-plex-token\n", encoding="utf-8")
    tmdb_file.write_text("file-tmdb-key\n", encoding="utf-8")

    config, sources = load_config_file(
        config_file=tmp_path / "missing.yml",
        environ={
            "PLEX_TOKEN_FILE": str(plex_file),
            "TMDB_API_KEY_FILE": str(tmdb_file),
        },
        return_sources=True,
    )
    assert config["plex"]["token"] == "file-plex-token"
    assert config["tmdb"]["api_key"] == "file-tmdb-key"
    assert sources[("plex", "token")] == "PLEX_TOKEN_FILE"

    config = load_config_file(
        config_file=tmp_path / "missing.yml",
        environ={
            "PLEX_TOKEN": "direct-token",
            "PLEX_TOKEN_FILE": str(plex_file),
            "TMDB_API_KEY_FILE": str(tmdb_file),
        },
    )
    assert config["plex"]["token"] == "direct-token"


def test_unreadable_or_empty_secret_file_fails_safely(tmp_path):
    with pytest.raises(ConfigError, match="Unable to read PLEX_TOKEN_FILE"):
        load_config_file(
            config_file=tmp_path / "missing.yml",
            environ={"PLEX_TOKEN_FILE": str(tmp_path / "absent")},
        )

    empty = tmp_path / "empty"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="is empty"):
        load_config_file(
            config_file=tmp_path / "missing.yml",
            environ={"TMDB_API_KEY_FILE": str(empty)},
        )


def test_config_validation_reports_actionable_errors_without_secrets():
    config = valid_config()
    config["settings"]["mode"] = "invalid"
    config["settings"]["run_times"] = ["25:99"]
    config["runtime"]["max_concurrency"] = 0
    config["poster_set"]["min_width"] = 3000
    config["poster_set"]["max_width"] = 1000

    errors = validate_config(config)

    assert any("settings.mode" in error for error in errors)
    assert any("25:99" in error for error in errors)
    assert any("max_concurrency" in error for error in errors)
    assert any("poster_set.min_width" in error for error in errors)
    assert "valid-plex-token" not in repr(errors)
    assert validate_config(valid_config()) == []


def test_invalid_environment_conversion_is_not_silently_accepted(tmp_path):
    config = load_config_file(
        config_file=tmp_path / "missing.yml",
        environ={
            "PLEX_TOKEN": "token",
            "TMDB_API_KEY": "key",
            "MAX_CONCURRENCY": "many",
        },
    )

    assert any("MAX_CONCURRENCY" in error for error in validate_config(config))


def test_malformed_yaml_is_a_validation_error_even_with_environment_secrets(tmp_path):
    config_file = tmp_path / "config.yml"
    config_file.write_text("settings: [broken", encoding="utf-8")

    config = load_config_file(
        config_file=config_file,
        environ={"PLEX_TOKEN": "token", "TMDB_API_KEY": "key"},
    )

    assert any("Unable to parse configuration YAML" in error for error in validate_config(config))


def test_config_source_report_redacts_secret_values(tmp_path):
    config, sources = load_config_file(
        config_file=tmp_path / "missing.yml",
        environ={"PLEX_TOKEN": "never-print-this", "TMDB_API_KEY": "also-secret"},
        return_sources=True,
    )

    report = "\n".join(config_source_report(config, sources))

    assert "never-print-this" not in report
    assert "also-secret" not in report
    assert "plex.token: set (PLEX_TOKEN)" in report
    assert "tmdb.api_key: set (TMDB_API_KEY)" in report


def test_doctor_mode_is_non_writing_and_never_prints_secrets(tmp_path):
    config_dir = tmp_path / "config"
    environment = os.environ.copy()
    environment.update(
        {
            "CONFIG_DIR": str(config_dir),
            "PLEX_URL": "http://plex:32400",
            "PLEX_TOKEN": "doctor-plex-secret",
            "TMDB_API_KEY": "doctor-tmdb-secret",
            "RUN_MODE": "plex",
            "RUN_SCHEDULE": "false",
            "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),
        }
    )

    result = subprocess.run(
        [sys.executable, "metafusion.py", "--doctor"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Configuration is valid" in output
    assert "doctor-plex-secret" not in output
    assert "doctor-tmdb-secret" not in output
    assert not (config_dir / "config.yml").exists()
    assert not config_dir.exists()
