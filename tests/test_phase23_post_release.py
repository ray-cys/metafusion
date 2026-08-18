import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

import metafusion
from helper import mapping_diagnostics
from helper.config import DEFAULT_CONFIG
from helper.diagnostics import write_asset_audit_report
from helper.plex import (
    _external_ids,
    load_plex_library_inventory,
    plex_inventory_record,
)
from helper.provider_mappings import resolve_split_series_mapping
from helper.provider_replay import (
    load_provider_replay,
    provider_replay_issues,
    sanitize_provider_payload,
)
from helper.tmdb import (
    resolve_episode_group_mapping,
    tmdb_external_id_consensus,
    tmdb_identity_consistent,
)
from modules import builder
from modules.utils import (
    artwork_candidate_explanations,
    get_best_poster,
)

REPLAY_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "provider_replays"
    / "core-provider-cases.json"
)


def complete_config():
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["plex"]["url"] = "http://plex:32400"
    config["plex"]["token"] = "token"
    config["tmdb"]["api_key"] = "key"
    config["tmdb"]["fallback"] = ["zh"]
    config["plex_libraries"] = ["Shows"]
    return config


def replay_case(name):
    document = load_provider_replay(REPLAY_FIXTURE)
    return next(case for case in document["cases"] if case["name"] == name)


def plex_item(document):
    return SimpleNamespace(
        **{
            **document,
            "guids": [SimpleNamespace(id=value) for value in document.get("guids", [])],
        }
    )


def test_provider_replay_pack_is_redacted_and_exercises_identity_regressions():
    document = load_provider_replay(REPLAY_FIXTURE)
    assert document["schema"] == 1
    assert provider_replay_issues(document) == []
    assert len(document["cases"]) == 6

    for case_name in (
        "movie-edition-with-trusted-alias",
        "localized-show-title",
        "title-year-disambiguates-plex-year",
    ):
        case = replay_case(case_name)
        item = plex_item(case["plex"])
        ids = _external_ids(item)
        accepted_ids, trusted, _reason = tmdb_external_id_consensus(
            item.type,
            case["tmdb"]["details"],
            imdb_id=ids["imdb"],
            tvdb_id=ids["tvdb"],
        )
        accepted_identity, reason = tmdb_identity_consistent(
            item.type,
            item.title,
            item.year,
            case["tmdb"]["details"],
            trusted_external_id=trusted,
        )
        assert accepted_ids and accepted_identity
        assert case["expected"]["identity_accepted"] is True
        if case["expected"].get("trusted_external_id"):
            assert trusted is True
        if case["expected"].get("reason_contains"):
            assert case["expected"]["reason_contains"] in reason


def test_tmdb_recovery_preserves_original_when_replacement_disappears(monkeypatch):
    async def request(_config, endpoint, **_kwargs):
        if endpoint == "movie/100":
            return {"external_ids": {"imdb_id": "tt-wrong"}}
        if endpoint == "movie/101":
            return None
        raise AssertionError(endpoint)

    async def resolve(*_args, **kwargs):
        assert kwargs["excluded_ids"] == {"100"}
        return "101"

    monkeypatch.setattr(builder, "tmdb_api_request", request)
    monkeypatch.setattr(builder, "resolve_tmdb_id", resolve)

    resolved, details, recovered = asyncio.run(
        builder.tmdb_details_with_recovery(
            {}, "movie", "100", imdb_id="tt-right", session=object()
        )
    )

    assert resolved == "100"
    assert recovered is None
    assert details["external_ids"]["imdb_id"] == "tt-wrong"


def test_provider_replay_pack_covers_episode_groups_split_series_and_artwork(
    monkeypatch,
):
    episode_case = replay_case("specials-and-alternate-episode-group")

    async def replay_tmdb(_config, endpoint, **_kwargs):
        if endpoint.endswith("/episode_groups"):
            return episode_case["tmdb"]["episode_groups"]
        return episode_case["tmdb"]["episode_group"]

    monkeypatch.setattr("helper.tmdb.tmdb_api_request", replay_tmdb)
    mapping = asyncio.run(
        resolve_episode_group_mapping(
            {"tmdb": {"episode_group_fallback": True}},
            "72844",
            episode_case["plex"]["inventory"],
        )
    )
    assert mapping["group_id"] == episode_case["expected"]["episode_group_id"]
    assert len(mapping["episodes"]) == episode_case["expected"]["mapped_count"]

    split_case = replay_case("split-series-season-source")
    split = resolve_split_series_mapping(
        {
            "tmdb": {
                "split_series_show_policy": "preserve",
                "split_series_mappings": split_case["configuration"][
                    "split_series_mappings"
                ],
            }
        },
        tvdb_id="2000",
    )
    assert split["seasons"][2] == {
        "tmdb_id": split_case["expected"]["mapped_tmdb_id"],
        "season_number": split_case["expected"]["mapped_season"],
    }

    artwork_case = replay_case("missing-preferred-artwork-language")
    config = complete_config()
    poster = get_best_poster(config, artwork_case["tmdb"]["images"]["posters"])
    assert poster["iso_639_1"] == artwork_case["expected"]["poster_language"]
    assert artwork_case["tmdb"]["images"]["backdrops"] == []


def test_provider_replay_sanitizer_redacts_secrets_hosts_paths_and_ids(tmp_path):
    raw = {
        "api_key": "secret",
        "url": "http://192.168.1.2:32400/library?X-Plex-Token=secret",
        "ratingKey": "43335",
        "machineIdentifier": "server-private",
        "locations": ["/mnt/user/media/Movies/Private/Movie.mkv"],
        "nested": {"authorization": "Bearer secret"},
    }
    sanitized = sanitize_provider_payload(raw)
    assert sanitized["api_key"] == "***"
    assert sanitized["nested"]["authorization"] == "***"
    assert sanitized["url"].startswith("http://provider.example.invalid:32400/")
    assert "secret" not in sanitized["url"]
    assert sanitized["locations"] == ["<redacted-media-path>"]
    assert sanitized["ratingKey"].startswith("replay-")
    assert sanitized["machineIdentifier"].startswith("replay-")
    assert provider_replay_issues(sanitized) == []
    assert provider_replay_issues(raw)
    with pytest.raises(ValueError, match="Unsafe provider replay"):
        unsafe = tmp_path / "unsafe.json"
        unsafe.write_text('{"token":"secret"}', encoding="utf-8")
        load_provider_replay(unsafe)


class PagedSection:
    title = "Large Movies"
    type = "movie"

    def __init__(self, items, totals=None):
        self.items = list(items)
        self.calls = []
        self.totals = list(totals or [len(self.items), len(self.items)])

    def totalViewSize(self, includeCollections=False):
        assert includeCollections is False
        return self.totals.pop(0) if len(self.totals) > 1 else self.totals[0]

    def all(self, container_start=0, container_size=200, maxresults=None):
        self.calls.append((container_start, container_size, maxresults))
        size = min(container_size, maxresults or container_size)
        return self.items[container_start : container_start + size]


def inventory_item(index, *, rating_key=None):
    return SimpleNamespace(
        ratingKey=str(index if rating_key is None else rating_key),
        title=f"Movie {index}",
        year=2020,
        type="movie",
        editionTitle=None,
        guids=[SimpleNamespace(id=f"tmdb://{1000 + index}")],
        guid=None,
    )


def test_plex_inventory_is_automatically_paged_and_can_be_lightweight():
    section = PagedSection([inventory_item(index) for index in range(650)])
    records = asyncio.run(
        load_plex_library_inventory(section, {"plex_retries": 1}, records_only=True)
    )
    assert len(records) == 650
    assert section.calls == [
        (0, 200, 200),
        (200, 200, 200),
        (400, 200, 200),
        (600, 200, 200),
    ]
    assert records[0] == plex_inventory_record(section.items[0])
    assert records[0]["tmdb_id"] == "1000"


def test_plex_inventory_fails_closed_on_duplicates_changes_and_missing_pages():
    duplicate = PagedSection(
        [inventory_item(1), inventory_item(2, rating_key="1")]
    )
    with pytest.raises(RuntimeError, match="repeated rating key"):
        asyncio.run(load_plex_library_inventory(duplicate, {"plex_retries": 1}))

    changed = PagedSection(
        [inventory_item(index) for index in range(3)], totals=[3, 4]
    )
    with pytest.raises(RuntimeError, match="changed during paging"):
        asyncio.run(load_plex_library_inventory(changed, {"plex_retries": 1}))

    incomplete = PagedSection(
        [inventory_item(index) for index in range(3)], totals=[4, 4]
    )
    with pytest.raises(RuntimeError, match="incomplete"):
        asyncio.run(load_plex_library_inventory(incomplete, {"plex_retries": 1}))


def test_plex_inventory_supports_simple_test_or_legacy_sections():
    class SimpleSection:
        title = "Movies"

        def all(self):
            return [inventory_item(1)]

    items = asyncio.run(
        load_plex_library_inventory(SimpleSection(), {"plex_retries": 1})
    )
    assert [item.ratingKey for item in items] == ["1"]


def test_processing_inventory_must_match_discovery_rating_keys():
    records = [{"rating_key": "1"}, {"rating_key": "2"}]
    inventory = [SimpleNamespace(ratingKey="1"), SimpleNamespace(ratingKey="2")]
    metafusion.validate_inventory_snapshot("Movies", inventory, records)

    with pytest.raises(RuntimeError, match="changed rating keys"):
        metafusion.validate_inventory_snapshot(
            "Movies",
            [SimpleNamespace(ratingKey="1"), SimpleNamespace(ratingKey="3")],
            records,
        )
    with pytest.raises(RuntimeError, match="changed between discovery"):
        metafusion.validate_inventory_snapshot("Movies", inventory[:1], records)


def mapping_meta(inventory):
    return {
        "library_name": "Shows",
        "library_type": "tv",
        "title": "Example Show",
        "year": 2024,
        "ratingKey": "10",
        "tmdb_id": "100",
        "imdb_id": None,
        "tvdb_id": None,
        "seasons_episodes": inventory,
        "episode_ordering": "tmdb_aired",
    }


def test_mapping_diagnosis_reports_aligned_group_and_unique_offset(monkeypatch):
    config = complete_config()

    async def metadata_aligned(_item, **_kwargs):
        return mapping_meta({1: [1, 2]})

    async def standard(_config, endpoint, **_kwargs):
        assert endpoint == "tv/100/season/1"
        return {
            "episodes": [
                {"season_number": 1, "episode_number": 1},
                {"season_number": 1, "episode_number": 2},
            ]
        }

    monkeypatch.setattr(mapping_diagnostics, "get_plex_metadata", metadata_aligned)
    monkeypatch.setattr(mapping_diagnostics, "tmdb_api_request", standard)
    aligned = asyncio.run(
        mapping_diagnostics.diagnose_mapping(SimpleNamespace(), config)
    )
    assert aligned["status"] == "aligned"

    async def metadata_offset(_item, **_kwargs):
        return mapping_meta({1: [2, 3]})

    async def no_group(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mapping_diagnostics, "get_plex_metadata", metadata_offset)
    monkeypatch.setattr(mapping_diagnostics, "resolve_episode_group_mapping", no_group)
    offset = asyncio.run(
        mapping_diagnostics.diagnose_mapping(SimpleNamespace(), config)
    )
    assert offset["status"] == "unresolved"
    proposed = offset["proposed_configuration"]["tmdb"]["episode_overrides"]
    assert proposed["tmdb:100"] == {"S01E02": "S01E01", "S01E03": "S01E02"}


def test_mapping_diagnosis_handles_configured_override_group_and_failures(monkeypatch):
    config = complete_config()
    config["tmdb"]["episode_overrides"] = {
        "tmdb:100": {"S01E02": "S01E01"}
    }

    async def metadata(_item, **_kwargs):
        return mapping_meta({1: [2]})

    async def standard(_config, _endpoint, **_kwargs):
        return {"episodes": [{"season_number": 1, "episode_number": 1}]}

    monkeypatch.setattr(mapping_diagnostics, "get_plex_metadata", metadata)
    monkeypatch.setattr(mapping_diagnostics, "tmdb_api_request", standard)
    configured = asyncio.run(
        mapping_diagnostics.diagnose_mapping(SimpleNamespace(), config)
    )
    assert configured["status"] == "configured_override"

    config["tmdb"]["episode_overrides"] = {}
    config["tmdb"]["split_series_mappings"] = {
        "tmdb:100": {
            "show_policy": "preserve",
            "seasons": {1: {"tmdb_id": "200", "season_number": 1}},
        }
    }

    async def split_standard(_config, endpoint, **_kwargs):
        episode_number = 2 if endpoint.startswith("tv/200/") else 1
        return {
            "episodes": [
                {"season_number": 1, "episode_number": episode_number}
            ]
        }

    monkeypatch.setattr(mapping_diagnostics, "tmdb_api_request", split_standard)
    split = asyncio.run(
        mapping_diagnostics.diagnose_mapping(SimpleNamespace(), config)
    )
    assert split["status"] == "split_series"

    config["tmdb"]["split_series_mappings"] = {}
    monkeypatch.setattr(mapping_diagnostics, "tmdb_api_request", standard)

    async def group(*_args, **_kwargs):
        return {"group_id": "group-1"}

    monkeypatch.setattr(mapping_diagnostics, "resolve_episode_group_mapping", group)
    grouped = asyncio.run(
        mapping_diagnostics.diagnose_mapping(SimpleNamespace(), config)
    )
    assert grouped["status"] == "episode_group"

    async def movie(_item, **_kwargs):
        return {**mapping_meta({}), "library_type": "movie"}

    monkeypatch.setattr(mapping_diagnostics, "get_plex_metadata", movie)
    unsupported = asyncio.run(
        mapping_diagnostics.diagnose_mapping(SimpleNamespace(), config)
    )
    assert unsupported["status"] == "unsupported"

    async def no_identity(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mapping_diagnostics, "get_plex_metadata", metadata)
    monkeypatch.setattr(mapping_diagnostics, "resolve_tmdb_id", no_identity)
    missing = asyncio.run(
        mapping_diagnostics.diagnose_mapping(SimpleNamespace(), config)
    )
    assert missing["status"] == "missing_identity"


def test_mapping_report_and_runner_are_read_only_and_report_missing_keys(
    monkeypatch, tmp_path
):
    item = SimpleNamespace(ratingKey="10")

    async def inventory(*_args, **_kwargs):
        return [item]

    async def diagnose(_item, _config, session=None):
        assert session == "session"
        return {
            "library": "Shows",
            "rating_key": "10",
            "title": "Example Show",
            "year": 2024,
            "tmdb_id": "100",
            "status": "unresolved",
            "explanation": "Review mapping.",
            "missing_standard": ["S01E02"],
            "proposed_configuration": {
                "tmdb": {
                    "episode_overrides": {"tmdb:100": {"S01E02": "S01E01"}}
                }
            },
        }

    monkeypatch.setattr(mapping_diagnostics, "load_plex_library_inventory", inventory)
    monkeypatch.setattr(mapping_diagnostics, "diagnose_mapping", diagnose)
    records, report = asyncio.run(
        mapping_diagnostics.run_mapping_diagnosis(
            [SimpleNamespace(title="Shows")],
            complete_config(),
            ["10", "missing"],
            session="session",
            base_dir=tmp_path,
        )
    )
    assert [record["status"] for record in records] == ["unresolved", "not_found"]
    contents = report.read_text(encoding="utf-8")
    assert "No mapping or metadata was changed" in contents
    assert "S01E02: S01E01" in contents
    assert "rating key: missing" in contents


def test_mapping_connector_command_uses_one_bounded_session(monkeypatch):
    config = complete_config()
    plex = SimpleNamespace(machineIdentifier="server")
    section = SimpleNamespace(title="Shows")
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
        return ([{"status": "aligned"}], "/config/reports/mapping.txt")

    monkeypatch.setattr(metafusion.aiohttp, "ClientSession", lambda **_kwargs: Session())
    monkeypatch.setattr(metafusion.aiohttp, "TCPConnector", lambda **_kwargs: object())
    monkeypatch.setattr(metafusion, "preflight_connectors", preflight)
    monkeypatch.setattr(
        metafusion,
        "connect_plex_library",
        lambda _config, plex=None: ([section], ["Shows"], []),
    )
    monkeypatch.setattr(metafusion, "run_mapping_diagnosis", run)

    result = asyncio.run(metafusion.mapping_diagnosis_connectors(config, ["10"]))
    assert result[0][0]["status"] == "aligned"
    assert calls == ["enter", "exit"]


def test_mapping_cli_executes_and_reports_connector_failures(monkeypatch, tmp_path, capsys):
    config = complete_config()
    report = tmp_path / "mapping.txt"

    monkeypatch.setattr(
        metafusion,
        "load_config_file",
        lambda **_kwargs: (copy.deepcopy(config), {}),
    )
    monkeypatch.setattr(metafusion, "validate_config", lambda _config: [])
    monkeypatch.setattr(metafusion, "validate_preflight_paths", lambda *_args: None)
    monkeypatch.setattr(metafusion, "begin_tmdb_cache", lambda _config: None)
    monkeypatch.setattr(metafusion.tmdb_response_cache, "reset_memory", lambda: None)

    async def success(_config, _keys):
        return ([{"status": "aligned"}], report)

    monkeypatch.setattr(metafusion, "mapping_diagnosis_connectors", success)
    assert metafusion.main(["--mapping-diagnose", "--rating-key", "10"]) == 0
    output = capsys.readouterr().out
    assert "0 require review" in output
    assert str(report) in output

    async def failure(_config, _keys):
        raise RuntimeError("connector unavailable")

    monkeypatch.setattr(metafusion, "mapping_diagnosis_connectors", failure)
    assert metafusion.main(["--mapping-diagnose", "--rating-key", "10"]) == 1
    assert "connector unavailable" in capsys.readouterr().err


def test_artwork_audit_explains_selected_components_and_rejected_candidates(tmp_path):
    config = complete_config()
    config["poster_set"].update(
        {
            "prefer_vote": 5,
            "vote_relaxed": 3,
            "max_width": 1000,
            "max_height": 1500,
            "min_width": 500,
            "min_height": 750,
        }
    )
    candidates = [
        {
            "file_path": "/selected.jpg",
            "iso_639_1": "en",
            "width": 1000,
            "height": 1500,
            "vote_average": 8,
        },
        {
            "file_path": "/fallback.jpg",
            "iso_639_1": "zh",
            "width": 1200,
            "height": 1800,
            "vote_average": 9,
        },
        {
            "file_path": "/small.jpg",
            "iso_639_1": "en",
            "width": 300,
            "height": 450,
            "vote_average": 2,
        },
    ]
    selected = candidates[0]
    rejected = artwork_candidate_explanations(
        config,
        candidates,
        selected,
        asset_type="poster",
        preferred_language="en",
    )
    assert len(rejected) == 2
    assert "lower language priority" in rejected[0]["reasons"]
    assert "below minimum dimensions" in rejected[1]["reasons"]

    summary = builder._candidate_summary(
        config,
        selected,
        "poster",
        candidate_pool=candidates,
    )
    assert summary["quality_score"] > 0
    assert len(summary["rejected_candidates"]) == 2

    quality = {
        "score": 93.0,
        "resolution": 45.0,
        "vote": 28.0,
        "aspect": 10.0,
        "language": 10.0,
    }
    report = write_asset_audit_report(
        [
            {
                "library": "Movies",
                "media_type": "Movie",
                "title": "Example",
                "asset_type": "poster",
                "action": "managed",
                "ownership": "managed",
                "candidate": {
                    "width": 1000,
                    "height": 1500,
                    "language": "en",
                    "vote": 8,
                    "quality_score": 93,
                    "quality_components": quality,
                    "rejected_candidates": rejected,
                },
            }
        ],
        base_dir=tmp_path,
    )
    contents = report.read_text(encoding="utf-8")
    assert "selected components:" in contents
    assert "highest-scoring rejected candidates:" in contents
    assert "lower language priority" in contents


def test_mapping_cli_requires_rating_key_and_sets_read_only_execution(capsys):
    assert metafusion.main(["--mapping-diagnose"]) == 2
    assert "requires --rating-key" in capsys.readouterr().err

    config = complete_config()
    args = metafusion.parse_cli_args(
        ["--mapping-diagnose", "--rating-key", "10,11"]
    )
    metafusion.override_config_with_cli(config, args)
    assert config["settings"]["dry_run"] is True
    assert config["cleanup"]["run_cleanup"] is False
    assert config["_execution"]["mapping_diagnose"] is True
    assert config["_execution"]["rating_keys"] == ["10", "11"]
