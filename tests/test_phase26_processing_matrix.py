import asyncio
from types import SimpleNamespace

import pytest

from modules import processing


class _Section:
    title = "TV Shows"
    type = "show"

    def __init__(self, items):
        self._items = items

    def all(self):
        return list(self._items)


def test_processing_summary_accounts_for_every_action(monkeypatch, tmp_path):
    actions = [
        "downloaded",
        "upgraded",
        "adopted",
        "skipped",
        "not_due",
        "preserved",
        "policy_preserved",
        "policy_missing",
        "missing",
        "failed",
        "deferred",
    ]
    items = [
        SimpleNamespace(
            ratingKey=str(index),
            title=f"Show {index}",
            year=2020,
            type="show",
            updatedAt="now",
            childCount=1,
            seasonCount=1,
            leafCount=1,
        )
        for index in range(len(actions))
    ]

    async def metadata(item, **_kwargs):
        return {
            "title": item.title,
            "year": item.year,
            "library_name": "TV Shows",
            "library_type": "show",
            "ratingKey": item.ratingKey,
            "updatedAt": item.updatedAt,
            "tmdb_id": str(100 + int(item.ratingKey)),
            "show_path": item.title,
            "seasons_episodes": {1: [1]},
        }

    async def process_item(plex_item, **_kwargs):
        index = int(plex_item.ratingKey)
        action = actions[index]
        stats = {
            "metadata_action": ["downloaded", "upgraded", "skipped", "failed"][
                index % 4
            ],
            "poster_action": action,
            "background_action": actions[-index - 1],
            "season_poster_actions": {index: action},
            "artwork_providers": {
                "poster": "TMDb" if index % 2 else None,
                "background": "Fanart.tv",
            },
            "season_artwork_providers": (
                {index: "Plex"} if index % 2 else {str(index): "TMDb"}
            ),
            "artwork_selection_stages": {
                "poster": (
                    "missing_only_relaxed"
                    if index == 0
                    else "missing_only_download_failover"
                ),
                "background": (
                    "missing_only_relaxed"
                    if index == 1
                    else "missing_only_download_failover"
                ),
            },
            "season_artwork_selection_stages": (
                {index: "missing_only_relaxed"}
                if index % 2
                else {str(index): "missing_only_download_failover"}
            ),
            "artwork_file_counts": {"expected": 3, "present": 2, "absent": 1},
            "artwork_current_providers": {"tmdb": 1, "existing": 1},
            "poster": {"size": 10},
            "background": {"size": 20},
            "seasons": {1: {"episodes": {1: {}}}},
            "is_complete": index % 2 == 0,
            "plex_metadata_writes": index,
            "_incremental_success": True,
        }
        if index == 0:
            stats["season_posters"] = {0: 30, 1: 40}
        else:
            stats["season_poster"] = {"size": 30}
        return stats

    monkeypatch.setattr(processing, "get_plex_metadata", metadata)
    monkeypatch.setattr(processing, "process_item", process_item)
    monkeypatch.setattr(processing, "load_item_exceptions", lambda *_args: [])
    monkeypatch.setattr(processing, "load_identity_overrides", lambda *_args: [])
    summaries = {}
    counts = {}
    sizes = {}
    flags = {
        "dry_run": True,
        "metadata_basic": True,
        "metadata_enhanced": True,
        "plex_metadata": False,
        "poster": True,
        "season": True,
        "background": True,
        "cleanup": False,
    }
    result = asyncio.run(
        processing.process_library(
            _Section(items),
            {
                "settings": {"mode": "plex", "path": str(tmp_path)},
                "plex": {},
                "runtime": {"max_concurrency": 3},
                "safety": {"allow_ambiguous_editions": False},
            },
            feature_flags=flags,
            library_item_counts=counts,
            library_filesize=sizes,
            metadata_summaries=summaries,
        )
    )

    assert len(result) == len(actions)
    assert counts == {"TV Shows": len(actions)}
    summary = summaries["TV Shows"]
    assert summary["complete"] == 6
    assert summary["incomplete"] == 5
    assert summary["season_count"] == len(actions)
    assert summary["episode_count"] == len(actions)
    library = summary["library_summary"]
    for prefix in ("poster", "background"):
        for action in actions:
            assert library[f"{prefix}_{action}"] == 1
    for action in actions:
        if action not in {"policy_preserved", "policy_missing"}:
            assert library[f"season_poster_{action}"] == 1
    assert library["artwork_deferred"] == 3
    assert library["artwork_automatic_relaxed"] >= 1
    assert library["artwork_download_failover"] >= 1
    assert library["artwork_file_expected"] == len(actions) * 3
    assert library["artwork_file_present"] == len(actions) * 2
    assert library["artwork_file_absent"] == len(actions)
    assert library["artwork_current_providers"] == {
        "existing": len(actions),
        "tmdb": len(actions),
    }
    assert library["poster_bytes"] == 110
    assert library["background_bytes"] == 220
    assert library["season_poster_bytes"] == 370


def test_artwork_provider_reconciliation_helpers_cover_all_source_paths(tmp_path):
    poster = tmp_path / "poster.jpg"
    poster.write_bytes(b"poster")
    other = tmp_path / "other.jpg"
    cached = {
        "poster_path": str(poster),
        "poster_checksum": "checksum",
        "poster_provider": "TMDb",
        "seasons": {
            "1": {
                "season_path": str(poster),
                "season_checksum": "checksum",
                "season_provider": "Plex",
            }
        },
    }

    assert processing._normalized_artwork_provider(" FanArt ") == "fanart"
    assert processing._normalized_artwork_provider("manual") == "unknown"
    assert processing._recorded_artwork_provider(None, "poster", poster) == "existing"
    assert (
        processing._recorded_artwork_provider(
            {"seasons": []}, "season", poster, season_number=1
        )
        == "existing"
    )
    assert (
        processing._recorded_artwork_provider(
            cached, "season", poster, season_number=1
        )
        == "plex"
    )
    assert (
        processing._recorded_artwork_provider(
            {"poster_path": str(poster)}, "poster", poster
        )
        == "existing"
    )
    assert (
        processing._recorded_artwork_provider(
            {"poster_path": object(), "poster_checksum": "checksum"},
            "poster",
            poster,
        )
        == "existing"
    )
    assert processing._recorded_artwork_provider(cached, "poster", other) == "existing"
    unknown = dict(cached, poster_provider="manual")
    assert processing._recorded_artwork_provider(unknown, "poster", poster) == "unknown"

    assert (
        processing._current_artwork_provider(
            {"artwork_providers": {"poster": "FanArt"}},
            cached,
            "poster",
            "downloaded",
            poster,
        )
        == "fanart"
    )
    assert (
        processing._current_artwork_provider(
            {"season_artwork_providers": {1: "Plex"}},
            cached,
            "season",
            "upgraded",
            poster,
            season_number=1,
        )
        == "plex"
    )
    assert (
        processing._current_artwork_provider(
            {"season_artwork_providers": {"1": "TMDb"}},
            cached,
            "season",
            "adopted",
            poster,
            season_number=1,
        )
        == "tmdb"
    )
    assert (
        processing._current_artwork_provider(
            {}, cached, "poster", "preserved", poster
        )
        == "existing"
    )
    assert (
        processing._current_artwork_provider(
            {}, cached, "poster", "policy_preserved", poster
        )
        == "existing"
    )
    assert (
        processing._current_artwork_provider(
            {"artwork_ownership": {"poster": "manual"}},
            cached,
            "poster",
            "skipped",
            poster,
        )
        == "existing"
    )
    assert (
        processing._current_artwork_provider(
            {"artwork_ownership": {"poster": "overwrite"}},
            cached,
            "poster",
            "skipped",
            poster,
        )
        == "unknown"
    )
    assert (
        processing._current_artwork_provider(
            {"artwork_ownership": {"poster": "managed"}},
            cached,
            "poster",
            "skipped",
            poster,
        )
        == "tmdb"
    )
    assert (
        processing._current_artwork_provider(
            {"season_artwork_ownership": {1: "managed"}},
            cached,
            "season",
            "skipped",
            poster,
            season_number=1,
        )
        == "plex"
    )
    assert (
        processing._current_artwork_provider(
            {"season_artwork_ownership": {"1": "shared"}},
            cached,
            "season",
            "skipped",
            poster,
            season_number=1,
        )
        == "plex"
    )


def test_processing_uses_storage_inventory_and_clears_retries(monkeypatch, tmp_path):
    item = SimpleNamespace(
        ratingKey="1",
        title="Movie",
        year=2020,
        type="movie",
        updatedAt="now",
    )
    section = _Section([item])
    section.title = "Movies"
    section.type = "movie"
    section.uuid = "uuid"
    section._server = SimpleNamespace(machineIdentifier="server")
    asset = tmp_path / "poster.jpg"
    asset.write_bytes(b"poster")

    async def metadata(_item, **_kwargs):
        return {
            "title": "Movie",
            "year": 2020,
            "library_name": "Movies",
            "library_type": "movie",
            "ratingKey": "1",
            "updatedAt": "now",
            "tmdb_id": "100",
            "movie_path": "Movie",
        }

    async def process_item(**_kwargs):
        return {
            "metadata_action": "skipped",
            "poster_action": "downloaded",
            "background_action": "skipped",
            "season_poster_actions": {},
            "storage_files": [
                {"path": str(asset), "asset_type": "poster", "bytes": 123}
            ],
            "_incremental_success": True,
        }

    async def to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    cleared = []
    monkeypatch.setattr(processing, "get_plex_metadata", metadata)
    monkeypatch.setattr(processing, "process_item", process_item)
    monkeypatch.setattr(processing, "load_due_item_retries", lambda *_args: {})
    monkeypatch.setattr(processing, "mark_items_started", lambda *_args: None)
    monkeypatch.setattr(
        processing, "clear_item_retries", lambda *args: cleared.append(args)
    )
    monkeypatch.setattr(processing.asyncio, "to_thread", to_thread)
    sizes = {}
    summaries = {}
    result = asyncio.run(
        processing.process_library(
            section,
            {
                "settings": {"mode": "plex", "path": str(tmp_path)},
                "plex": {},
                "runtime": {"max_concurrency": 1},
                "safety": {},
            },
            feature_flags={
                "dry_run": False,
                "metadata_basic": False,
                "metadata_enhanced": False,
                "plex_metadata": False,
                "poster": False,
                "season": False,
                "background": False,
                "cleanup": False,
            },
            library_filesize=sizes,
            metadata_summaries=summaries,
            incremental_fingerprint="fingerprint",
        )
    )
    assert result[0]["storage_files"][0]["bytes"] == 123
    assert sizes == {"Movies": 123}
    assert cleared and cleared[0][-1] == {"1"}
    assert summaries["Movies"]["library_summary"]["storage_scope"] == "full inventory"


def test_item_artwork_reconciliation_is_independent_of_action_counters(
    monkeypatch, tmp_path
):
    item = SimpleNamespace(
        ratingKey="1",
        title="Movie",
        year=2020,
        type="movie",
        updatedAt="now",
    )
    meta = {
        "title": "Movie",
        "year": 2020,
        "library_name": "Movies",
        "library_type": "movie",
        "ratingKey": "1",
        "tmdb_id": "100",
        "movie_path": "Movie (2020)",
    }
    poster = tmp_path / "poster.jpg"
    poster.write_bytes(b"poster")
    background = tmp_path / "background.jpg"

    async def metadata(*_args, **_kwargs):
        return dict(meta)

    async def build(*_args, **_kwargs):
        return {
            "metadata_action": "not_due",
            "poster_action": "skipped",
            "background_action": "skipped",
            "season_poster_actions": {},
            # A newly considered candidate must not become the installed source.
            "artwork_providers": {"poster": "fanart"},
        }

    def asset_path(_config, _meta, *, asset_type, **_kwargs):
        return poster if asset_type == "poster" else background

    events = []
    monkeypatch.setattr(processing, "get_plex_metadata", metadata)
    monkeypatch.setattr(processing, "build_movie", build)
    monkeypatch.setattr(processing, "get_asset_path", asset_path)
    monkeypatch.setattr(
        processing,
        "load_cache",
        lambda: {
            processing.cache_key_for_meta(meta): {
                "poster_path": str(poster.resolve()),
                "poster_checksum": "known-checksum",
                "poster_provider": "tmdb",
            }
        },
    )
    monkeypatch.setattr(processing, "log_item_outcomes", lambda *_a, **_k: None)
    monkeypatch.setattr(
        processing,
        "log_processing_event",
        lambda event, **kwargs: events.append((event, kwargs)),
    )

    stats = asyncio.run(
        processing.process_item(
            item,
            {},
            {"plex": {}, "settings": {"mode": "kometa", "dry_run": False}},
            feature_flags={
                "metadata_basic": False,
                "metadata_enhanced": False,
                "plex_metadata": False,
                "poster": True,
                "background": True,
                "season": False,
                "dry_run": False,
            },
        )
    )

    assert stats["artwork_file_counts"] == {
        "expected": 2,
        "present": 1,
        "absent": 1,
    }
    assert stats["artwork_current_providers"] == {"tmdb": 1}
    assert events[-1][0] == "processing_artwork_reconciliation"
    assert events[-1][1]["absent"] == 1


def test_processing_identity_failure_and_inventory_helpers(monkeypatch, tmp_path):
    missing = tmp_path / "missing.yml"
    assert processing._output_snapshot(missing) == (False, None)
    existing = tmp_path / "metadata.yml"
    existing.write_text("metadata: {}\n", encoding="utf-8")
    assert processing._output_snapshot(existing)[0] is True
    assert processing._read_existing_metadata(existing, True) == {"metadata": {}}

    assert not processing.apply_cached_tmdb_recovery(None, {})
    meta = {"title": "Movie", "year": 2020, "library_type": "movie", "tmdb_id": "1"}
    monkeypatch.setattr(processing, "plex_identity_fingerprint", lambda _meta: "fp")
    cache = {
        processing.cache_key_for_meta(meta): {
            "tmdb_recovery_source_id": "1",
            "tmdb_id": "2",
            "tmdb_recovery_identity_fingerprint": "fp",
        }
    }
    assert processing.apply_cached_tmdb_recovery(meta, cache)
    assert meta["tmdb_id"] == "2" and meta["plex_tmdb_id"] == "1"
    assert not processing.apply_cached_tmdb_recovery(meta, {processing.cache_key_for_meta(meta): []})

    assert not processing.apply_learned_tmdb_identity({})
    learned = {
        "ratingKey": "1",
        "server_id": "server",
        "library_uuid": "uuid",
        "tmdb_id": "1",
    }
    monkeypatch.setattr(processing, "load_identity_binding", lambda *_a, **_k: {"tmdb_id": "2"})
    assert processing.apply_learned_tmdb_identity(learned)
    assert learned["identity_binding_reused"] is True
    monkeypatch.setattr(processing, "load_identity_binding", lambda *_a, **_k: None)
    assert not processing.apply_learned_tmdb_identity(learned)

    assert not processing.apply_manual_tmdb_identity({}, {})
    config = {"_identity_overrides_by_rating_key": {"1": {"media_type": "tv", "tmdb_id": "3"}}}
    assert not processing.apply_manual_tmdb_identity({"ratingKey": "1", "library_type": "movie"}, config)
    assert processing._item_exception_scopes(
        {"_item_exceptions_by_rating_key": {"1": [{"output_type": "Poster"}]}},
        {"ratingKey": "1"},
    ) == {"poster"}
    assert processing._item_exception_seasons(
        {"_item_exceptions_by_rating_key": {"1": [{"output_type": "season", "season_number": 0}]}},
        {"ratingKey": "1"},
    ) == {0}
    assert "rating key 1" in processing._item_failure_label(SimpleNamespace(title="Movie", year=2020, ratingKey="1"))

    try:
        try:
            raise ValueError("root")
        except ValueError as error:
            raise RuntimeError("wrapper") from error
    except RuntimeError as error:
        assert processing._root_error_message(error) == "root"
    failures = [
        (f"Item {index}", RuntimeError(str(index)))
        for index in range(processing.MAX_ITEM_FAILURE_DETAILS + 2)
    ]
    assert "... and 2 more" in processing.format_item_failures(failures)

    ambiguous = processing.find_ambiguous_editions(
        [
            {"library_type": "movie", "title": "Movie", "year": 2020, "edition_title": None},
            {"library_type": "movie", "title": "Movie", "year": 2020, "edition_title": None},
            {"library_type": "tv", "title": "Show", "year": 2020},
        ]
    )
    assert "duplicate editions blank" in ambiguous[0]
    errors = processing.cleanup_inventory_errors(
        [
            {"library_type": "movie", "title": "Movie"},
            {"library_type": "show", "title": "Show", "year": 2020, "ratingKey": "2", "show_path": "Show"},
        ],
        {"poster": True, "metadata_basic": True},
    )
    assert "movie_path" in errors[0] and "seasons_episodes" in errors[1]


def test_process_item_applies_exceptions_retry_class_and_storage(monkeypatch, tmp_path):
    item = SimpleNamespace(
        ratingKey="1", title="Movie", year=2020, type="movie", updatedAt="old"
    )
    meta = {
        "title": "Movie",
        "year": 2020,
        "library_name": "Movies",
        "library_type": "movie",
        "ratingKey": "1",
        "tmdb_id": "100",
        "movie_path": "Movie",
    }

    async def metadata(*_args, **_kwargs):
        return dict(meta)

    received = {}

    async def build(_config, _consolidated, **kwargs):
        received.update(kwargs["feature_flags"])
        config["_artwork_gaps"].append(
            {"category": "path_invalid", "detail": "mapping required"}
        )
        return {
            "metadata_action": "failed",
            "poster_action": "deferred",
            "background_action": "skipped",
            "season_poster_actions": {0: "failed", None: "skipped"},
            "plex_candidate": {"root": {}},
        }

    async def apply(*_args, **_kwargs):
        return {"writes": 0, "failures": 1, "deferred": 1}

    monkeypatch.setattr(processing, "get_plex_metadata", metadata)
    monkeypatch.setattr(processing, "build_movie", build)
    monkeypatch.setattr(processing, "apply_plex_metadata", apply)
    monkeypatch.setattr(processing, "log_item_outcomes", lambda *_a, **_k: None)
    config = {
        "plex": {},
        "settings": {"mode": "plex", "dry_run": False},
        "plex_metadata": {"enabled": True},
        "_artwork_gaps": [
            {"category": "path_invalid", "detail": "mapping required"}
        ],
        "_item_exceptions_by_rating_key": {
            "1": [
                {"output_type": "background"},
                {"output_type": "season", "season_number": 0},
            ]
        },
    }
    stats = asyncio.run(
        processing.process_item(
            item,
            {},
            config,
            feature_flags={
                "metadata_basic": True,
                "metadata_enhanced": True,
                "plex_metadata": True,
                "poster": True,
                "background": True,
                "season": True,
                "dry_run": False,
            },
            work_reasons={"metadata", "poster", "background", "season"},
        )
    )
    assert received["background"] is False
    assert stats["_incremental_success"] is False
    assert stats["_retry_failure_class"] == "permanent"
    assert stats["_retry_error"] == "mapping required"
    assert stats["storage_files"] == []


def test_process_item_plex_write_reload_cache_and_builder_failures(monkeypatch, tmp_path):
    class Item(SimpleNamespace):
        def reload(self):
            self.updatedAt = "new"

    item = Item(ratingKey="1", title="Show", year=2020, type="show", updatedAt="old")

    async def metadata(*_args, **_kwargs):
        return {
            "title": "Show",
            "year": 2020,
            "library_name": "Shows",
            "library_type": "show",
            "ratingKey": "1",
            "tmdb_id": "100",
            "show_path": "Show",
        }

    async def build(*_args, **_kwargs):
        return {
            "metadata_action": "skipped",
            "poster_action": "skipped",
            "background_action": "skipped",
            "season_poster_actions": {},
            "plex_candidate": {"root": {}},
        }

    async def apply(*_args, **_kwargs):
        return {"writes": 1, "failures": 0}

    async def operation(function, *_args, **_kwargs):
        return function()

    cached = []
    monkeypatch.setattr(processing, "get_plex_metadata", metadata)
    monkeypatch.setattr(processing, "build_tv", build)
    monkeypatch.setattr(processing, "apply_plex_metadata", apply)
    monkeypatch.setattr(processing, "plex_operation", operation)
    async def cache(*_args, **_kwargs):
        cached.append(True)

    monkeypatch.setattr(processing, "meta_cache_async", cache)
    monkeypatch.setattr(processing, "log_item_outcomes", lambda *_a, **_k: None)
    result = asyncio.run(
        processing.process_item(
            item,
            {},
            {"plex": {}, "runtime": {}, "settings": {"dry_run": False}},
            feature_flags={"plex_metadata": True, "dry_run": False},
        )
    )
    assert result["metadata_action"] == "upgraded" and cached

    async def no_result(*_args, **_kwargs):
        return None

    monkeypatch.setattr(processing, "build_tv", no_result)
    with pytest.raises(processing.ItemProcessingError, match="Builder returned no result"):
        asyncio.run(
            processing.process_item(
                item, {}, {"plex": {}}, feature_flags={"dry_run": True}
            )
        )

    async def unsupported_metadata(*_args, **_kwargs):
        value = await metadata()
        value["library_type"] = "music"
        return value

    monkeypatch.setattr(processing, "get_plex_metadata", unsupported_metadata)
    with pytest.raises(processing.ItemProcessingError, match="Failed to process"):
        asyncio.run(processing.process_item(item, {}, {"plex": {}}, feature_flags={}))
    assert asyncio.run(processing.process_item(None, {}, {"plex": {}}, feature_flags={})) is None


def test_processing_identity_noop_cleanup_path_and_all_output_exception(monkeypatch):
    learned = {
        "ratingKey": "1",
        "server_id": "server",
        "library_uuid": "library",
        "tmdb_id": "2",
    }
    monkeypatch.setattr(
        processing, "load_identity_binding", lambda *_args, **_kwargs: {"tmdb_id": "2"}
    )
    assert processing.apply_learned_tmdb_identity(learned) is False
    assert processing.cleanup_inventory_errors(
        [{"library_type": "tv", "title": "Show", "year": 2020, "ratingKey": "1"}],
        {"poster": True},
    )[0].endswith("show_path")

    item = SimpleNamespace(
        ratingKey="1", title="Movie", year=2020, type="movie", updatedAt="now"
    )

    async def metadata(*_args, **_kwargs):
        return {
            "ratingKey": "1",
            "title": "Movie",
            "year": 2020,
            "library_type": "movie",
            "movie_path": "Movie",
        }

    received = {}

    async def build(_config, _metadata, **kwargs):
        received.update(kwargs["feature_flags"])
        return {
            "metadata_action": "skipped",
            "poster_action": "skipped",
            "background_action": "skipped",
            "season_poster_actions": {},
        }

    monkeypatch.setattr(processing, "get_plex_metadata", metadata)
    monkeypatch.setattr(processing, "build_movie", build)
    monkeypatch.setattr(processing, "log_item_outcomes", lambda *_args, **_kwargs: None)
    result = asyncio.run(
        processing.process_item(
            item,
            {},
            {
                "settings": {"mode": "plex", "dry_run": True},
                "plex_metadata": {"enabled": True},
                "_item_exceptions_by_rating_key": {
                    "1": [{"output_type": "all"}]
                },
            },
            feature_flags={
                "metadata_basic": True,
                "metadata_enhanced": True,
                "plex_metadata": True,
                "poster": True,
                "background": True,
                "season": True,
                "dry_run": True,
            },
        )
    )
    assert result["metadata_action"] == "skipped"
    assert all(received[name] is False for name in ("poster", "background", "season"))


def test_process_item_and_library_cancellation_are_never_normalized(monkeypatch, tmp_path):
    item = SimpleNamespace(
        ratingKey="1", title="Movie", year=2020, type="movie", updatedAt="now"
    )

    async def metadata(*_args, **_kwargs):
        return {
            "ratingKey": "1",
            "title": "Movie",
            "year": 2020,
            "library_name": "Movies",
            "library_type": "movie",
            "movie_path": "Movie",
            "updatedAt": "now",
        }

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(processing, "get_plex_metadata", metadata)
    monkeypatch.setattr(processing, "build_movie", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            processing.process_item(
                item, {}, {"settings": {"dry_run": True}}, feature_flags={}
            )
        )

    section = _Section([item])
    section.title = "Movies"
    section.type = "movie"
    section.uuid = "library"
    section._server = SimpleNamespace(machineIdentifier="server")
    monkeypatch.setattr(processing, "process_item", cancelled)
    monkeypatch.setattr(processing, "load_due_item_retries", lambda *_args: {})
    monkeypatch.setattr(processing, "mark_items_started", lambda *_args: None)
    monkeypatch.setattr(processing, "record_item_failure", lambda *_args, **_kwargs: True)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            processing.process_library(
                section,
                {
                    "settings": {"mode": "plex", "dry_run": False},
                    "runtime": {"max_concurrency": 1},
                    "plex": {},
                    "safety": {},
                },
                feature_flags={
                    "dry_run": False,
                    "metadata_basic": False,
                    "metadata_enhanced": False,
                    "plex_metadata": False,
                    "poster": True,
                    "background": False,
                    "season": False,
                    "cleanup": False,
                },
            )
        )


def test_inventory_failure_is_persisted_and_blocks_cleanup(monkeypatch, tmp_path):
    item = SimpleNamespace(
        ratingKey="1", title="Movie", year=2020, type="movie", updatedAt="now"
    )
    section = _Section([item])
    section.title = "Movies"
    section.type = "movie"
    section.uuid = "library"
    section._server = SimpleNamespace(machineIdentifier="server")

    async def failed_metadata(*_args, **_kwargs):
        raise ConnectionError("Plex disconnected")

    failures = []
    monkeypatch.setattr(processing, "get_plex_metadata", failed_metadata)
    monkeypatch.setattr(
        processing,
        "record_item_failure",
        lambda *args, **kwargs: failures.append((args, kwargs)),
    )
    with pytest.raises(processing.LibraryProcessingError, match="inventory was incomplete"):
        asyncio.run(
            processing.process_library(
                section,
                {
                    "settings": {"mode": "plex", "dry_run": False},
                    "runtime": {"max_concurrency": 1},
                    "plex": {},
                    "safety": {},
                },
                feature_flags={
                    "dry_run": False,
                    "metadata_basic": False,
                    "metadata_enhanced": False,
                    "plex_metadata": False,
                    "poster": False,
                    "background": False,
                    "season": False,
                    "cleanup": True,
                },
            )
        )
    assert failures and isinstance(failures[0][0][3], ConnectionError)
