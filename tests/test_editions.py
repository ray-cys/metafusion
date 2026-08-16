import asyncio
from types import SimpleNamespace

from helper.identity import cache_key_for_meta, match_for_meta, metadata_key_for_meta
from helper.plex import get_plex_metadata
from helper.tmdb import tmdb_response_cache
from modules import builder as builder_module


def movie_meta(rating_key, edition_title=None):
    return {
        "library_type": "movie",
        "title": "Example",
        "year": 2020,
        "ratingKey": rating_key,
        "tmdb_id": "123",
        "imdb_id": None,
        "movie_path": f"Example {rating_key}",
        "movie_dir": f"/movies/Example {rating_key}",
        "edition_title": edition_title,
        "requires_unique_key": True,
        "edition_key_collision": False,
    }


def test_movie_identity_and_yaml_keys_do_not_collapse():
    theatrical = movie_meta("10", "Theatrical")
    directors_cut = movie_meta("11", "Director's Cut")
    no_edition = movie_meta("12")

    assert cache_key_for_meta(theatrical) == "movie:plex:10"
    assert cache_key_for_meta(directors_cut) == "movie:plex:11"
    assert metadata_key_for_meta(theatrical) == "Example (2020) [Theatrical]"
    assert metadata_key_for_meta(directors_cut) == "Example (2020) [Director's Cut]"
    assert metadata_key_for_meta(no_edition) == "Example (2020) [No Edition - plex:12]"
    assert match_for_meta(theatrical, 123)["edition"] == "Theatrical"
    assert match_for_meta(no_edition, 123)["blank_edition"] is True


def test_duplicate_tv_libraries_use_distinct_cache_and_yaml_keys():
    first = {
        "library_type": "tv",
        "library_name": "TV Shows",
        "title": "Example",
        "year": 2020,
        "ratingKey": "100",
        "requires_unique_key": True,
    }
    second = {**first, "library_name": "Anime", "ratingKey": "200"}

    assert cache_key_for_meta(first) == "tv:plex:100"
    assert cache_key_for_meta(second) == "tv:plex:200"
    assert metadata_key_for_meta(first) != metadata_key_for_meta(second)
    assert "TV Shows" in metadata_key_for_meta(first)
    assert "Anime" in metadata_key_for_meta(second)


def test_plex_metadata_reads_edition_and_filename_fallback():
    class Movie:
        title = "Example"
        year = 2020
        type = "movie"
        librarySection = SimpleNamespace(title="Movies", type="movie")
        guids = [SimpleNamespace(id="tmdb://123")]

        def __init__(self, rating_key, path, edition_title=None):
            self.ratingKey = rating_key
            self.path = path
            self.editionTitle = edition_title

        def iterParts(self):
            return [SimpleNamespace(file=self.path)]

    native = asyncio.run(
        get_plex_metadata(Movie("20", "/movies/Example/movie.mkv", "Theatrical"))
    )
    fallback = asyncio.run(
        get_plex_metadata(Movie("21", "/movies/Example {edition-4K77}/movie.mkv"))
    )

    assert native["edition_title"] == "Theatrical"
    assert fallback["edition_title"] == "4K77"


def test_builder_emits_distinct_kometa_entries_for_editions(monkeypatch):
    async def no_cache_write(*args, **kwargs):
        return None

    monkeypatch.setattr(builder_module, "meta_cache_async", no_cache_write)
    tmdb_response_cache.clear()
    tmdb_response_cache["movie/123"] = {
        "release_dates": {"results": []},
        "genres": [],
        "production_companies": [],
        "production_countries": [],
        "credits": {"crew": [], "cast": []},
        "images": {"posters": [], "backdrops": []},
        "release_date": "2020-01-01",
        "runtime": 100,
        "overview": "Example overview",
    }

    async def cached_request(_config, endpoint, **_kwargs):
        return tmdb_response_cache.get(endpoint)

    monkeypatch.setattr(builder_module, "tmdb_api_request", cached_request)
    config = {
        "settings": {"mode": "kometa"},
        "tmdb": {"language": "en", "region": "US"},
    }
    flags = {
        "metadata_basic": True,
        "metadata_enhanced": False,
        "poster": False,
        "background": False,
        "dry_run": False,
    }
    consolidated = {"metadata": {}}

    asyncio.run(
        builder_module.build_movie(
            config,
            consolidated,
            feature_flags=flags,
            meta=movie_meta("10", "Theatrical"),
        )
    )
    asyncio.run(
        builder_module.build_movie(
            config,
            consolidated,
            feature_flags=flags,
            meta=movie_meta("11", "Director's Cut"),
        )
    )

    assert set(consolidated["metadata"]) == {
        "Example (2020) [Theatrical]",
        "Example (2020) [Director's Cut]",
    }
    assert consolidated["metadata"]["Example (2020) [Theatrical]"]["match"]["edition"] == "Theatrical"
    assert consolidated["metadata"]["Example (2020) [Director's Cut]"]["match"]["edition"] == "Director's Cut"
    tmdb_response_cache.clear()
