import asyncio
import hashlib
from types import SimpleNamespace as Obj
from xml.etree import ElementTree

from helper import episode_identity as ep
from helper import findings, plex
from helper.asset_registry import AssetDestinationRegistry
from helper.incremental import child_inventory_fingerprint
from helper.kometa_application_verification import audio_language_warnings
from helper.movie_rebinding import movie_rebind_context


def test_anthology_uses_episode_identity_and_refuses_ambiguous_sources():
    scores = {(4, 1): {"tmdb_id": "401"}, (4, 2): {"title": "Deux", "date": "2026-01-02"}}
    meta = {
        "plex_tmdb_ids": ["1", "2", "3"],
        "episode_evidence": scores,
        "seasons_episodes": {1: [1], 4: [1, 2]},
    }
    mapping = {"seasons": {1: {"tmdb_id": "1", "season_number": 1}}}
    data = {
        "2": {
            "episodes": [
                {"episode_number": 1, "id": 401},
                {"episode_number": 2, "name": "Deux", "air_date": "2026-01-02"},
            ]
        },
        "3": {"episodes": [{"episode_number": 1, "id": 501}]},
    }

    async def fetch(identifier):
        return data[identifier]

    def resolve():
        return asyncio.run(ep.anthology_sources(meta, mapping, fetch))

    assert ep.episode_target(resolve(), {}, "1", 4, 2) == ("2", 1, 2)
    assert ep.episode_target(resolve(), {(1, 1): (5, 6)}, "1", 1, 1) == ("1", 5, 6)
    assert ep.episode_target(resolve(), {}, "1", 0, 2) is None
    data["3"] = data["2"]
    assert 4 not in resolve()["seasons"]
    data["3"] = {}
    assert 4 not in resolve()["seasons"]
    assert ep.episode_evidence({}, None) == "missing"
    assert (
        ep.episode_evidence({"title": "localized", "date": "2026-01-02"}, data["2"]["episodes"][1])
        == "date"
    )
    assert (
        ep.episode_evidence({"title": "other", "date": "2026-01-03"}, data["2"]["episodes"][1])
        == "conflict"
    )
    assert ep.episode_evidence({"title": "localized"}, data["2"]["episodes"][1]) == "number"


def test_plex_order_is_inherited_and_tracked():
    show = Obj(
        type="show",
        leafCount=1,
        _data=ElementTree.fromstring(
            '<Video><Preferences><Setting id="showOrdering" value="tvdbAiring"/></Preferences></Video>'
        ),
    )
    first = child_inventory_fingerprint(show)
    assert plex.show_ordering(show) == "tvdbAiring"
    show._data.find(".//Setting").set("value", "")
    assert plex.show_ordering(show) is None and child_inventory_fingerprint(show) != first
    assert (
        asyncio.run(
            plex.read_library_order(
                Obj(settings=lambda: [Obj(id="showOrdering", value="tmdbAiring")])
            )
        )
        == "tmdbAiring"
    )


def test_verified_movie_rebinding_moves_claim_without_touching_file(tmp_path):
    old_key, new_key = "movie:plex:76076", "movie:plex:76087"
    scope = {"server_id": "plex", "library_uuid": "movies", "library_name": "Movies"}
    record = {
        "rating_key": "76087",
        "tmdb_id": "99",
        "media_type": "movie",
        "movie_dir": "/media/Street Fighter",
    }
    config = {"settings": {"mode": "kometa", "path": str(tmp_path)}}
    context = movie_rebind_context(config, scope, {"Movies": [record]})
    destination = tmp_path / "assets/movie/Street Fighter/poster.jpg"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"manual")
    digest = hashlib.sha256(b"original").hexdigest()
    old = {
        **scope,
        "rating_key": "76076",
        "media_type": "movie",
        "tmdb_id": "99",
        "poster_path": str(destination),
        "poster_checksum": digest,
    }
    store = {old_key: old}
    registry = AssetDestinationRegistry(
        [
            {
                "cache_key": old_key,
                "media_type": "movie",
                "tmdb_id": "99",
                "asset_type": "poster",
                "destination": str(destination),
                "checksum": digest,
            }
        ]
    )

    def rebind():
        return registry.rebind_departed_movie(context, store, destination, new_key, "poster", "99")

    assert not registry.rebind_departed_movie({}, store, destination, new_key, "poster", "99")
    assert not rebind()
    destination.write_bytes(b"original")
    context["destinations"][str(destination)].append(record)
    assert not rebind()
    context["destinations"][str(destination)].pop()
    store[new_key] = {"server_id": "different"}
    assert not rebind()
    del store[new_key]
    registry.reconcile_departed_movies(context, store)
    assert not rebind()
    assert "poster_path" not in store[old_key] and store[new_key]["poster_checksum"] == digest
    assert destination.read_bytes() == b"original"


def test_forgotten_owner_recovery_requires_fresh_inventory(tmp_path):
    destination = str(tmp_path / "poster.jpg")
    gap = {
        "category": "ownership_collision",
        "server_id": "plex",
        "library_uuid": "movies",
        "library": "Movies",
        "rating_key": "76087",
        "media_type": "movie",
        "tmdb_id": "99",
        "asset_type": "poster",
        "destination": destination,
        "conflicting_owner": "movie:plex:76076",
    }

    def publish(config):
        return findings.publish_findings(config, True, path=tmp_path / "db", base_dir=tmp_path)

    assert publish({"_artwork_gaps": [gap]})["new"] == 1
    scope = ("plex", "movies")
    config = {
        "_findings_inventory": {scope: {"76087", "76076"}},
        "_verified_movie_destinations": {scope: {destination: [gap]}},
    }
    assert publish(config)["resolved"] == 0
    config["_findings_inventory"][scope].remove("76076")
    assert publish(config)["resolved"] == 1


def test_language_warning_recognizes_only_explicit_preference_errors(tmp_path):
    config = {"settings": {"path": str(tmp_path)}}
    assert audio_language_warnings(config) == {}
    log = tmp_path / "logs/meta.log"
    log.parent.mkdir()
    log.write_text(
        "audio_language zh Not Found rating key: 74765\nother Not Found rating key: 12345"
    )
    assert audio_language_warnings(config) == {"74765": "zh"}


def test_plex_inventory_requests_preferences(monkeypatch):
    import metafusion
    from helper.config import config_for_library

    class Section:
        title = "TV"
        type = "show"
        key = 4
        totalSize = 1

        def settings(self):
            return [Obj(id="showOrdering", value="tmdbAiring")]

        def all(self, **kwargs):
            raise AssertionError("use the paged preference request")

        def fetchItems(self, url, **kwargs):
            assert "includePreferences=1" in url and kwargs["maxresults"] == 1
            return [] if kwargs["container_start"] else [Obj(ratingKey=1)]

    section = Section()
    monkeypatch.setattr(plex, "ShowSection", Section)
    assert asyncio.run(plex.load_plex_library_inventory(section))[0].ratingKey == 1
    orders = asyncio.run(metafusion.library_orders([section]))
    assert (
        config_for_library({"_library_orders": orders}, "TV")["plex"]["_library_order"]
        == "tmdbAiring"
    )
    assert (
        plex.show_ordering(Obj(_data=ElementTree.fromstring('<Video showOrdering="tvdbDvd"/>')))
        == "tvdbDvd"
    )


def test_verified_group_requires_corroboration_for_tmdb_exception():
    group = {"episodes": {(1, 1): {"id": 1}, (1, 2): {"id": 8}}}
    meta = {"episode_ordering": "tmdbAiring", "episode_evidence": {}}
    assert ep.verified_group(meta, group, {(1, 2)}) is None
    meta["episode_evidence"][(1, 2)] = {"tmdb_id": "2"}
    assert ep.verified_group(meta, group, {(1, 2)}) is None
    meta["episode_evidence"][(1, 2)] = {"tmdb_id": "8"}
    assert ep.verified_group(meta, group, {(1, 2)})["episodes"] == {(1, 2): {"id": 8}}


def test_anthology_output_and_diagnosis_share_episode_identity(monkeypatch, tmp_path):
    from test_phase26_builder_matrix import _config, _flags, _patch_identity, _show_meta

    from helper import mapping_diagnostics as diag
    from modules import builder

    _patch_identity(monkeypatch)
    meta = _show_meta(
        plex_tmdb_ids=["200", "201", "202"],
        seasons_episodes={1: [1], 4: [1], 5: [1]},
        plex_seasons=[1, 4, 5],
        episode_evidence={(4, 1): {"tmdb_id": "402"}},
    )

    async def get_meta(*args, **kwargs):
        return meta

    async def fetch(config, endpoint, **kwargs):
        identifier = endpoint.split("/")[1]
        return {
            "name": "Installment",
            "episodes": [
                {
                    "name": "Round " + identifier,
                    "episode_number": 1,
                    "id": {"200": 10, "201": 402, "202": 11}[identifier],
                }
            ],
        }

    monkeypatch.setattr(diag, "get_plex_metadata", get_meta)
    monkeypatch.setattr(diag, "tmdb_api_request", fetch)
    monkeypatch.setattr(builder, "tmdb_api_request", fetch)
    for mode in ["kometa", "plex"]:
        config = _config(tmp_path, mode=mode)
        config["tmdb"]["split_series_mappings"] = {
            "tmdb:200": {"seasons": {1: {"tmdb_id": "200", "season_number": 1}}}
        }
        result = asyncio.run(
            builder._build_tv(
                config,
                {"metadata": {}},
                feature_flags=_flags(
                    metadata_basic=True, poster=False, background=False, season=False, dry_run=True
                ),
                meta=meta,
                session=object(),
            )
        )
        assert result["seasons"][4]["episodes"][1]["title"] == "Round 201"
        assert 5 not in result["seasons"]
        diagnosis = asyncio.run(diag.diagnose_mapping(Obj(), config))
        assert diagnosis["missing_standard"] == ["S05E01"]
        report = diag.write_mapping_diagnosis_report([diagnosis], base_dir=tmp_path)
        assert "S04E01 -> TMDb ('201', 1, 1) (id)" in report.read_text()


def test_language_warning_visible_in_text_report(tmp_path):
    from helper.kometa_application_verification import write_kometa_application_report

    path = write_kometa_application_report(
        [{"rating_key": "11", "kometa_audio_language_warning": "zh"}], [], base_dir=tmp_path
    )
    assert "Plex item exists" in path.read_text()


def test_scheduled_order_also_applies_to_preloaded_items(monkeypatch):
    meta = {"library_type": "tv", "episode_ordering": None}
    monkeypatch.setitem(plex._plex_cache, "22", meta)
    item = Obj(type="show", ratingKey="22")
    result = asyncio.run(
        plex.get_plex_metadata(item, _plex_config={"_library_order": "tmdbAiring"})
    )
    assert result["episode_ordering"] == "tmdbAiring"


def test_plex_context_and_duplicate_movies(tmp_path):
    cfg = {"settings": {"mode": "plex"}}
    movie = {"media_type": "movie", "rating_key": "1", "movie_dir": str(tmp_path), "tmdb_id": "10"}
    duplicate = {**movie, "rating_key": "2"}
    scope = {"library_name": "A"}
    context = movie_rebind_context(cfg, scope, {"A": [movie], "B": [duplicate]})
    assert len(context["destinations"][str(tmp_path / "poster.jpg")]) == 2
    registry = AssetDestinationRegistry()
    registry.reconcile_departed_movies(context, {})
    assert not movie_rebind_context(cfg, scope, {"A": [{**movie, "movie_dir": None}]})[
        "destinations"
    ]


def test_group_diagnosis_displays_verified_source(monkeypatch, tmp_path):
    from helper import mapping_diagnostics as diag

    async def meta(*args, **kwargs):
        return {"library_type": "tv", "tmdb_id": "24", "seasons_episodes": {3: [5]}}

    async def empty(*args, **kwargs):
        return {}

    async def group(*args, **kwargs):
        return {"group_id": "g", "episodes": {(3, 5): {"season_number": 1, "episode_number": 7}}}

    monkeypatch.setattr(diag, "get_plex_metadata", meta)
    monkeypatch.setattr(diag, "tmdb_api_request", empty)
    monkeypatch.setattr(diag, "resolve_episode_group_mapping", group)
    result = asyncio.run(diag.diagnose_mapping(Obj(), {}))
    assert result["episode_sources"] == [
        {"plex": "S03E05", "tmdb": ("24", 1, 7), "evidence": "episode_group"}
    ]


def test_claim_presence_alone_does_not_close_collision(tmp_path):
    shared = {"library": "Movies", "rating_key": "1", "asset_type": "poster", "media_type": "movie"}
    for config in [
        {"_artwork_gaps": [{**shared, "category": "ownership_collision"}]},
        {"_adoption_audit_records": [{**shared, "status": "filesystem_verified"}]},
    ]:
        result = findings.publish_findings(config, True, path=tmp_path / "db", base_dir=tmp_path)
    assert result["resolved"] == 0
