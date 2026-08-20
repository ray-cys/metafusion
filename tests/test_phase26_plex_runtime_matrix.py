import asyncio
from types import SimpleNamespace

import pytest

from helper import plex


def _config(retries=2, delay=0):
    return {
        "plex": {"url": "http://plex", "token": "secret"},
        "runtime": {"plex_retries": retries, "plex_retry_delay": delay},
    }


def test_country_external_ids_and_show_directory_edge_cases(tmp_path):
    assert plex.get_plex_country("US") == "United States of America"
    assert plex.get_plex_country("XX") == "XX"
    item = SimpleNamespace(
        guids=[
            SimpleNamespace(id="tmdb://10"),
            "imdb://tt123",
            SimpleNamespace(id="tvdb/20"),
            None,
        ],
        guid="themoviedb://99",
    )
    assert plex._external_ids(item) == {"tmdb": "10", "imdb": "tt123", "tvdb": "20"}
    assert plex._discover_show_directory([]) is None
    assert plex._discover_show_directory([tmp_path / "Show" / "Season 01"]) == tmp_path / "Show"
    assert plex._discover_show_directory([tmp_path / "Show"]) == tmp_path / "Show"
    assert plex._discover_show_directory(
        [tmp_path / "Show" / "Season 01", tmp_path / "Show" / "Season 02"]
    ) == tmp_path / "Show"
    assert plex._discover_show_directory(
        [tmp_path / "One" / "Season 01", tmp_path / "Two" / "Season 01"]
    ) == tmp_path


def test_connect_server_and_library_retry_selection_matrix(monkeypatch):
    attempts = []

    def server(*_args, **_kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("temporary")
        return SimpleNamespace(version="1.0")

    monkeypatch.setattr(plex, "PlexServer", server)
    monkeypatch.setattr(plex, "log_plex_event", lambda *_a, **_k: None)
    assert plex.connect_plex_server(_config()).version == "1.0"
    monkeypatch.setattr(
        plex,
        "PlexServer",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    with pytest.raises(RuntimeError, match="Unable to connect"):
        plex.connect_plex_server(_config(retries=1))

    sections = [
        SimpleNamespace(title="Movies", TYPE="movie", uuid="m"),
        SimpleNamespace(title="Shows", type="show", key="s"),
        SimpleNamespace(title="Music", type="artist"),
    ]
    library_attempts = []

    def list_sections():
        library_attempts.append(True)
        if len(library_attempts) == 1:
            raise RuntimeError("temporary")
        return sections

    server_object = SimpleNamespace(library=SimpleNamespace(sections=list_sections))
    selected, names, available = plex.connect_plex_library(
        _config(), ["auto"], plex=server_object
    )
    assert [section.title for section in selected] == ["Movies", "Shows"]
    assert names == ["Movies", "Shows"]
    assert len(available) == 3

    with pytest.raises(RuntimeError, match="unsupported types"):
        plex.connect_plex_library(_config(), ["Music"], plex=server_object)
    empty = SimpleNamespace(library=SimpleNamespace(sections=lambda: []))
    with pytest.raises(RuntimeError, match="No supported"):
        plex.connect_plex_library(_config(), [], plex=empty)
    assert plex.connect_plex_library(_config(), ["Ghost"], plex=server_object)[0] == []

    broken = SimpleNamespace(
        library=SimpleNamespace(
            sections=lambda: (_ for _ in ()).throw(RuntimeError("offline"))
        )
    )
    with pytest.raises(RuntimeError, match="Unable to retrieve"):
        plex.connect_plex_library(_config(retries=1), ["Movies"], plex=broken)


def test_path_samples_include_locations_parts_and_isolate_failures(monkeypatch):
    good = SimpleNamespace(
        title="Good",
        locations=["/location", ""],
        iterParts=lambda: [SimpleNamespace(file="/part"), SimpleNamespace(file=None)],
    )
    bad_item = SimpleNamespace(
        title="Bad",
        locations=[],
        iterParts=lambda: (_ for _ in ()).throw(RuntimeError("parts")),
    )
    section = SimpleNamespace(title="Movies", search=lambda **_kwargs: [good, bad_item])
    failed = SimpleNamespace(
        title="Failed",
        search=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("search")),
    )
    monkeypatch.setattr(plex, "log_plex_event", lambda *_a, **_k: None)
    assert plex.collect_plex_path_samples([section, failed]) == ["/location", "/part"]


def test_plex_operation_circuit_retry_and_helpers(monkeypatch):
    attempts = []

    def operation():
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("temporary")
        return "ok"

    monkeypatch.setattr(plex, "log_plex_event", lambda *_a, **_k: None)
    assert asyncio.run(plex.plex_operation(operation, _config()["runtime"])) == "ok"
    with pytest.raises(RuntimeError, match="failed after"):
        asyncio.run(
            plex.plex_operation(
                lambda: (_ for _ in ()).throw(RuntimeError("always")),
                {"plex_retries": 1},
            )
        )

    def signature_error():
        return []

    monkeypatch.setattr(
        plex.inspect,
        "signature",
        lambda _value: (_ for _ in ()).throw(ValueError("signature")),
    )
    assert plex._supports_paged_all(SimpleNamespace(all=signature_error)) is True
    assert plex._library_total(SimpleNamespace(totalSize=lambda: "bad")) is None
    assert plex._library_total(SimpleNamespace(totalViewSize=lambda **_k: 3)) == 3
    assert plex.plex_inventory_record(SimpleNamespace(type="shows", guids=[]))["media_type"] == "tv"
    assert plex.plex_inventory_record(SimpleNamespace(type="movies", guids=[]))["media_type"] == "movie"


class PagedSection:
    title = "Movies"

    def __init__(self, items, totals):
        self.items = items
        self.totals = iter(totals)

    def totalViewSize(self, **_kwargs):
        return next(self.totals)

    def all(self, container_start=0, container_size=200, **_kwargs):
        return self.items[container_start : container_start + container_size]


def test_inventory_paging_records_and_integrity_failures():
    items = [
        SimpleNamespace(ratingKey=str(index), title=f"Movie {index}", type="movie", guids=[])
        for index in range(3)
    ]
    records = asyncio.run(
        plex.load_plex_library_inventory(PagedSection(items, [3, 3]), records_only=True)
    )
    assert [record["rating_key"] for record in records] == ["0", "1", "2"]

    duplicate = [items[0], items[0]]
    with pytest.raises(RuntimeError, match="repeated rating key"):
        asyncio.run(plex.load_plex_library_inventory(PagedSection(duplicate, [2, 2])))
    with pytest.raises(RuntimeError, match="changed during paging"):
        asyncio.run(plex.load_plex_library_inventory(PagedSection(items, [3, 4])))
    with pytest.raises(RuntimeError, match="incomplete"):
        asyncio.run(plex.load_plex_library_inventory(PagedSection(items[:2], [3, 3])))


def test_get_metadata_handles_ambiguous_movie_and_show_paths_and_explicit_seasons():
    plex._plex_cache.clear()
    section = SimpleNamespace(title="Movies", type="movie", uuid="m")
    movie = SimpleNamespace(
        title="Movie",
        year=2020,
        type="movie",
        ratingKey="1",
        librarySection=section,
        guids=[SimpleNamespace(id="tmdb://10")],
        iterParts=lambda: [
            SimpleNamespace(file="/one/Movie/file.mkv"),
            SimpleNamespace(file="/two/Movie/file.mkv"),
        ],
    )
    metadata = asyncio.run(plex.get_plex_metadata(movie))
    assert metadata["movie_path"] is None and metadata["tmdb_id"] == "10"
    assert asyncio.run(plex.get_plex_metadata(movie)) is metadata

    episode = SimpleNamespace(
        seasonNumber=1,
        episodeNumber=1,
        parentThumb=None,
        media=[
            SimpleNamespace(parts=[SimpleNamespace(file="/one/Show/S01/a.mkv")]),
            SimpleNamespace(parts=[SimpleNamespace(file="/two/Show/S01/b.mkv")]),
        ],
    )
    season = SimpleNamespace(index=1, thumb="/season-thumb")
    show = SimpleNamespace(
        title="Show",
        year=2020,
        type="show",
        ratingKey="2",
        librarySection=SimpleNamespace(title="Shows", type="show"),
        guids=[],
        locations=[],
        episodes=lambda: [episode],
        seasons=lambda: [season],
    )
    metadata = asyncio.run(plex.get_plex_metadata(show))
    assert metadata["show_path"] is None
    assert metadata["season_path_errors"][1]
    assert metadata["plex_artwork"]["seasons"][1] == "/season-thumb"
