import copy
from pathlib import Path

import pytest
import yaml

from helper import config as config_module
from helper.config import (
    DEFAULT_CONFIG,
    ConfigError,
    apply_secret_file_overrides,
    config_for_library,
    config_source_report,
    get_disabled_features,
    get_image_upgrade_days,
    load_config_file,
    safe_bool,
    safe_float,
    safe_int,
    safe_json_mapping,
    safe_list,
    safe_path_mappings,
    validate_config,
)

TEMPLATE_FILE = Path(__file__).parents[1] / "config" / "config_template.yml"


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
        "path_mappings": [],
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
            "VALIDATE_MEDIA_MOUNTS": "false",
            "MIN_FREE_SPACE_MB": "64",
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
        "config_reload": True,
        "max_image_mb": 12,
        "validate_media_mounts": False,
        "min_free_space_mb": 64,
    }
    assert config["safety"]["allow_ambiguous_editions"] is True


def test_provider_mapping_json_environment_is_parsed_and_validated(tmp_path):
    config = load_config_file(
        config_file=tmp_path / "config.yml",
        template_file=TEMPLATE_FILE,
        environ={
            "TMDB_SPLIT_SERIES_MAPPINGS": (
                '{"tvdb:42":{"show_policy":"primary","seasons":'
                '{"2":{"tmdb_id":99,"season_number":1}}}}'
            ),
            "TMDB_EPISODE_OVERRIDES": (
                '{"tvdb:42":{"S02E01":"S01E03"}}'
            ),
            "METADATA_PENDING_RECHECK_HOURS": "6",
            "REPORT_RETENTION": "4",
        },
    )

    assert config["tmdb"]["split_series_mappings"]["tvdb:42"][
        "seasons"
    ]["2"]["tmdb_id"] == 99
    assert config["tmdb"]["episode_overrides"]["tvdb:42"]["S02E01"] == "S01E03"
    assert config["incremental"]["metadata_pending_recheck_hours"] == 6.0
    assert config["output"]["report_retention"] == 4


def test_dashboard_automatic_refresh_is_opt_in(tmp_path):
    default_config = load_config_file(
        config_file=tmp_path / "default.yml",
        template_file=TEMPLATE_FILE,
        environ={"PLEX_TOKEN": "token"},
    )
    enabled_config = load_config_file(
        config_file=tmp_path / "enabled.yml",
        template_file=TEMPLATE_FILE,
        environ={"PLEX_TOKEN": "token", "DASHBOARD_ENABLED": "true"},
    )

    assert default_config["output"]["dashboard_enabled"] is False
    assert enabled_config["output"]["dashboard_enabled"] is True


def test_invalid_provider_mapping_environment_fails_validation(tmp_path):
    config = load_config_file(
        config_file=tmp_path / "config.yml",
        template_file=TEMPLATE_FILE,
        environ={"TMDB_EPISODE_OVERRIDES": "not-json"},
    )

    assert any(
        "TMDB_EPISODE_OVERRIDES" in error for error in validate_config(config)
    )


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


def test_tmdb_cache_limits_support_environment_overrides(tmp_path):
    config = load_config_file(
        config_file=tmp_path / "missing.yml",
        environ={
            "TMDB_CACHE_ENABLED": "true",
            "TMDB_CACHE_TTL_HOURS": "12",
            "TMDB_CACHE_NEGATIVE_TTL_HOURS": "6",
            "TMDB_CACHE_MAX_ENTRIES": "3000",
            "TMDB_CACHE_MAX_MB": "128.5",
        },
    )

    assert config["tmdb_cache"] == {
        "enabled": True,
        "ttl_hours": 12.0,
        "negative_ttl_hours": 6.0,
        "max_entries": 3000,
        "max_mb": 128.5,
    }


def test_phase12_tmdb_and_kometa_policies_support_environment_overrides(tmp_path):
    config = load_config_file(
        config_file=tmp_path / "config.yml",
        template_file=TEMPLATE_FILE,
        environ={
            "ARTWORK_ALLOW_ANY_LANGUAGE": "false",
            "TMDB_TITLE_SEARCH_FALLBACK": "true",
            "TMDB_EPISODE_GROUP_FALLBACK": "false",
            "KOMETA_TAG_POLICY": "sync",
        },
    )

    assert config["tmdb"]["artwork_allow_any_language"] is False
    assert config["tmdb"]["title_search_fallback"] is True
    assert config["tmdb"]["episode_group_fallback"] is False
    assert config["kometa"]["tag_policy"] == "sync"
    config["kometa"]["tag_policy"] = "replace"
    assert any("kometa.tag_policy" in error for error in validate_config(config))


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

    assert current["cleanup"]["run_cleanup"] is True
    assert sources[("cleanup", "run_cleanup")] == "RUN_CLEANUP"
    assert legacy["cleanup"]["run_cleanup"] is False


def test_template_keeps_destructive_cleanup_disabled():
    template = yaml.safe_load(TEMPLATE_FILE.read_text(encoding="utf-8"))

    assert template["cleanup"]["run_cleanup"] is False


def test_library_artwork_overrides_inherit_global_configuration():
    config = {**DEFAULT_CONFIG, "image_upgrades": dict(DEFAULT_CONFIG["image_upgrades"])}
    config["library_overrides"] = {
        "Anime": {"image_upgrades": {"series_days": 7, "season_days": 3}}
    }

    anime = config_for_library(config, "Anime")
    movies = config_for_library(config, "Movies")

    assert get_image_upgrade_days(anime, "series") == 7
    assert get_image_upgrade_days(anime, "season") == 3
    assert get_image_upgrade_days(movies, "series") == 30
    assert anime["_library_name"] == "Anime"


def test_library_override_validation_rejects_unsupported_features():
    config = {**DEFAULT_CONFIG, "library_overrides": {"Anime": {"cleanup": True}}}
    assert any("unsupported keys" in error for error in validate_config(config))


def test_safe_converters_reject_invalid_values_without_sharing_defaults(monkeypatch):
    events = []
    monkeypatch.setattr(
        config_module,
        "log_config_event",
        lambda event, **kwargs: events.append((event, kwargs)),
    )
    default = ["default"]

    assert safe_int("bad", 3, key="COUNT") == 3
    assert safe_float(None, 2.5, key="RATIO") == 2.5
    assert safe_bool(True, False) is True
    assert safe_bool("maybe", False, key="FLAG") is False
    assert safe_list([" one ", ""], default) == ["one"]
    assert safe_list(10, default, key="LIST") == default
    assert safe_list(10, default) is not default
    assert safe_path_mappings([" /a=>/b "], []) == ["/a=>/b"]
    assert safe_path_mappings(10, default, key="MAPPINGS") == default
    assert safe_json_mapping({"one": 1}, {}) == {"one": 1}
    assert safe_json_mapping('{"two": 2}', {}) == {"two": 2}
    assert safe_json_mapping("[]", {"fallback": True}, key="JSON") == {
        "fallback": True
    }
    assert len(events) == 6


def test_configuration_schema_loader_rejects_missing_and_unsupported_schema(
    monkeypatch, tmp_path
):
    missing = tmp_path / "missing.yml"
    monkeypatch.setattr(config_module, "CONFIG_SCHEMA_FILE", missing)
    with pytest.raises(ConfigError, match="Unable to load configuration schema"):
        config_module._load_config_schema()

    invalid = tmp_path / "schema.yml"
    invalid.write_text("schema_version: 2\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_SCHEMA_FILE", invalid)
    with pytest.raises(ConfigError, match="Unsupported or invalid"):
        config_module._load_config_schema()


def test_feature_reporting_library_override_and_invalid_upgrade_interval(monkeypatch):
    events = []
    monkeypatch.setattr(
        config_module,
        "log_config_event",
        lambda event, **kwargs: events.append((event, kwargs)),
    )
    get_disabled_features(
        {
            "metadata": {"run_basic": True},
            "assets": {"run_poster": False},
            "cleanup": {},
        },
        None,
    )
    assert any(
        event == "feature_enabled" and details.get("feature") == "Metadata Extraction"
        for event, details in events
    )
    assert any(
        event == "feature_disabled" and details.get("feature") == "Cleanup Libraries"
        for event, details in events
    )
    profile = next(details for event, details in events if event == "feature_profile")
    assert profile["mode"] == "Kometa"
    assert profile["metadata"] == "Basic"
    assert profile["poster"] == "Disabled"
    assert profile["cleanup"] == "Disabled"
    assert profile["dashboard"] == "Disabled"

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["library_overrides"] = {
        "Movies": {
            "plex_metadata": {"enabled": True, "policy": "managed"},
        }
    }
    effective = config_for_library(config, "Movies")
    assert effective["plex_metadata"]["enabled"] is True
    assert effective["plex_metadata"]["policy"] == "managed"
    config["image_upgrades"]["movie_days"] = "invalid"
    assert get_image_upgrade_days(config, "movie") == 30.0


def test_secret_files_handle_priority_empty_and_unreadable_values(tmp_path):
    config = copy.deepcopy(DEFAULT_CONFIG)
    token_file = tmp_path / "plex-token"
    token_file.write_text("file-token\n", encoding="utf-8")
    sources = {}
    apply_secret_file_overrides(
        config,
        environ={"PLEX_TOKEN_FILE": str(token_file)},
        sources=sources,
    )
    assert config["plex"]["token"] == "file-token"
    assert sources[("plex", "token")] == "PLEX_TOKEN_FILE"

    config["plex"]["token"] = "unchanged"
    apply_secret_file_overrides(
        config,
        environ={"PLEX_TOKEN": "direct", "PLEX_TOKEN_FILE": str(token_file)},
    )
    assert config["plex"]["token"] == "unchanged"

    empty = tmp_path / "empty"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="is empty"):
        apply_secret_file_overrides(
            copy.deepcopy(DEFAULT_CONFIG),
            environ={"TMDB_API_KEY_FILE": str(empty)},
        )
    with pytest.raises(ConfigError, match="Unable to read"):
        apply_secret_file_overrides(
            copy.deepcopy(DEFAULT_CONFIG),
            environ={"TMDB_API_KEY_FILE": str(tmp_path / "absent")},
        )


def test_validation_reports_malformed_configuration_surface():
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["settings"].update(
        {
            "mode": "invalid",
            "path": "",
            "schedule": True,
            "run_times": ["25:99"],
            "log_max_mb": "large",
            "log_backup_count": 0,
        }
    )
    config["plex"].update(
        {
            "url": "http://user:pass@plex:32400",
            "token": "PLEX_TOKEN",
            "path_mappings": "not-a-list",
        }
    )
    config["tmdb"]["api_key"] = "TMDB_API_KEY"
    config["kometa"]["tag_policy"] = "replace"
    config["plex_libraries"] = ["auto", "Movies"]
    config["plex_metadata"].update(
        {
            "enabled": True,
            "policy": "invalid",
            "fields": ["unsupported"],
            "max_writes_per_run": "many",
        }
    )
    config["runtime"].update(
        {"request_timeout": 1, "connect_timeout": 10, "max_image_mb": -1}
    )
    config["incremental"]["full_scan_interval_hours"] = "often"
    config["output"]["report_retention"] = 1001
    config["image_upgrades"].update(
        {"default_days": 4000, "movie_days": "soon", "series_days": -1}
    )
    config["library_overrides"] = {
        "BadValue": "not-a-mapping",
        "BadImages": {"image_upgrades": "not-a-mapping"},
        "Movies": {
            "image_upgrades": {
                "unknown": 1,
                "movie_days": "soon",
                "season_days": 4001,
            },
            "plex_metadata": {
                "enabled": True,
                "policy": "overwrite",
                "allow_overwrite": False,
                "fields": "summary",
                "recheck_days": "often",
                "max_writes_per_run": 0,
                "unknown": True,
            },
        },
    }
    config["assets"].update(
        {
            "update_policy": "replace",
            "run_poster": False,
            "run_season": False,
            "run_background": False,
        }
    )
    config["metadata"].update({"run_basic": False, "run_enhanced": True})
    config["cleanup"]["run_cleanup"] = False
    config["poster_set"].update({"min_width": 0, "max_height": "large"})

    errors = validate_config(config)
    joined = "\n".join(errors)

    for expected in (
        "settings.mode",
        "kometa.tag_policy",
        "plex.path_mappings",
        "embedded credentials",
        "Plex token",
        "TMDb API key",
        "auto cannot be combined",
        "Invalid schedule time",
        "settings.log_max_mb must be numeric",
        "runtime.max_image_mb must be between",
        "output.report_retention must be between",
        "image_upgrades.movie_days must be numeric",
        "library_overrides.BadValue must be a mapping",
        "image_upgrades must be a mapping",
        "plex_metadata.allow_overwrite",
        "plex_metadata.fields must be a list",
        "assets.update_policy",
        "metadata.run_basic",
        "poster_set.min_width",
        "poster_set height limits",
    ):
        assert expected in joined


def test_config_source_report_redacts_secret_values():
    config = {"plex": {"token": "secret"}, "tmdb": {"api_key": ""}, "plain": 1}
    report = config_source_report(
        config,
        {("plex", "token"): "PLEX_TOKEN", ("tmdb", "api_key"): "default"},
    )

    assert "plex.token: set (PLEX_TOKEN)" in report
    assert "tmdb.api_key: missing (default)" in report
    assert all("secret" not in line for line in report)


def test_additional_validation_shapes_cover_mutually_exclusive_errors():
    base = copy.deepcopy(DEFAULT_CONFIG)
    base["settings"].update(
        {"mode": "kometa", "path": "", "schedule": True, "run_times": []}
    )
    base["plex"].update(
        {"url": "not-a-url", "path_mappings": ["not-a-mapping"]}
    )
    base["plex_libraries"] = "Movies"
    base["plex_metadata"]["fields"] = "summary"
    errors = "\n".join(validate_config(base))
    assert "settings.path is required" in errors
    assert "plex_metadata.fields must be a list" in errors
    assert "plex.url must be a complete" in errors
    assert "plex_libraries must be a list" in errors
    assert "settings.run_times must contain" in errors
    assert "path mapping" in errors.lower()

    non_mapping = copy.deepcopy(DEFAULT_CONFIG)
    non_mapping["library_overrides"] = []
    assert any("library_overrides must be a mapping" in error for error in validate_config(non_mapping))

    override_types = copy.deepcopy(DEFAULT_CONFIG)
    override_types["library_overrides"] = {
        "Movies": {
            "image_upgrades": {"movie_days": None},
            "plex_metadata": "invalid",
        }
    }
    assert any("plex_metadata must be a mapping" in error for error in validate_config(override_types))

    override_values = copy.deepcopy(DEFAULT_CONFIG)
    override_values["runtime"]["request_timeout"] = "invalid"
    override_values["library_overrides"] = {
        "Movies": {
            "plex_metadata": {
                "policy": "invalid",
                "fields": ["unsupported"],
            }
        }
    }
    errors = "\n".join(validate_config(override_values))
    assert "policy must be fill_missing" in errors
    assert "contains unsupported fields" in errors


def test_unknown_key_reporting_and_private_source_entries(monkeypatch):
    events = []
    monkeypatch.setattr(
        config_module,
        "log_config_event",
        lambda event, **kwargs: events.append((event, kwargs)),
    )
    config_module.warn_unknown_keys(
        {"settings": {"unknown": True}}, {"settings": {"mode": "kometa"}}
    )
    assert events == [("unknown_key", {"key": "settings.unknown"})]
    assert safe_path_mappings("/host=>/media;/tv=>/shows", []) == [
        "/host=>/media",
        "/tv=>/shows",
    ]
    report = config_source_report({"_private": "hidden", "public": True}, {})
    assert report == ["public: True (default)"]


def test_config_loading_handles_missing_template_invalid_root_and_read_error(
    monkeypatch, tmp_path
):
    config_file = tmp_path / "missing" / "config.yml"
    config = load_config_file(
        config_file=config_file,
        template_file=tmp_path / "missing-template.yml",
        environ={},
    )
    assert not config_file.exists()
    assert config == DEFAULT_CONFIG

    invalid = tmp_path / "invalid.yml"
    invalid.write_text("- not\n- a mapping\n", encoding="utf-8")
    config = load_config_file(
        config_file=invalid,
        template_file=TEMPLATE_FILE,
        environ={},
    )
    assert any("Unable to parse" in error for error in config["_config_errors"])

    unreadable = tmp_path / "unreadable.yml"
    unreadable.write_text("settings: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        "builtins.open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("denied")),
    )
    with pytest.raises(ConfigError, match="Unable to read configuration file"):
        load_config_file(
            config_file=unreadable,
            template_file=TEMPLATE_FILE,
            environ={},
        )


def test_json_environment_validation_accepts_mapping_and_secret_files_are_optional():
    assert config_module._valid_env_conversion(safe_json_mapping, {"key": "value"})
    config = copy.deepcopy(DEFAULT_CONFIG)
    assert apply_secret_file_overrides(config, environ={}) is config
