import asyncio
from types import SimpleNamespace

import pytest

from helper.plex import _plex_cache, get_plex_metadata
from helper.plex_paths import PlexPathError, parse_path_mappings, translate_plex_path


def test_path_mappings_use_longest_component_prefix():
    mappings = ["/media=>/mnt/media", "/media/tv=>/mnt/television"]

    assert translate_plex_path("/media/tv/Show/Episode.mkv", mappings) == (
        translate_plex_path("/mnt/television/Show/Episode.mkv")
    )
    assert translate_plex_path("/media2/Movie.mkv", mappings) == (
        translate_plex_path("/media2/Movie.mkv")
    )


def test_path_mapping_rejects_relative_and_traversal_paths():
    with pytest.raises(PlexPathError, match="source must be absolute"):
        parse_path_mappings(["media=>/media"])
    with pytest.raises(PlexPathError, match="unsafe traversal"):
        translate_plex_path("/media/../secret/file.mkv")


def test_show_paths_come_from_actual_season_directories():
    section = SimpleNamespace(
        title="TV Shows",
        type="show",
        uuid="library-1",
        _server=SimpleNamespace(machineIdentifier="server-1"),
    )
    episodes = [
        SimpleNamespace(
            seasonNumber=0,
            episodeNumber=1,
            media=[
                SimpleNamespace(
                    parts=[SimpleNamespace(file="/plex/TV/Example/Specials/S00E01.mkv")]
                )
            ],
        ),
        SimpleNamespace(
            seasonNumber=1,
            episodeNumber=1,
            media=[
                SimpleNamespace(
                    parts=[
                        SimpleNamespace(file="/plex/TV/Example/Series One/S01E01.mkv")
                    ]
                )
            ],
        ),
    ]
    item = SimpleNamespace(
        title="Example",
        year=2020,
        type="show",
        ratingKey="10",
        librarySection=section,
        guids=[],
        episodes=lambda: episodes,
    )
    _plex_cache.clear()

    metadata = asyncio.run(
        get_plex_metadata(
            item,
            _plex_config={"path_mappings": ["/plex/TV=>/media/tv"]},
        )
    )

    assert metadata["show_dir"] == "/media/tv/Example"
    assert metadata["season_dirs"] == {
        0: "/media/tv/Example/Specials",
        1: "/media/tv/Example/Series One",
    }
    assert metadata["server_id"] == "server-1"
    assert metadata["library_uuid"] == "library-1"


def test_flat_show_layout_uses_episode_directory_as_show_directory():
    section = SimpleNamespace(title="TV Shows", type="show")
    episodes = [
        SimpleNamespace(
            seasonNumber=1,
            episodeNumber=1,
            media=[
                SimpleNamespace(
                    parts=[SimpleNamespace(file="/media/TV/Flat Show/S01E01.mkv")]
                )
            ],
        ),
        SimpleNamespace(
            seasonNumber=2,
            episodeNumber=1,
            media=[
                SimpleNamespace(
                    parts=[SimpleNamespace(file="/media/TV/Flat Show/S02E01.mkv")]
                )
            ],
        ),
    ]
    item = SimpleNamespace(
        title="Flat Show",
        year=2022,
        type="show",
        ratingKey="flat",
        librarySection=section,
        guids=[],
        episodes=lambda: episodes,
    )
    _plex_cache.clear()

    metadata = asyncio.run(get_plex_metadata(item))

    assert metadata["show_dir"] == "/media/TV/Flat Show"
    assert metadata["season_dirs"] == {
        1: "/media/TV/Flat Show",
        2: "/media/TV/Flat Show",
    }


def test_legacy_agent_guid_and_empty_show_location_are_discovered():
    section = SimpleNamespace(title="TV Shows", type="show")
    item = SimpleNamespace(
        title="Legacy Show",
        year=2020,
        type="show",
        ratingKey="legacy",
        librarySection=section,
        guids=[],
        guid="com.plexapp.agents.thetvdb://12345?lang=en",
        locations=["/media/TV/Legacy Show"],
        episodes=lambda: [],
    )
    _plex_cache.clear()

    metadata = asyncio.run(get_plex_metadata(item))

    assert metadata["tvdb_id"] == "12345"
    assert metadata["show_dir"] == "/media/TV/Legacy Show"
