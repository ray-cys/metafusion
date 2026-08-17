import asyncio

import pytest

from helper import cache as cache_module
from helper.asset_registry import AssetDestinationRegistry
from helper.io import sha256_file
from modules import builder


def _record(cache_key, destination, checksum, tmdb_id="100", source="/poster.jpg"):
    return {
        "cache_key": cache_key,
        "media_type": "movie",
        "tmdb_id": tmdb_id,
        "asset_type": "poster",
        "season_number": "",
        "source_path": source,
        "destination": str(destination),
        "checksum": checksum,
    }


def test_same_persisted_plex_owner_is_not_a_collision_after_restart(tmp_path):
    destination = tmp_path / "poster.jpg"
    destination.write_bytes(b"managed")
    checksum = sha256_file(destination)
    database = tmp_path / "meta_db.sqlite3"
    cache_module.begin_cache_session(database_path=database)
    cache_module.save_cache(
        {
            "movie:plex:49591": {
                "server_id": "server",
                "library_uuid": "movies",
                "rating_key": "49591",
                "media_type": "movie",
                "tmdb_id": "100",
                "poster_source_path": "/poster.jpg",
                "poster_path": str(destination),
                "poster_checksum": checksum,
            }
        }
    )
    restarted = cache_module.begin_cache_session(database_path=database)
    registry = AssetDestinationRegistry(
        restarted.asset_destination_records(
            [{"server_id": "server", "library_uuid": "movies"}]
        )
    )
    config = {
        "assets": {"update_policy": "managed"},
        "_asset_destination_registry": registry,
    }

    assert builder.protected_asset_destination(
        config,
        "movie:plex:49591",
        destination,
        "poster",
        media_type="Movie",
        full_title="28 Days Later (2002)",
        tmdb_id="100",
        source_path="/poster.jpg",
    ) == (True, "managed")


def test_shared_editions_reuse_verified_artwork_after_restart(tmp_path):
    destination = tmp_path / "poster.jpg"
    destination.write_bytes(b"shared")
    checksum = sha256_file(destination)
    database = tmp_path / "meta_db.sqlite3"
    cache_module.begin_cache_session(database_path=database)
    cache_module.save_cache(
        {
            f"movie:plex:{number}": {
                "server_id": "server",
                "library_uuid": "movies",
                "rating_key": str(number),
                "media_type": "movie",
                "tmdb_id": "100",
                "poster_source_path": "/poster.jpg",
                "poster_path": str(destination),
                "poster_checksum": checksum,
            }
            for number in (1, 2)
        }
    )
    restarted = cache_module.begin_cache_session(database_path=database)
    registry = AssetDestinationRegistry(
        restarted.asset_destination_records(
            [{"server_id": "server", "library_uuid": "movies"}]
        )
    )
    config = {
        "assets": {"update_policy": "managed"},
        "_asset_destination_registry": registry,
    }

    shared = builder._managed_shared_asset_checksum(
        config,
        "movie:plex:1",
        "100",
        "/poster.jpg",
        destination,
        "poster",
    )

    assert shared == checksum
    assert builder.protected_asset_destination(
        config,
        "movie:plex:1",
        destination,
        "poster",
        media_type="Movie",
        full_title="Example (2020) [Theatrical]",
        tmdb_id="100",
        source_path="/poster.jpg",
        shared_managed=True,
    ) == (False, "shared")


def test_shared_editions_can_advance_to_one_changed_tmdb_source(tmp_path):
    destination = tmp_path / "poster.jpg"
    destination.write_bytes(b"old")
    checksum = sha256_file(destination)
    registry = AssetDestinationRegistry(
        [
            _record("movie:plex:1", destination, checksum, source="/old.jpg"),
            _record("movie:plex:2", destination, checksum, source="/old.jpg"),
        ]
    )

    assert registry.claim(
        "movie:plex:1",
        destination,
        media_type="movie",
        tmdb_id="100",
        asset_type="poster",
        source_path="/new.jpg",
    )[0] == "self"
    assert registry.claim(
        "movie:plex:2",
        destination,
        media_type="movie",
        tmdb_id="100",
        asset_type="poster",
        source_path="/new.jpg",
    )[0] == "self"


def test_manual_change_is_not_accepted_as_verified_shared_artwork(tmp_path):
    destination = tmp_path / "poster.jpg"
    destination.write_bytes(b"managed")
    stored_checksum = sha256_file(destination)
    destination.write_bytes(b"manual replacement")
    registry = AssetDestinationRegistry(
        [_record("movie:plex:1", destination, stored_checksum)]
    )

    assert registry.shared_checksum(
        "movie:plex:2",
        destination,
        media_type="movie",
        tmdb_id="100",
        asset_type="poster",
        source_path="/poster.jpg",
    ) is None


def test_cross_library_different_titles_still_collide(tmp_path):
    destination = tmp_path / "poster.jpg"
    registry = AssetDestinationRegistry(
        [
            _record("movie:plex:1", destination, "a" * 64, tmdb_id="100"),
            _record("movie:plex:2", destination, "b" * 64, tmdb_id="200"),
        ]
    )

    status, owner = registry.claim(
        "movie:plex:1",
        destination,
        media_type="movie",
        tmdb_id="100",
        asset_type="poster",
        source_path="/poster.jpg",
    )

    assert status == "collision"
    assert owner == "movie:plex:2"


def test_unknown_source_is_never_treated_as_shareable(tmp_path):
    destination = tmp_path / "poster.jpg"
    registry = AssetDestinationRegistry(
        [_record("movie:plex:1", destination, "a" * 64, source=None)]
    )

    status, _owner = registry.claim(
        "movie:plex:2",
        destination,
        media_type="movie",
        tmdb_id="100",
        asset_type="poster",
        source_path=None,
    )

    assert status == "collision"


def test_global_preload_includes_ownership_from_unselected_libraries(tmp_path):
    destination = tmp_path / "poster.jpg"
    store = cache_module.begin_cache_session(
        database_path=tmp_path / "meta_db.sqlite3"
    )
    cache_module.save_cache(
        {
            "movie:plex:1": {
                "server_id": "server",
                "library_uuid": "movies-a",
                "rating_key": "1",
                "media_type": "movie",
                "tmdb_id": "100",
                "poster_source_path": "/poster.jpg",
                "poster_path": str(destination),
                "poster_checksum": "a" * 64,
            },
            "movie:plex:2": {
                "server_id": "server",
                "library_uuid": "movies-b",
                "rating_key": "2",
                "media_type": "movie",
                "tmdb_id": "200",
                "poster_source_path": "/poster.jpg",
                "poster_path": str(destination),
                "poster_checksum": "b" * 64,
            },
        }
    )

    records = store.asset_destination_records()

    assert {record["cache_key"] for record in records} == {
        "movie:plex:1",
        "movie:plex:2",
    }


def test_actual_destination_lock_is_shared_in_plex_and_kometa_modes(tmp_path):
    async def locks():
        plex_dir = tmp_path / "plex" / "Movie"
        plex_dir.mkdir(parents=True)
        plex = {
            "settings": {"mode": "plex"},
            "_asset_destination_registry": AssetDestinationRegistry(),
        }
        plex_meta = {"library_type": "movie", "movie_dir": str(plex_dir)}
        flags = {"poster": True, "background": False, "season": False}
        plex_first = builder._media_asset_lock(plex, plex_meta, flags)
        plex_second = builder._media_asset_lock(plex, plex_meta, flags)

        kometa = {
            "settings": {"mode": "kometa", "path": str(tmp_path / "kometa")},
            "_asset_destination_registry": AssetDestinationRegistry(),
        }
        kometa_meta = {
            "library_type": "movie",
            "movie_path": "Example (2020)",
        }
        kometa_first = builder._media_asset_lock(kometa, kometa_meta, flags)
        kometa_second = builder._media_asset_lock(kometa, kometa_meta, flags)
        return plex_first, plex_second, kometa_first, kometa_second

    plex_first, plex_second, kometa_first, kometa_second = asyncio.run(locks())
    assert plex_first is plex_second
    assert kometa_first is kometa_second


def test_indexed_registry_query_for_2000_movies_uses_one_select(tmp_path):
    store = cache_module.begin_cache_session(
        database_path=tmp_path / "meta_db.sqlite3"
    )
    cache_module.save_cache(
        {
            f"movie:plex:{number}": {
                "server_id": "server",
                "library_uuid": "movies",
                "rating_key": str(number),
                "media_type": "movie",
                "tmdb_id": str(number),
                "poster_source_path": f"/{number}.jpg",
                "poster_path": str(tmp_path / "assets" / str(number) / "poster.jpg"),
                "poster_checksum": f"{number:064x}"[-64:],
            }
            for number in range(2000)
        }
    )
    selects = []
    store._connection.set_trace_callback(
        lambda statement: selects.append(statement)
        if statement.lstrip().upper().startswith("SELECT")
        else None
    )

    records = store.asset_destination_records()

    assert len(records) == 2000
    assert len(selects) <= 2
    assert not any("SELECT cache_key, payload FROM media_state" in query for query in selects)


@pytest.mark.parametrize(
    ("policy", "expected"),
    [("fill_missing", False), ("managed", True), ("overwrite", True)],
)
def test_owner_policy_decisions_remain_effective(
    monkeypatch, tmp_path, policy, expected
):
    destination = tmp_path / "poster.jpg"
    destination.write_bytes(b"managed")
    config = {
        "assets": {"update_policy": policy},
        "_asset_destination_registry": AssetDestinationRegistry(),
    }
    monkeypatch.setattr(
        builder,
        "asset_write_allowed",
        lambda *_args, **_kwargs: (expected, policy),
    )

    assert builder.protected_asset_destination(
        config,
        "movie:plex:1",
        destination,
        "poster",
        media_type="Movie",
        full_title="Example (2020)",
        tmdb_id="100",
        source_path="/poster.jpg",
    )[0] is expected
