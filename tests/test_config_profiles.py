import copy
from pathlib import Path

import pytest
import yaml

from helper import config as config_module
from helper.config import (
    DEFAULT_CONFIG,
    ConfigError,
    config_source_overview,
    load_config_file,
)

REPO_ROOT = Path(__file__).parents[1]


def write_profile(path, mode, **sections):
    document = {"settings": {"mode": mode}, **sections}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def test_generated_run_type_profiles_are_complete_and_mode_specific():
    for mode in ("kometa", "plex"):
        profile = yaml.safe_load(
            (REPO_ROOT / "config" / "examples" / f"{mode}.yml").read_text(
                encoding="utf-8"
            )
        )
        expected = copy.deepcopy(DEFAULT_CONFIG)
        expected["settings"]["mode"] = mode
        expected["plex"]["token"] = "YOUR_PLEX_TOKEN"
        expected["tmdb"]["api_key"] = "YOUR_TMDB_API_KEY"
        assert profile == expected


def test_environment_only_start_needs_no_yaml(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "BASE_CONFIG_DIR", tmp_path)
    config, sources = load_config_file(
        environ={
            "RUN_MODE": "plex",
            "PLEX_URL": "http://plex:32400",
            "PLEX_TOKEN": "token",
            "TMDB_API_KEY": "key",
        },
        create_if_missing=False,
        return_sources=True,
    )

    overview = config_source_overview(config, sources)
    assert config["settings"]["mode"] == "plex"
    assert overview == {
        "config_file": "None",
        "selection": "environment and defaults",
        "yaml_values": 0,
        "environment_overrides": 4,
        "secret_file_overrides": 0,
        "cli_overrides": 0,
    }
    assert list(tmp_path.iterdir()) == []


def test_single_run_type_profile_is_selected_and_environment_overrides(monkeypatch, tmp_path):
    profile = tmp_path / "kometa.yml"
    write_profile(
        profile,
        "kometa",
        plex={"url": "http://yaml-plex:32400", "token": "yaml-token"},
        tmdb={"api_key": "yaml-key"},
    )
    monkeypatch.setattr(config_module, "BASE_CONFIG_DIR", tmp_path)

    config, sources = load_config_file(
        environ={"PLEX_URL": "http://env-plex:32400"},
        create_if_missing=False,
        return_sources=True,
    )

    overview = config_source_overview(config, sources)
    assert config["settings"]["mode"] == "kometa"
    assert config["plex"]["url"] == "http://env-plex:32400"
    assert overview["config_file"] == str(profile)
    assert overview["selection"] == "single run-type profile"
    assert overview["yaml_values"] == 3
    assert overview["environment_overrides"] == 1


def test_conventional_config_wins_over_run_type_profiles(monkeypatch, tmp_path):
    write_profile(tmp_path / "kometa.yml", "kometa")
    write_profile(tmp_path / "plex.yml", "plex")
    conventional = tmp_path / "config.yml"
    write_profile(conventional, "plex")
    monkeypatch.setattr(config_module, "BASE_CONFIG_DIR", tmp_path)

    config, sources = load_config_file(
        environ={}, create_if_missing=False, return_sources=True
    )

    assert config["settings"]["mode"] == "plex"
    assert config_source_overview(config, sources)["selection"] == (
        "conventional config.yml"
    )


def test_run_mode_selects_between_two_profiles(monkeypatch, tmp_path):
    write_profile(tmp_path / "kometa.yml", "kometa")
    write_profile(tmp_path / "plex.yml", "plex")
    monkeypatch.setattr(config_module, "BASE_CONFIG_DIR", tmp_path)

    config, sources = load_config_file(
        environ={"RUN_MODE": "plex"},
        create_if_missing=False,
        return_sources=True,
    )

    assert config["settings"]["mode"] == "plex"
    assert config_source_overview(config, sources)["selection"] == (
        "RUN_MODE-selected profile"
    )


def test_ambiguous_or_conflicting_run_type_profiles_fail(monkeypatch, tmp_path):
    write_profile(tmp_path / "kometa.yml", "kometa")
    write_profile(tmp_path / "plex.yml", "plex")
    monkeypatch.setattr(config_module, "BASE_CONFIG_DIR", tmp_path)

    with pytest.raises(ConfigError, match=r"Both /config/kometa\.yml"):
        load_config_file(environ={}, create_if_missing=False)

    (tmp_path / "plex.yml").unlink()
    with pytest.raises(ConfigError, match="conflicts with the only run-type"):
        load_config_file(environ={"RUN_MODE": "plex"}, create_if_missing=False)


def test_run_type_filename_and_declared_mode_must_agree(tmp_path):
    profile = tmp_path / "kometa.yml"
    write_profile(profile, "plex")

    with pytest.raises(ConfigError, match="expected 'kometa'"):
        load_config_file(config_file=profile, environ={}, create_if_missing=False)


def test_run_type_profile_settings_must_be_a_mapping(tmp_path):
    profile = tmp_path / "plex.yml"
    profile.write_text("settings: []\n", encoding="utf-8")

    config = load_config_file(
        config_file=profile, environ={}, create_if_missing=False
    )

    assert "Unable to parse configuration YAML" in config["_config_errors"][0]
