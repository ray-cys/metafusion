from pathlib import Path

import yaml

from helper.config import DEFAULT_CONFIG, get_image_upgrade_days, load_config_file


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
                "plex": {"url": "http://yaml-plex:32400", "token": "yaml-token"},
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
    assert config["plex"]["url"] == "http://yaml-plex:32400"
    assert config["settings"]["run_times"] == ["06:00", "18:30"]
    assert config["metafusion_run"] is False


def test_blank_environment_values_fall_back_to_yaml(tmp_path):
    config_file = tmp_path / "config.yml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "plex": {"url": "http://yaml-plex:32400", "token": "yaml-token"},
                "tmdb": {"api_key": "yaml-key"},
                "settings": {"mode": "plex"},
            }
        ),
        encoding="utf-8",
    )

    config = load_config_file(
        config_file=config_file,
        template_file=TEMPLATE_FILE,
        environ={
            "PLEX_URL": "",
            "PLEX_TOKEN": "   ",
            "TMDB_API_KEY": "",
            "RUN_MODE": "",
        },
    )

    assert config["plex"] == {
        "url": "http://yaml-plex:32400",
        "token": "yaml-token",
    }
    assert config["tmdb"]["api_key"] == "yaml-key"
    assert config["settings"]["mode"] == "plex"


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


def test_blank_environment_does_not_block_template_creation(tmp_path):
    config_file = tmp_path / "nested" / "config.yml"

    load_config_file(
        config_file=config_file,
        template_file=TEMPLATE_FILE,
        environ={"PLEX_TOKEN": "", "TMDB_API_KEY": "  ", "RUN_MODE": ""},
    )

    assert config_file.exists()


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


def test_runtime_limits_and_safety_flags_accept_environment_overrides(tmp_path):
    config = load_config_file(
        config_file=tmp_path / "config.yml",
        template_file=TEMPLATE_FILE,
        environ={
            "MAX_CONCURRENCY": "4",
            "REQUEST_TIMEOUT": "45.5",
            "CONNECT_TIMEOUT": "7",
            "PLEX_TIMEOUT": "8",
            "SHUTDOWN_TIMEOUT": "12",
            "MAX_IMAGE_MB": "12",
            "ALLOW_AMBIGUOUS_EDITIONS": "true",
        },
    )

    assert config["runtime"] == {
        "max_concurrency": 4,
        "request_timeout": 45.5,
        "connect_timeout": 7.0,
        "plex_timeout": 8.0,
        "plex_retries": 3,
        "plex_retry_delay": 1.0,
        "shutdown_timeout": 12.0,
        "max_image_mb": 12,
    }
    assert config["safety"]["allow_ambiguous_editions"] is True


def test_image_upgrade_intervals_support_global_and_per_type_env(tmp_path):
    config = load_config_file(
        config_file=tmp_path / "config.yml",
        template_file=TEMPLATE_FILE,
        environ={
            "IMAGE_UPGRADE_DAYS": "7",
            "MOVIE_IMAGE_UPGRADE_DAYS": "30",
            "SERIES_IMAGE_UPGRADE_DAYS": "15",
            "SEASON_IMAGE_UPGRADE_DAYS": "0.5",
        },
    )

    assert get_image_upgrade_days(config, "movie") == 30
    assert get_image_upgrade_days(config, "tv") == 15
    assert get_image_upgrade_days(config, "season") == 0.5

    inherited = load_config_file(
        config_file=tmp_path / "other.yml",
        template_file=TEMPLATE_FILE,
        environ={"IMAGE_UPGRADE_DAYS": "7"},
    )
    assert get_image_upgrade_days(inherited, "movie") == 7
    assert get_image_upgrade_days(inherited, "series") == 7


def test_run_cleanup_controls_cleanup_setting(tmp_path):
    current, sources = load_config_file(
        config_file=tmp_path / "config.yml",
        template_file=TEMPLATE_FILE,
        environ={"RUN_CLEANUP": "true"},
        return_sources=True,
    )
    legacy = load_config_file(
        config_file=tmp_path / "legacy.yml",
        template_file=TEMPLATE_FILE,
        environ={"RUN_PROCESS": "true"},
    )

    assert current["cleanup"]["run_process"] is True
    assert sources[("cleanup", "run_process")] == "RUN_CLEANUP"
    assert legacy["cleanup"]["run_process"] is False


def test_template_keeps_destructive_cleanup_disabled():
    template = yaml.safe_load(TEMPLATE_FILE.read_text(encoding="utf-8"))

    assert template["cleanup"]["run_process"] is False
