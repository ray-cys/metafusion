import asyncio
import copy
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import metafusion
from helper import identity_diagnostics, state_db
from helper.config import DEFAULT_CONFIG
from helper.identity import plex_identity_fingerprint
from helper.state_db import (
    inspect_identity_binding,
    load_identity_binding,
    save_identity_binding,
)
from modules import builder, processing

UTC = timezone.utc


def complete_config(tmp_path, mode="kometa"):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["settings"]["mode"] = mode
    config["settings"]["path"] = str(tmp_path / "kometa")
    config["plex"]["url"] = "http://plex:32400"
    config["plex"]["token"] = "token"
    config["tmdb"]["api_key"] = "key"
    config["plex_libraries"] = ["Movies"]
    return config


def test_identity_history_records_only_transitions_and_is_bounded(monkeypatch, tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    monkeypatch.setattr(state_db, "IDENTITY_HISTORY_LIMIT", 3)
    started = datetime(2026, 8, 18, 1, 0, tzinfo=UTC)

    assert save_identity_binding(
        "server",
        "library",
        "10",
        "movie",
        "100",
        "fingerprint-one",
        source="plex_tmdb_guid",
        match_reason="matched",
        path=database,
        now=started,
    )
    assert save_identity_binding(
        "server",
        "library",
        "10",
        "movie",
        "100",
        "fingerprint-one",
        source="plex_tmdb_guid",
        match_reason="matched",
        path=database,
        now=started + timedelta(minutes=1),
    )
    first = inspect_identity_binding(
        "server",
        "library",
        "10",
        current_fingerprint="fingerprint-one",
        path=database,
    )
    assert first["status"] == "current"
    assert [event["event_type"] for event in first["history"]] == ["established"]

    assert load_identity_binding(
        "server",
        "library",
        "10",
        "fingerprint-two",
        path=database,
        record_mismatch=True,
    ) is None
    assert load_identity_binding(
        "server",
        "library",
        "10",
        "fingerprint-two",
        path=database,
        record_mismatch=True,
    ) is None
    stale = inspect_identity_binding(
        "server",
        "library",
        "10",
        current_fingerprint="fingerprint-two",
        path=database,
    )
    assert stale["status"] == "stale"
    assert [event["event_type"] for event in stale["history"]] == [
        "bypassed",
        "established",
    ]

    assert save_identity_binding(
        "server",
        "library",
        "10",
        "movie",
        "200",
        "fingerprint-two",
        source="imdb_external_id",
        match_reason="matched IMDb; matched",
        path=database,
        now=started + timedelta(minutes=2),
    )
    replaced = inspect_identity_binding(
        "server",
        "library",
        "10",
        current_fingerprint="fingerprint-two",
        path=database,
    )
    assert replaced["active"]["tmdb_id"] == "200"
    assert replaced["active"]["source"] == "imdb_external_id"
    assert [event["event_type"] for event in replaced["history"]] == [
        "established",
        "invalidated",
        "bypassed",
    ]


def test_schema_four_binding_can_be_inspected_without_upgrade_or_history(tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE identity_bindings (
                server_id TEXT NOT NULL,
                library_uuid TEXT NOT NULL,
                rating_key TEXT NOT NULL,
                media_type TEXT NOT NULL,
                tmdb_id TEXT NOT NULL,
                plex_fingerprint TEXT NOT NULL,
                confidence TEXT NOT NULL,
                title TEXT,
                year INTEGER,
                validated_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL,
                PRIMARY KEY (server_id, library_uuid, rating_key)
            ) WITHOUT ROWID;
            INSERT INTO identity_bindings VALUES (
                'server', 'library', '10', 'movie', '100', 'fingerprint',
                'high', 'Example', 2020, 'now', 'now'
            );
            PRAGMA user_version = 4;
            """
        )
    inspected = inspect_identity_binding(
        "server",
        "library",
        "10",
        current_fingerprint="fingerprint",
        path=database,
    )
    assert inspected["status"] == "current"
    assert inspected["history_available"] is False
    assert inspected["history"] == []
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4


def test_schema_four_identity_writer_remains_compatible_after_current_upgrade(tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    save_identity_binding(
        "server", "library", "10", "movie", "100", "fingerprint", path=database
    )
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == state_db.SCHEMA_VERSION
        )
        connection.execute(
            """
            INSERT INTO identity_bindings(
                server_id, library_uuid, rating_key, media_type, tmdb_id,
                plex_fingerprint, confidence, title, year,
                validated_at, last_used_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "server",
                "library",
                "11",
                "movie",
                "101",
                "older-image-fingerprint",
                "high",
                "Older image write",
                2020,
                "now",
                "now",
            ),
        )
        assert connection.execute(
            "SELECT tmdb_id FROM identity_bindings WHERE rating_key = '11'"
        ).fetchone()[0] == "101"


def test_identity_inspection_handles_missing_unverifiable_and_noop_state(tmp_path):
    database = tmp_path / "missing" / "meta_db.sqlite3"
    missing = inspect_identity_binding(
        "server", "library", "10", current_fingerprint="fingerprint", path=database
    )
    assert missing == {
        "status": "missing",
        "active": None,
        "history": [],
        "history_available": False,
        "schema_version": None,
    }
    assert not database.exists()
    assert not save_identity_binding(
        "server", "library", "10", "movie", None, "fingerprint", path=database
    )

    connection = state_db._connect(database, writable=True)
    connection.close()
    assert state_db.record_identity_binding_mismatch(
        "server", "library", "10", "fingerprint", path=database
    ) is False
    save_identity_binding(
        "server", "library", "10", "movie", "100", "fingerprint", path=database
    )
    unverifiable = inspect_identity_binding(
        "server", "library", "10", current_fingerprint=None, path=database
    )
    assert unverifiable["status"] == "unverifiable"
    assert state_db.record_identity_binding_mismatch(
        "server", "library", "10", "fingerprint", path=database
    ) is False


def test_normal_processing_records_mismatch_without_changing_active_binding(
    monkeypatch, tmp_path
):
    database = tmp_path / "meta_db.sqlite3"
    monkeypatch.setattr(state_db, "STATE_DATABASE", database)
    monkeypatch.setattr(processing, "load_identity_binding", load_identity_binding)
    original = {
        "library_type": "movie",
        "plex_provider_tmdb_id": "100",
        "imdb_id": "tt100",
    }
    original_fingerprint = plex_identity_fingerprint(original)
    save_identity_binding(
        "server",
        "library",
        "10",
        "movie",
        "100",
        original_fingerprint,
        path=database,
    )
    changed = {
        **original,
        "server_id": "server",
        "library_uuid": "library",
        "ratingKey": "10",
        "imdb_id": "tt200",
        "tmdb_id": "100",
    }
    assert processing.apply_learned_tmdb_identity(
        changed, touch=False, record_mismatch=False
    ) is False
    before_recording = inspect_identity_binding(
        "server",
        "library",
        "10",
        current_fingerprint=plex_identity_fingerprint(changed),
        path=database,
    )
    assert [event["event_type"] for event in before_recording["history"]] == [
        "established"
    ]
    assert processing.apply_learned_tmdb_identity(
        changed, touch=False, record_mismatch=True
    ) is False
    inspected = inspect_identity_binding(
        "server",
        "library",
        "10",
        current_fingerprint=plex_identity_fingerprint(changed),
        path=database,
    )
    assert inspected["active"]["tmdb_id"] == "100"
    assert inspected["history"][0]["event_type"] == "bypassed"


def test_identity_diagnosis_explains_provider_binding_titles_and_destinations(
    monkeypatch, tmp_path
):
    config = complete_config(tmp_path)
    item = SimpleNamespace(
        ratingKey="10",
        title="Localized Plex Title",
        originalTitle="Original Plex Title",
        year=2020,
        guid="plex://movie/example",
        guids=[SimpleNamespace(id="tmdb://100"), SimpleNamespace(id="imdb://tt100")],
    )
    meta = {
        "server_id": "server",
        "library_uuid": "library",
        "library_name": "Movies",
        "library_type": "movie",
        "ratingKey": "10",
        "title": item.title,
        "year": 2020,
        "edition_title": "Director's Cut",
        "plex_provider_tmdb_id": "100",
        "tmdb_id": "100",
        "imdb_id": "tt100",
        "tvdb_id": None,
        "movie_path": "Localized Plex Title (2020)",
        "movie_dir": str(tmp_path / "media" / "Localized Plex Title (2020)"),
    }
    async def metadata(*_args, **_kwargs):
        return copy.deepcopy(meta)

    async def tmdb(_config, endpoint, **kwargs):
        assert endpoint == "movie/100"
        assert kwargs["cache"] is False
        return {
            "id": 100,
            "title": "Localized TMDb Title",
            "original_title": "Original TMDb Title",
            "release_date": "2020-01-02",
            "external_ids": {"imdb_id": "tt100"},
        }

    monkeypatch.setattr(identity_diagnostics, "get_plex_metadata", metadata)
    monkeypatch.setattr(identity_diagnostics, "tmdb_api_request", tmdb)
    monkeypatch.setattr(
        identity_diagnostics,
        "inspect_identity_binding",
        lambda *_args, **_kwargs: {
            "status": "current",
            "active": {
                "tmdb_id": "100",
                "confidence": "high",
                "source": "plex_tmdb_guid",
                "match_reason": "matched IMDb; matched",
            },
            "history": [
                {
                    "occurred_at": "2026-08-18T01:00:00+00:00",
                    "event_type": "established",
                    "reason_code": "first_high_confidence_match",
                    "previous_tmdb_id": None,
                    "tmdb_id": "100",
                    "source": "plex_tmdb_guid",
                    "reason": "matched IMDb; matched",
                }
            ],
        },
    )

    record = asyncio.run(
        identity_diagnostics.diagnose_identity(
            item,
            config,
            session=object(),
            identity_counts={
                ("movie", "Localized Plex Title", 2020): 2,
            },
            edition_counts={
                ("Localized Plex Title", 2020, "Director's Cut"): 1,
            },
        )
    )
    assert record["status"] == "accepted"
    assert record["selection"]["source"] == "plex_tmdb_guid"
    assert record["selection"]["confidence"] == "high"
    assert record["plex"]["original_title"] == "Original Plex Title"
    assert record["tmdb"]["original_title"] == "Original TMDb Title"
    assert record["metadata_destination"]["path"].endswith("movie_metadata.yml")
    assert record["metadata_destination"]["entry"].endswith("[Director's Cut]")
    assert record["artwork_destinations"]["poster"]["path"].endswith(
        "poster.jpg"
    )

    report = identity_diagnostics.write_identity_inspection_report(
        [record], base_dir=tmp_path
    )
    contents = report.read_text(encoding="utf-8")
    assert "No binding, cache, metadata, artwork" in contents
    assert "Plex original title: Original Plex Title" in contents
    assert "TMDb original title: Original TMDb Title" in contents
    assert "Binding established by: plex_tmdb_guid" in contents
    assert "Director's Cut" in contents


def test_plex_mode_identity_diagnosis_reports_api_and_mapped_tv_destinations(
    monkeypatch, tmp_path
):
    config = complete_config(tmp_path, mode="plex")
    show_dir = tmp_path / "media" / "Example Show"
    specials_dir = show_dir / "Specials"
    season_dir = show_dir / "Season 01"
    for directory in (show_dir, specials_dir, season_dir):
        directory.mkdir(parents=True, exist_ok=True)
    item = SimpleNamespace(
        ratingKey="20",
        title="Example Show",
        originalTitle="Original Example Show",
        year=2024,
        guid="plex://show/example",
        guids=[SimpleNamespace(id="tmdb://200"), SimpleNamespace(id="tvdb://2000")],
    )
    meta = {
        "server_id": "server",
        "library_uuid": "library",
        "library_name": "TV Shows",
        "library_type": "show",
        "ratingKey": "20",
        "title": item.title,
        "year": 2024,
        "plex_provider_tmdb_id": "200",
        "tmdb_id": "200",
        "imdb_id": "tt200",
        "tvdb_id": "2000",
        "show_dir": str(show_dir),
        "season_dirs": {"0": str(specials_dir), 1: str(season_dir)},
    }

    async def metadata(*_args, **_kwargs):
        return copy.deepcopy(meta)

    async def tmdb(_config, endpoint, **kwargs):
        assert endpoint == "tv/200"
        assert kwargs["cache"] is False
        return {
            "id": 200,
            "name": "Example Show",
            "original_name": "Original Example Show",
            "first_air_date": "2024-01-01",
            "external_ids": {"imdb_id": "tt200", "tvdb_id": 2000},
        }

    monkeypatch.setattr(identity_diagnostics, "get_plex_metadata", metadata)
    monkeypatch.setattr(identity_diagnostics, "tmdb_api_request", tmdb)
    monkeypatch.setattr(
        identity_diagnostics,
        "inspect_identity_binding",
        lambda *_args, **_kwargs: {
            "status": "missing",
            "active": None,
            "history": [],
        },
    )

    record = asyncio.run(
        identity_diagnostics.diagnose_identity(item, config, session=object())
    )

    assert record["status"] == "accepted"
    assert record["selection"]["confidence"] == "high"
    assert record["metadata_destination"] == {
        "kind": "plex_api",
        "path": "/library/metadata/20",
        "entry": "Plex item fields",
    }
    artwork = record["artwork_destinations"]
    assert artwork["poster"]["path"] == str(show_dir / "poster.jpg")
    assert artwork["poster"]["directory_writable"] is True
    assert artwork["seasons"] == [
        {
            "season": 0,
            "path": str(specials_dir / "season-specials-poster.jpg"),
            "directory_exists": True,
            "directory_writable": True,
        },
        {
            "season": 1,
            "path": str(season_dir / "Season01.jpg"),
            "directory_exists": True,
            "directory_writable": True,
        },
    ]


def test_identity_diagnosis_recovers_stale_provider_id_without_cache(
    monkeypatch, tmp_path
):
    config = complete_config(tmp_path, mode="plex")
    item = SimpleNamespace(
        ratingKey="30",
        title="Recovered Movie",
        originalTitle=None,
        year=2023,
        guid="plex://movie/recovered",
        guids=[SimpleNamespace(id="tmdb://100"), SimpleNamespace(id="imdb://tt200")],
    )
    meta = {
        "server_id": "server",
        "library_uuid": "library",
        "library_name": "Movies",
        "library_type": "movie",
        "ratingKey": "30",
        "title": item.title,
        "year": 2023,
        "plex_provider_tmdb_id": "100",
        "tmdb_id": "100",
        "imdb_id": "tt200",
        "tvdb_id": None,
        "movie_dir": str(tmp_path / "missing-media"),
    }
    endpoints = []

    async def metadata(*_args, **_kwargs):
        return copy.deepcopy(meta)

    async def tmdb(_config, endpoint, **kwargs):
        endpoints.append(endpoint)
        assert kwargs["cache"] is False
        if endpoint == "movie/100":
            return None
        return {
            "id": 200,
            "title": "Recovered Movie",
            "original_title": "Recovered Movie",
            "release_date": "2023-03-01",
            "external_ids": {"imdb_id": "tt200"},
        }

    async def resolve(_config, media_type, **kwargs):
        assert media_type == "movie"
        assert kwargs["imdb_id"] == "tt200"
        assert kwargs["excluded_ids"] == {"100"}
        assert kwargs["cache"] is False
        return "200"

    monkeypatch.setattr(identity_diagnostics, "get_plex_metadata", metadata)
    monkeypatch.setattr(identity_diagnostics, "tmdb_api_request", tmdb)
    monkeypatch.setattr(identity_diagnostics, "resolve_tmdb_id", resolve)
    monkeypatch.setattr(
        identity_diagnostics,
        "inspect_identity_binding",
        lambda *_args, **_kwargs: {"status": "missing", "active": None, "history": []},
    )

    record = asyncio.run(
        identity_diagnostics.diagnose_identity(item, config, session=object())
    )

    assert endpoints == ["movie/100", "movie/200"]
    assert record["status"] == "accepted"
    assert record["selection"]["tmdb_id"] == "200"
    assert record["selection"]["source"] == "stale_identity_recovery_via_imdb_external_id"
    assert record["artwork_destinations"]["poster"] == {
        "path": str(tmp_path / "missing-media" / "poster.jpg"),
        "directory_exists": False,
        "directory_writable": False,
    }


def test_identity_diagnosis_reports_unresolved_without_external_ids(
    monkeypatch, tmp_path
):
    config = complete_config(tmp_path, mode="plex")
    item = SimpleNamespace(
        ratingKey="40",
        title="Unknown Movie",
        originalTitle=None,
        year=2025,
        guid=None,
        guids=[SimpleNamespace(id=f"provider://{index}") for index in range(25)],
    )
    meta = {
        "server_id": "server",
        "library_uuid": "library",
        "library_name": "Movies",
        "library_type": "movie",
        "ratingKey": "40",
        "title": item.title,
        "year": 2025,
        "plex_provider_tmdb_id": None,
        "tmdb_id": None,
        "imdb_id": None,
        "tvdb_id": None,
        "movie_dir": None,
    }
    searches = []

    async def metadata(*_args, **_kwargs):
        return copy.deepcopy(meta)

    async def resolve(_config, media_type, **kwargs):
        searches.append((media_type, kwargs["title"], kwargs["year"], kwargs["cache"]))
        return None

    monkeypatch.setattr(identity_diagnostics, "get_plex_metadata", metadata)
    monkeypatch.setattr(identity_diagnostics, "resolve_tmdb_id", resolve)
    monkeypatch.setattr(
        identity_diagnostics,
        "inspect_identity_binding",
        lambda *_args, **_kwargs: {"status": "missing", "active": None, "history": []},
    )

    record = asyncio.run(
        identity_diagnostics.diagnose_identity(item, config, session=object())
    )

    assert len(searches) == 2
    assert all(search == ("movie", "Unknown Movie", 2025, False) for search in searches)
    assert record["status"] == "unresolved"
    assert record["selection"]["source"] == "unresolved"
    assert len(record["plex"]["guids"]) == 20
    assert record["artwork_destinations"]["poster"]["path"] is None


def test_identity_inspection_runner_reports_missing_keys(monkeypatch, tmp_path):
    item = SimpleNamespace(ratingKey="10")

    async def inventory(_section, _runtime, records_only=False):
        if records_only:
            return [
                {
                    "rating_key": "10",
                    "title": "Example",
                    "year": 2020,
                    "media_type": "movie",
                    "edition": None,
                }
            ]
        return [item]

    async def diagnose(_item, *_args, **_kwargs):
        return {
            "status": "accepted",
            "library": "Movies",
            "rating_key": "10",
            "media_type": "movie",
            "plex": {"localized_title": "Example", "year": 2020},
            "selection": {"tmdb_id": "100"},
            "tmdb": {},
            "binding": {"status": "missing", "history": []},
            "metadata_destination": {},
            "artwork_destinations": {},
        }

    monkeypatch.setattr(identity_diagnostics, "load_plex_library_inventory", inventory)
    monkeypatch.setattr(identity_diagnostics, "diagnose_identity", diagnose)
    records, report = asyncio.run(
        identity_diagnostics.run_identity_inspection(
            [SimpleNamespace(title="Movies")],
            complete_config(tmp_path),
            ["10", "missing"],
            session=object(),
            base_dir=tmp_path,
        )
    )
    assert [record["status"] for record in records] == ["accepted", "not_found"]
    assert "Plex rating key: missing" in report.read_text(encoding="utf-8")


def test_identity_connector_uses_one_bounded_session(monkeypatch, tmp_path):
    config = complete_config(tmp_path)
    plex = SimpleNamespace(machineIdentifier="server")
    section = SimpleNamespace(title="Movies")
    calls = []

    class Session:
        async def __aenter__(self):
            calls.append("enter")
            return self

        async def __aexit__(self, *_args):
            calls.append("exit")
            return False

    async def preflight(_config, session):
        assert isinstance(session, Session)
        return plex

    async def run(sections, _config, keys, session=None):
        assert sections == [section]
        assert keys == ["10"]
        assert isinstance(session, Session)
        return ([{"status": "accepted"}], tmp_path / "identity.txt")

    monkeypatch.setattr(metafusion.aiohttp, "ClientSession", lambda **_kwargs: Session())
    monkeypatch.setattr(metafusion.aiohttp, "TCPConnector", lambda **_kwargs: object())
    monkeypatch.setattr(metafusion, "preflight_connectors", preflight)
    monkeypatch.setattr(
        metafusion,
        "connect_plex_library",
        lambda _config, plex=None: ([section], ["Movies"], []),
    )
    monkeypatch.setattr(metafusion, "run_identity_inspection", run)

    result = asyncio.run(metafusion.identity_inspection_connectors(config, ["10"]))
    assert result[0][0]["status"] == "accepted"
    assert calls == ["enter", "exit"]


def test_identity_cli_is_standalone_read_only_and_handles_connector_errors(
    monkeypatch, tmp_path, capsys
):
    config = complete_config(tmp_path)
    report = tmp_path / "identity.txt"
    monkeypatch.setattr(
        metafusion,
        "load_config_file",
        lambda **_kwargs: (copy.deepcopy(config), {}),
    )
    monkeypatch.setattr(metafusion, "validate_config", lambda _config: [])
    monkeypatch.setattr(metafusion, "validate_preflight_paths", lambda *_args: None)
    monkeypatch.setattr(
        metafusion,
        "begin_tmdb_cache",
        lambda *_args: (_ for _ in ()).throw(AssertionError("cache must not open")),
    )
    monkeypatch.setattr(metafusion.tmdb_response_cache, "reset_memory", lambda: None)

    async def success(_config, keys):
        assert keys == ["10"]
        return ([{"status": "accepted"}], report)

    monkeypatch.setattr(metafusion, "identity_inspection_connectors", success)
    assert metafusion.main(["--identity-inspect", "--rating-key", "10"]) == 0
    assert "0 require review" in capsys.readouterr().out

    async def failure(_config, _keys):
        raise RuntimeError("connector unavailable")

    monkeypatch.setattr(metafusion, "identity_inspection_connectors", failure)
    assert metafusion.main(["--identity-inspect", "--rating-key", "10"]) == 1
    assert "connector unavailable" in capsys.readouterr().err

    assert metafusion.main(["--identity-inspect"]) == 2
    assert "requires --rating-key" in capsys.readouterr().err
    assert (
        metafusion.main(
            [
                "--identity-inspect",
                "--mapping-diagnose",
                "--rating-key",
                "10",
            ]
        )
        == 2
    )
    assert (
        metafusion.main(
            ["--identity-inspect", "--metafusion_run", "--rating-key", "10"]
        )
        == 2
    )


def test_binding_source_classification_is_deterministic():
    assert (
        builder._identity_binding_source(
            {"plex_provider_tmdb_id": "100"}, "100"
        )
        == "plex_tmdb_guid"
    )
    assert (
        builder._identity_binding_source({}, "100", recovered=True)
        == "stale_tmdb_recovery"
    )
    assert (
        builder._identity_binding_source({}, "100", split_mapping=True)
        == "split_series_mapping"
    )
    assert (
        builder._identity_binding_source(
            {}, "100", consensus_reason="matched IMDb"
        )
        == "imdb_external_id"
    )
