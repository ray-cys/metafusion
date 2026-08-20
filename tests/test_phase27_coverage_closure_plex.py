import asyncio
from types import SimpleNamespace

import pytest

from helper import plex, plex_metadata


class Editable:
    def __init__(self, **values):
        self.ratingKey = values.pop("ratingKey", "1")
        self.type = values.pop("type", "movie")
        self._locks = values.pop("locks", {})
        for key, value in values.items():
            setattr(self, key, value)

    def reload(self):
        return self

    def isLocked(self, field):
        return self._locks.get(field, False)

    def batchEdits(self):
        return self

    def editField(self, field, value, locked=False):
        setattr(self, field, value)
        self._locks[field] = locked
        return self

    def editTags(self, field, values, locked=False, remove=False):
        attribute = plex_metadata.TAG_ATTRIBUTES.get(field, f"{field}s")
        current = plex_metadata._clean_tags(getattr(self, attribute, []))
        if remove:
            removed = {str(value).casefold() for value in values}
            current = [value for value in current if value.casefold() not in removed]
        else:
            current.extend(plex_metadata._missing_tags(current, values))
        setattr(self, attribute, [SimpleNamespace(tag=value) for value in current])
        self._locks[field] = locked
        return self

    def saveEdits(self):
        return self


def _config(policy="managed", dry_run=False):
    return {
        "settings": {"mode": "plex", "dry_run": dry_run},
        "plex_metadata": {
            "enabled": True,
            "policy": policy,
            "lock_writes": False,
            "lock_merged_tags": False,
            "max_writes_per_run": 100,
            "fields": [],
        },
        "runtime": {"plex_retries": 1, "plex_retry_delay": 0},
    }


def _identity():
    return {
        "server_id": "server",
        "library_uuid": "library",
        "library_name": "Movies",
        "rating_key": "1",
        "media_type": "movie",
    }


def _owner(
    field,
    kind="scalar",
    *,
    applied="applied",
    original="original",
    owned=None,
    locked=False,
):
    return plex_metadata._record_payload(
        _identity(),
        "",
        field,
        kind,
        original,
        applied,
        owned if owned is not None else [applied],
        False,
        locked,
    )


def test_plex_directory_retry_and_cached_extraction_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(
        plex.os.path,
        "commonpath",
        lambda _paths: (_ for _ in ()).throw(ValueError("different drives")),
    )
    assert plex._discover_show_directory(["/one/Season 01", "/two/Season 02"]) is None
    monkeypatch.undo()
    root = tmp_path / "Show"
    assert plex._discover_show_directory([root, root / "Season 01"]) == root

    sleeps = []
    attempts = []

    def sections():
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("temporary")
        return [SimpleNamespace(title="Movies", type="movie")]

    monkeypatch.setattr(plex.time, "sleep", lambda value: sleeps.append(value))
    selected, *_rest = plex.connect_plex_library(
        {"runtime": {"plex_retries": 2, "plex_retry_delay": 1}},
        ["Movies"],
        plex=SimpleNamespace(library=SimpleNamespace(sections=sections)),
    )
    assert selected and sleeps == [1]

    plex._plex_cache.clear()
    part = SimpleNamespace(file=str(tmp_path / "Movie" / "movie.mkv"))
    movie = SimpleNamespace(
        title="Movie",
        year=2026,
        ratingKey="movie",
        type="movies",
        librarySection=SimpleNamespace(title="Movies", type="movies"),
        guids=[],
        iterParts=lambda: pytest.fail("cached parts should be reused"),
    )
    metadata = asyncio.run(plex.get_plex_metadata(movie, _movie_cache={"movie": [part]}))
    assert metadata["library_type"] == "movie"

    plex._plex_cache.clear()
    episode = SimpleNamespace(
        seasonNumber=1,
        episodeNumber=1,
        parentThumb=None,
        media=[SimpleNamespace(parts=[SimpleNamespace(file=None)])],
    )
    season = SimpleNamespace(index=None, seasonNumber=None, thumb=None)
    show = SimpleNamespace(
        title="Show",
        year=2026,
        ratingKey="show",
        type="show",
        librarySection=SimpleNamespace(title="Shows", type="show"),
        guids=[],
        locations=["/one/Show", "/two/Show"],
        episodes=lambda: pytest.fail("cached episodes should be reused"),
        seasons=lambda: pytest.fail("cached seasons should be reused"),
    )
    metadata = asyncio.run(
        plex.get_plex_metadata(
            show,
            _episode_cache={"show": [episode]},
            _season_cache={"show": [season]},
        )
    )
    assert metadata["show_dir"] is None


def test_plex_extraction_failures_and_incomplete_episode_records(monkeypatch):
    events = []
    monkeypatch.setattr(plex, "log_plex_event", lambda event, **_kwargs: events.append(event))

    class BrokenSection:
        title = "Broken"

        @property
        def type(self):
            raise RuntimeError("type")

    class BrokenGuids:
        title = "Broken"
        year = 2026
        type = "movie"
        ratingKey = "broken"
        librarySection = BrokenSection()

        @property
        def guids(self):
            raise RuntimeError("guids")

    plex._plex_cache.clear()
    asyncio.run(plex.get_plex_metadata(BrokenGuids()))
    assert "plex_failed_extract_library_type" in events
    assert "plex_failed_extract_ids" in events

    class BrokenNumber:
        title = "Show"
        year = 2026
        type = "show"
        ratingKey = "numbers"
        librarySection = SimpleNamespace(title="Shows", type="show")
        guids = []
        locations = []

        def episodes(self):
            return [
                SimpleNamespace(
                    seasonNumber=None,
                    parentIndex=None,
                    episodeNumber=None,
                    index=None,
                    media=[],
                )
            ]

        def seasons(self):
            raise RuntimeError("seasons")

    plex._plex_cache.clear()
    result = asyncio.run(plex.get_plex_metadata(BrokenNumber()))
    assert result["seasons_episodes"] == {}
    assert "plex_failed_extract_seasons" in events

    class ExplodingEpisode:
        media = []

        @property
        def seasonNumber(self):
            raise RuntimeError("season number")

    plex._plex_cache.clear()
    broken = SimpleNamespace(
        title="Show",
        year=2026,
        type="show",
        ratingKey="explode",
        librarySection=SimpleNamespace(title="Shows", type="show"),
        guids=[],
        locations=[],
        episodes=lambda: [ExplodingEpisode()],
    )
    asyncio.run(plex.get_plex_metadata(broken))
    assert "plex_failed_extract_show_path" in events
    assert "plex_failed_extract_seasons_episodes" in events


def test_plex_metadata_policy_exclusion_and_managed_tag_ownership():
    config = _config()
    config["plex_metadata"]["fields"] = ["summary"]
    reporter = plex_metadata.PlexMetadataReporter(config)
    writes, _records = plex_metadata._apply_object(
        Editable(genres=[]),
        {"tags": {"genre": ["Drama"]}},
        "",
        _identity(),
        {},
        config["plex_metadata"],
        reporter,
        "Movie",
        False,
    )
    assert writes == 0 and reporter.counts["policy_excluded"] == 1

    config["plex_metadata"]["fields"] = []
    owner = _owner("genre", "tag", applied=["Old"], original=[], owned=["Old"])
    writes, records = plex_metadata._apply_object(
        Editable(genres=[SimpleNamespace(tag="Old")]),
        {"tags": {"genre": ["New"]}},
        "",
        _identity(),
        {("", "genre"): owner},
        config["plex_metadata"],
        reporter,
        "Movie",
        False,
    )
    assert writes == 1
    assert records[0]["owned_values"]["values"] == ["New"]


def test_plex_metadata_child_failure_rollback_failure_and_retry_exhaustion(monkeypatch, caplog):
    config = _config()
    reporter = plex_metadata.PlexMetadataReporter(config)
    item = Editable(title="Movie")
    monkeypatch.setattr(plex_metadata, "load_plex_metadata_ownership", lambda *_a: {})
    monkeypatch.setattr(plex_metadata, "_existing_children", lambda root: {"": root})
    monkeypatch.setattr(
        plex_metadata,
        "_apply_object",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("write")),
    )
    result = plex_metadata._apply_candidate(item, {"root": {}}, config, {}, reporter)
    assert result["failures"] == 1

    monkeypatch.setattr(
        plex_metadata,
        "_apply_object",
        lambda *_args, **_kwargs: (1, [_owner("summary")]),
    )
    monkeypatch.setattr(
        plex_metadata,
        "save_plex_metadata_ownership",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ledger")),
    )
    monkeypatch.setattr(
        plex_metadata,
        "_rollback_untracked_write",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("rollback")),
    )
    result = plex_metadata._apply_candidate(item, {"root": {}}, config, {}, reporter)
    assert result["failures"] == 1 and "roll back" in caplog.text

    monkeypatch.setattr(
        plex_metadata,
        "_apply_candidate",
        lambda *_args: {"writes": 0, "failures": 1, "deferred": 2},
    )
    exhausted = asyncio.run(
        plex_metadata.apply_plex_metadata(item, {"root": {}}, config, {"title": "Movie"})
    )
    assert exhausted == {"writes": 0, "failures": 1, "deferred": 2}

    dry = _config(dry_run=True)
    monkeypatch.setattr(
        plex_metadata,
        "_apply_candidate",
        lambda *_args: {"writes": 0, "failures": 0},
    )
    assert (
        asyncio.run(plex_metadata.apply_plex_metadata(item, {"root": {}}, dry, {"title": "Movie"}))[
            "failures"
        ]
        == 0
    )


def test_plex_metadata_existing_children_invalid_indexes():
    episode = SimpleNamespace(index=None, episodeNumber=None)
    season = SimpleNamespace(index=None, seasonNumber=None, episodes=lambda: [episode])
    root = SimpleNamespace(type="show", seasons=lambda: [season])
    assert plex_metadata._existing_children(root) == {"": root}

    season = SimpleNamespace(index=1, episodes=lambda: [episode])
    root = SimpleNamespace(type="show", seasons=lambda: [season])
    assert set(plex_metadata._existing_children(root)) == {"", "season:1"}


def test_plex_metadata_restore_decisions_and_verification_failures(monkeypatch):
    config = _config()
    reporter = plex_metadata.PlexMetadataReporter(config)
    child = Editable(
        summary="manual",
        tagline="applied",
        genres=[SimpleNamespace(tag="manual")],
        countries=[SimpleNamespace(tag="applied")],
    )
    ownership = {
        ("", "summary"): _owner("summary", applied="applied"),
        ("", "tagline"): _owner("tagline", applied="applied", locked=False),
        ("", "genre"): _owner("genre", "tag", applied=["applied"], original=["original"]),
        ("", "country"): _owner("country", "tag", applied=["applied"], original=[], locked=False),
    }
    monkeypatch.setattr(plex_metadata, "load_plex_metadata_ownership", lambda *_a: ownership)
    monkeypatch.setattr(plex_metadata, "_existing_children", lambda _item: {"": child})
    result = plex_metadata._restore_candidate(
        child, config, {"title": "Movie"}, reporter, unlock_only=True
    )
    assert result["failures"] == 0
    assert reporter.counts["conflict"] >= 2
    assert reporter.counts["unchanged"] >= 2

    child = Editable(
        genres=[SimpleNamespace(tag="applied")],
        locks={"genre": True},
    )
    ownership = {("", "genre"): _owner("genre", "tag", applied=["applied"], original=["original"])}
    monkeypatch.setattr(plex_metadata, "load_plex_metadata_ownership", lambda *_a: ownership)
    monkeypatch.setattr(plex_metadata, "_existing_children", lambda _item: {"": child})
    result = plex_metadata._restore_candidate(child, config, {"title": "Movie"}, reporter)
    assert result["writes"] == 1

    def failed_restore(item):
        monkeypatch.setattr(plex_metadata, "_existing_children", lambda _root: {"": item})
        return plex_metadata._restore_candidate(item, config, {"title": "Movie"}, reporter)

    lock_reject = Editable(summary="applied")
    lock_reject.isLocked = lambda _field: True
    monkeypatch.setattr(
        plex_metadata,
        "load_plex_metadata_ownership",
        lambda *_a: {("", "summary"): _owner("summary")},
    )
    assert failed_restore(lock_reject)["failures"] == 1

    scalar_reject = Editable(summary="applied")
    scalar_reject.editField = lambda *_args, **_kwargs: scalar_reject
    assert failed_restore(scalar_reject)["failures"] == 1

    tag_reject = Editable(genres=[SimpleNamespace(tag="applied")])
    tag_reject.editTags = lambda *_args, **_kwargs: tag_reject
    monkeypatch.setattr(
        plex_metadata,
        "load_plex_metadata_ownership",
        lambda *_a: {
            ("", "genre"): _owner("genre", "tag", applied=["applied"], original=["original"])
        },
    )
    assert failed_restore(tag_reject)["failures"] == 1

    monkeypatch.setattr(
        plex_metadata,
        "_restore_candidate",
        lambda *_args, **_kwargs: {"writes": 1, "failures": 0},
    )
    assert asyncio.run(plex_metadata.restore_plex_metadata(Editable(), config, {})) == {
        "writes": 1,
        "failures": 0,
    }
