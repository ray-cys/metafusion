from helper.provider_mappings import (
    resolve_episode_overrides,
    resolve_split_series_mapping,
    validate_provider_mapping_config,
)


def test_builtin_split_series_resolves_by_tvdb_and_primary_tmdb():
    by_tvdb = resolve_split_series_mapping({}, tvdb_id="345246")
    by_tmdb = resolve_split_series_mapping({}, tmdb_id="109958")

    assert by_tvdb["show_policy"] == "preserve"
    assert by_tvdb["seasons"][2] == {
        "tmdb_id": "109958",
        "season_number": 1,
    }
    assert by_tmdb["identity"] == "tvdb:345246"


def test_configured_mapping_overrides_builtin_and_normalizes_seasons():
    config = {
        "tmdb": {
            "split_series_show_policy": "preserve",
            "split_series_mappings": {
                "TVDB:345246": {
                    "show_policy": "primary",
                    "seasons": {
                        "2": {"tmdb_id": 999, "season_number": "4"},
                        "3": {"tmdb_id": 1000, "season": 1},
                    },
                }
            },
        }
    }

    mapping = resolve_split_series_mapping(config, tvdb_id=345246)

    assert mapping["show_policy"] == "primary"
    assert mapping["seasons"][2] == {"tmdb_id": "999", "season_number": 4}
    assert mapping["seasons"][3] == {"tmdb_id": "1000", "season_number": 1}


def test_episode_overrides_match_provider_identity_and_normalize_values():
    config = {
        "tmdb": {
            "episode_overrides": {
                "TVDB:42": {
                    "S01E01": "S02E03",
                    "S01E02": {"season_number": 2, "episode_number": 4},
                }
            }
        }
    }

    assert resolve_episode_overrides(config, tvdb_id="42") == {
        (1, 1): (2, 3),
        (1, 2): (2, 4),
    }


def test_episode_overrides_can_follow_a_reverse_split_series_identity():
    config = {
        "tmdb": {
            "split_series_mappings": {
                "tvdb:42": {
                    "seasons": {1: {"tmdb_id": 900, "season_number": 1}}
                }
            },
            "episode_overrides": {"tvdb:42": {"S01E01": "S01E02"}},
        }
    }

    assert resolve_episode_overrides(config, tmdb_id=900) == {(1, 1): (1, 2)}


def test_provider_mapping_validation_rejects_unsafe_shapes():
    errors = validate_provider_mapping_config(
        {
            "split_series_show_policy": "replace",
            "split_series_mappings": {
                "bad-key": {"seasons": {}},
                "tvdb:1": {"seasons": {"x": {"tmdb_id": 0}}},
            },
            "episode_overrides": {"tvdb:1": {"episode-one": "S01E01"}},
        }
    )

    assert any("split_series_show_policy" in error for error in errors)
    assert any("bad-key" in error for error in errors)
    assert any("requires positive tmdb_id" in error for error in errors)
    assert any("episode-one" in error for error in errors)
