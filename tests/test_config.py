from pathlib import Path

import yaml

from helper.config import DEFAULT_CONFIG, load_config_file


TEMPLATE_FILE = Path(__file__).parents[1] / "config_template.yml"


def test_environment_only_config_does_not_copy_template(tmp_path):
    config_file = tmp_path / "config.yml"

    config = load_config_file(
        config_file=config_file,
        template_file=TEMPLATE_FILE,
        environ={
            "PLEX_TOKEN": "actual-token",
            "TMDB_API_KEY": "actual-key",
            "METAFUSION_RUN": "false",
        },
    )

    assert not config_file.exists()
    assert config["plex"]["token"] == "actual-token"
    assert config["tmdb"]["api_key"] == "actual-key"
    assert config["metafusion_run"] is False


def test_environment_values_override_yaml(tmp_path):
    config_file = tmp_path / "config.yml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "metafusion_run": True,
                "plex": {"token": "yaml-token"},
                "settings": {"run_times": ["01:00"]},
            }
        ),
        encoding="utf-8",
    )

    config = load_config_file(
        config_file=config_file,
        template_file=TEMPLATE_FILE,
        environ={
            "PLEX_TOKEN": "env-token",
            "RUN_TIMES": "06:00, 18:30",
            "METAFUSION_RUN": "no",
        },
    )

    assert config["plex"]["token"] == "env-token"
    assert config["settings"]["run_times"] == ["06:00", "18:30"]
    assert config["metafusion_run"] is False


def test_loading_yaml_does_not_mutate_defaults(tmp_path):
    config_file = tmp_path / "config.yml"
    config_file.write_text("settings:\n  log_level: DEBUG\n", encoding="utf-8")

    config = load_config_file(
        config_file=config_file,
        template_file=TEMPLATE_FILE,
        environ={},
    )

    assert config["settings"]["log_level"] == "DEBUG"
    assert DEFAULT_CONFIG["settings"]["log_level"] == "INFO"


def test_template_is_created_only_when_no_config_source_exists(tmp_path):
    config_file = tmp_path / "nested" / "config.yml"

    config = load_config_file(
        config_file=config_file,
        template_file=TEMPLATE_FILE,
        environ={},
    )

    assert config_file.exists()
    assert config["metafusion_run"] is False


def test_dry_run_config_loading_does_not_create_template(tmp_path):
    config_file = tmp_path / "nested" / "config.yml"

    config = load_config_file(
        config_file=config_file,
        template_file=TEMPLATE_FILE,
        environ={},
        create_if_missing=False,
    )

    assert not config_file.exists()
    assert not config_file.parent.exists()
    assert config["settings"]["dry_run"] is False
