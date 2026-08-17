import asyncio
import logging
from types import SimpleNamespace

import yaml

import metafusion
from helper.tmdb import tmdb_response_cache
from modules import builder, processing
from modules.kometa import validate_metadata_document


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FakeSection:
    def __init__(self, title, library_type, items):
        self.title = title
        self.type = library_type
        self.TYPE = library_type
        self._items = items

    def all(self):
        return list(self._items)


def test_mocked_plex_tmdb_pipeline_generates_valid_movie_show_and_specials(
    monkeypatch,
    tmp_path,
):
    movie_item = SimpleNamespace(
        title="Example Movie",
        year=2020,
        type="movie",
        ratingKey="m1",
        updatedAt="1",
        editionTitle=None,
    )
    show_item = SimpleNamespace(
        title="Example Show",
        year=2021,
        type="show",
        ratingKey="s1",
        updatedAt="1",
    )
    movie_section = FakeSection("Movies", "movie", [movie_item])
    show_section = FakeSection("TV Shows", "show", [show_item])

    async def fake_metadata(item, **_kwargs):
        if item is movie_item:
            return {
                "library_name": "Movies",
                "library_type": "movie",
                "title": item.title,
                "year": item.year,
                "ratingKey": item.ratingKey,
                "updatedAt": item.updatedAt,
                "tmdb_id": "100",
                "imdb_id": "tt100",
                "movie_path": "Example Movie (2020)",
                "movie_dir": "/media/Example Movie (2020)",
                "edition_title": None,
            }
        return {
            "library_name": "TV Shows",
            "library_type": "tv",
            "title": item.title,
            "year": item.year,
            "ratingKey": item.ratingKey,
            "updatedAt": item.updatedAt,
            "tmdb_id": "200",
            "tvdb_id": "300",
            "imdb_id": "tt200",
            "show_path": "Example Show (2021)",
            "show_dir": "/media/Example Show (2021)",
            "seasons_episodes": {0: [1]},
        }

    async def fake_tmdb(_config, endpoint, **_kwargs):
        if endpoint == "movie/100":
            return {
                "release_dates": {"results": []},
                "genres": [{"name": "Drama"}],
                "production_companies": [{"name": "Studio"}],
                "production_countries": [{"iso_3166_1": "US"}],
                "credits": {"crew": [], "cast": []},
                "images": {"posters": [], "backdrops": []},
                "release_date": "2020-01-01",
                "runtime": 120,
                "overview": "Movie summary",
            }
        if endpoint == "tv/200":
            return {
                "content_ratings": {"results": []},
                "genres": [{"name": "Drama"}],
                "networks": [{"name": "Network"}],
                "origin_country": ["US"],
                "credits": {"crew": []},
                "images": {"posters": [], "backdrops": []},
                "seasons": [{"season_number": 0}],
                "first_air_date": "2021-01-01",
                "overview": "Show summary",
            }
        if endpoint == "tv/200/season/0":
            return {
                "air_date": "2020-12-01",
                "credits": {"crew": []},
                "images": {"posters": []},
                "episodes": [
                    {
                        "episode_number": 1,
                        "name": "Special",
                        "air_date": "2020-12-01",
                        "overview": "Special summary",
                        "crew": [],
                    }
                ],
            }
        return {}

    async def no_cache_write(*_args, **_kwargs):
        return None

    async def fake_preflight(_config, _session):
        return object()

    monkeypatch.setattr(processing, "get_plex_metadata", fake_metadata)
    monkeypatch.setattr(processing, "meta_cache_async", no_cache_write)
    monkeypatch.setattr(builder, "meta_cache_async", no_cache_write)
    monkeypatch.setattr(builder, "tmdb_api_request", fake_tmdb)
    monkeypatch.setattr(metafusion, "preflight_connectors", fake_preflight)
    monkeypatch.setattr(
        metafusion,
        "connect_plex_library",
        lambda _config, plex=None: (
            [movie_section, show_section],
            ["Movies", "TV Shows"],
            [
                {"title": "Movies", "type": "movie"},
                {"title": "TV Shows", "type": "show"},
            ],
        ),
    )
    monkeypatch.setattr(metafusion, "check_sys_requirements", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(metafusion, "get_meta_banner", lambda *_args: None)
    monkeypatch.setattr(metafusion, "get_disabled_features", lambda *_args: None)
    monkeypatch.setattr(metafusion, "log_final_summary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        metafusion, "mark_library_scan_started", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        metafusion, "mark_library_scan_complete", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(metafusion.aiohttp, "ClientSession", lambda **_kwargs: FakeSession())
    monkeypatch.setattr(metafusion.aiohttp, "TCPConnector", lambda **_kwargs: object())
    tmdb_response_cache.configure(tmp_path / "tmdb.json")

    config = {
        "settings": {"mode": "kometa", "path": str(tmp_path), "dry_run": False},
        "plex": {"url": "http://plex:32400", "token": "token"},
        "plex_libraries": ["Movies", "TV Shows"],
        "tmdb": {"api_key": "key", "language": "en", "region": "US", "fallback": []},
        "metadata": {"run_basic": True, "run_enhanced": True},
        "assets": {"run_poster": False, "run_season": False, "run_background": False},
        "cleanup": {"run_cleanup": False},
        "runtime": {"max_concurrency": 2},
        "incremental": {"enabled": False, "full_scan_interval_hours": 168},
        "output": {"validate_schema": True, "backup_count": 1},
        "safety": {"allow_ambiguous_editions": False},
    }

    asyncio.run(metafusion.metafusion_main(config, logging.getLogger("integration")))

    movie_doc = yaml.safe_load((tmp_path / "metadata" / "movie_metadata.yml").read_text())
    show_doc = yaml.safe_load((tmp_path / "metadata" / "tv_metadata.yml").read_text())
    assert validate_metadata_document(movie_doc)
    assert validate_metadata_document(show_doc)
    assert show_doc["metadata"]["Example Show (2021)"]["seasons"][0]["episodes"][1]["title"] == "Special"
