import asyncio
import logging

import pytest

from modules import processing


class FakeSection:
    title = "Movies"
    type = "movie"

    def __init__(self, items):
        self._items = items

    def all(self):
        return list(self._items)


def feature_flags():
    return {
        "dry_run": False,
        "metadata_basic": False,
        "metadata_enhanced": False,
        "poster": False,
        "season": False,
        "background": False,
    }


def config(tmp_path, mode="plex", max_concurrency=3):
    return {
        "settings": {"mode": mode, "path": str(tmp_path)},
        "runtime": {"max_concurrency": max_concurrency},
        "safety": {"allow_ambiguous_editions": False},
    }


def metadata_for(item):
    return {
        "title": f"Movie {item}",
        "year": 2020,
        "library_name": "Movies",
        "library_type": "movie",
        "ratingKey": str(item),
        "movie_path": f"Movie {item} (2020)",
    }


def test_process_library_bounds_item_concurrency(monkeypatch, tmp_path):
    active = 0
    maximum = 0

    async def fake_metadata(item, **_kwargs):
        return metadata_for(item)

    async def fake_process_item(**_kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {}

    monkeypatch.setattr(processing, "get_plex_metadata", fake_metadata)
    monkeypatch.setattr(processing, "process_item", fake_process_item)

    result = asyncio.run(
        processing.process_library(
            FakeSection(range(12)),
            config(tmp_path, max_concurrency=3),
            feature_flags=feature_flags(),
        )
    )

    assert result == []
    assert maximum == 3
    assert not (tmp_path / "metadata").exists()


def test_plex_metadata_progress_logs_start_and_completion(
    monkeypatch, tmp_path, caplog
):
    async def fake_metadata(item, **_kwargs):
        return metadata_for(item)

    async def fake_process_item(**_kwargs):
        return {
            "metadata_action": "skipped",
            "plex_metadata_writes": 0,
            "is_complete": True,
        }

    monkeypatch.setattr(processing, "get_plex_metadata", fake_metadata)
    monkeypatch.setattr(processing, "process_item", fake_process_item)
    flags = feature_flags()
    flags.update(
        {
            "metadata_basic": True,
            "metadata_enhanced": True,
            "plex_metadata": True,
        }
    )

    with caplog.at_level(logging.INFO):
        asyncio.run(
            processing.process_library(
                FakeSection([1, 2, 3]),
                config(tmp_path),
                feature_flags=flags,
            )
        )

    assert "[Plex Metadata] Movies: 0/3 checked (0.0%)" in caplog.text
    assert "[Plex Metadata] Movies: 3/3 checked (100.0%)" in caplog.text
    assert "items changed: 0, API batches: 0, unchanged: 3, failed: 0" in caplog.text


def test_process_library_propagates_item_failures(monkeypatch, tmp_path):
    class FailedItem:
        title = "Broken Movie"
        year = 2020
        ratingKey = "99"
        type = "movie"

    async def fake_metadata(_item, **_kwargs):
        return {
            **metadata_for(99),
            "title": "Broken Movie",
            "ratingKey": "99",
        }

    async def fake_process_item(**_kwargs):
        raise RuntimeError("builder failed")

    monkeypatch.setattr(processing, "get_plex_metadata", fake_metadata)
    monkeypatch.setattr(processing, "process_item", fake_process_item)

    with pytest.raises(processing.LibraryProcessingError, match="items failed") as caught:
        asyncio.run(
            processing.process_library(
                FakeSection([FailedItem()]),
                config(tmp_path),
                feature_flags=feature_flags(),
            )
        )

    assert "builder failed" in str(caught.value)
    assert "Broken Movie (2020) [rating key 99]" in str(caught.value)
    assert "successful item output was preserved" in str(caught.value)


def test_process_library_preserves_successful_yaml_when_another_item_crashes(
    monkeypatch, tmp_path
):
    async def fake_metadata(item, **_kwargs):
        return metadata_for(item)

    async def fake_process_item(plex_item, consolidated_metadata, **_kwargs):
        consolidated_metadata["metadata"][f"Movie {plex_item} (2020)"] = {
            "summary": "generated"
        }
        if plex_item == 2:
            raise RuntimeError("builder failed after a partial edit")
        return {"_incremental_success": True}

    monkeypatch.setattr(processing, "get_plex_metadata", fake_metadata)
    monkeypatch.setattr(processing, "process_item", fake_process_item)

    with pytest.raises(
        processing.LibraryProcessingError, match="successful item output was preserved"
    ):
        asyncio.run(
            processing.process_library(
                FakeSection([1, 2]),
                config(tmp_path, mode="kometa"),
                feature_flags=feature_flags(),
                incremental_fingerprint="fingerprint",
            )
        )

    output = tmp_path / "metadata" / "movie_metadata.yml"
    document = processing.yaml.safe_load(output.read_text(encoding="utf-8"))
    assert document["metadata"] == {
        "Movie 1 (2020)": {"summary": "generated"}
    }


def test_process_library_rewrites_order_only_match_normalization(
    monkeypatch, tmp_path
):
    async def fake_metadata(item, **_kwargs):
        return metadata_for(item)

    async def fake_process_item(**_kwargs):
        return {}

    output = tmp_path / "metadata" / "movie_metadata.yml"
    output.parent.mkdir(parents=True)
    output.write_text(
        processing.yaml.safe_dump(
            {
                "metadata": {
                    "Movie 1 (2020)": {
                        "summary": "Existing",
                        "match": {
                            "title": "Movie 1",
                            "year": 2020,
                            "mapping_id": 1,
                        },
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(processing, "get_plex_metadata", fake_metadata)
    monkeypatch.setattr(processing, "process_item", fake_process_item)

    asyncio.run(
        processing.process_library(
            FakeSection([1]),
            config(tmp_path, mode="kometa"),
            feature_flags=feature_flags(),
        )
    )

    written = processing.yaml.safe_load(output.read_text(encoding="utf-8"))
    assert next(iter(written["metadata"]["Movie 1 (2020)"])) == "match"


def test_cached_tmdb_recovery_is_scoped_to_same_plex_source_id():
    meta = {
        "library_type": "movie",
        "ratingKey": "1",
        "title": "Movie 1",
        "year": 2020,
        "tmdb_id": "100",
        "plex_provider_tmdb_id": "100",
        "imdb_id": "tt0000100",
    }
    fingerprint = processing.plex_identity_fingerprint(meta)
    cache = {
        "movie:plex:1": {
            "tmdb_id": "101",
            "tmdb_recovery_source_id": "100",
            "tmdb_recovery_identity_fingerprint": fingerprint,
        }
    }
    original_meta = dict(meta)

    assert processing.apply_cached_tmdb_recovery(meta, cache) is True
    assert meta["plex_tmdb_id"] == "100"
    assert meta["tmdb_id"] == "101"

    corrected_by_plex = {**meta, "tmdb_id": "102"}
    assert processing.apply_cached_tmdb_recovery(corrected_by_plex, cache) is False
    assert corrected_by_plex["tmdb_id"] == "102"

    changed_guid = {**original_meta, "imdb_id": "tt0000200"}
    assert processing.apply_cached_tmdb_recovery(changed_guid, cache) is False
    assert changed_guid["tmdb_id"] == "100"


def test_ambiguous_blank_editions_fail_safely(monkeypatch, tmp_path):
    async def fake_metadata(item, **_kwargs):
        value = metadata_for(item)
        value["title"] = "Same Movie"
        return value

    monkeypatch.setattr(processing, "get_plex_metadata", fake_metadata)

    with pytest.raises(processing.AmbiguousEditionError, match="unique Plex edition"):
        asyncio.run(
            processing.process_library(
                FakeSection([1, 2]),
                config(tmp_path, mode="kometa"),
                feature_flags=feature_flags(),
            )
        )


def test_metadata_write_failure_propagates(monkeypatch, tmp_path):
    async def fake_metadata(item, **_kwargs):
        return metadata_for(item)

    async def fake_process_item(**_kwargs):
        return {}

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(processing, "get_plex_metadata", fake_metadata)
    monkeypatch.setattr(processing, "process_item", fake_process_item)
    monkeypatch.setattr(processing, "write_kometa_metadata", fail_write)

    with pytest.raises(processing.LibraryProcessingError, match="Unable to save"):
        asyncio.run(
            processing.process_library(
                FakeSection([1]),
                config(tmp_path, mode="kometa"),
                feature_flags=feature_flags(),
            )
        )


def test_cleanup_rejects_incomplete_plex_inventory(monkeypatch, tmp_path):
    async def incomplete_metadata(item, **_kwargs):
        value = metadata_for(item)
        value["movie_path"] = None
        return value

    flags = feature_flags()
    flags.update({"cleanup": True, "poster": True})
    monkeypatch.setattr(processing, "get_plex_metadata", incomplete_metadata)

    with pytest.raises(processing.LibraryProcessingError, match="complete Plex"):
        asyncio.run(
            processing.process_library(
                FakeSection([1]),
                config(tmp_path),
                feature_flags=flags,
            )
        )


def test_metadata_cleanup_requires_complete_tv_episode_inventory():
    flags = feature_flags()
    flags.update({"cleanup": True, "metadata_basic": True})

    errors = processing.cleanup_inventory_errors(
        [
            {
                "title": "Show",
                "year": 2020,
                "ratingKey": "1",
                "library_type": "tv",
                "seasons_episodes": None,
            }
        ],
        flags,
    )

    assert errors == ["Show (2020): seasons_episodes"]


def test_incremental_library_run_processes_only_changed_or_targeted_items(monkeypatch, tmp_path):
    class Item:
        type = "movie"
        year = 2020
        editionTitle = None

        def __init__(self, rating_key, updated_at):
            self.ratingKey = rating_key
            self.updatedAt = updated_at
            self.title = f"Movie {rating_key}"

    items = [Item("1", "same"), Item("2", "new")]
    seen = []

    async def fake_metadata(item, **_kwargs):
        return metadata_for(item.ratingKey)

    async def fake_process_item(**kwargs):
        seen.append(kwargs["plex_item"].ratingKey)
        return {}

    monkeypatch.setattr(processing, "get_plex_metadata", fake_metadata)
    monkeypatch.setattr(processing, "process_item", fake_process_item)
    monkeypatch.setattr(
        processing,
        "load_cache",
        lambda: {
            "movie:plex:1": {
                "rating_key": "1",
                "plex_updated_at": "same",
                "config_fingerprint": "fingerprint",
            }
        },
    )

    asyncio.run(
        processing.process_library(
            FakeSection(items),
            config(tmp_path),
            feature_flags=feature_flags(),
            full_scan=False,
            incremental_fingerprint="fingerprint",
        )
    )
    assert seen == ["2"]

    seen.clear()
    asyncio.run(
        processing.process_library(
            FakeSection(items),
            config(tmp_path),
            feature_flags=feature_flags(),
            full_scan=False,
            rating_keys=["1"],
            incremental_fingerprint="fingerprint",
        )
    )
    assert seen == ["1"]


def test_incremental_success_marker_waits_for_metadata_commit(monkeypatch, tmp_path):
    marker_calls = []

    async def fake_metadata(item, **_kwargs):
        value = metadata_for(item)
        value["updatedAt"] = "updated"
        return value

    async def fake_process_item(**kwargs):
        kwargs["consolidated_metadata"]["metadata"]["Movie 1 (2020)"] = {
            "summary": "new"
        }
        return {"_incremental_success": True}

    async def record_marker(*args, **kwargs):
        marker_calls.append((args, kwargs))

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(processing, "get_plex_metadata", fake_metadata)
    monkeypatch.setattr(processing, "process_item", fake_process_item)
    monkeypatch.setattr(processing, "meta_cache_async", record_marker)
    monkeypatch.setattr(processing, "write_kometa_metadata", fail_write)

    with pytest.raises(processing.LibraryProcessingError, match="Unable to save"):
        asyncio.run(
            processing.process_library(
                FakeSection([1]),
                config(tmp_path, mode="kometa"),
                feature_flags={**feature_flags(), "metadata_basic": True},
                incremental_fingerprint="fingerprint",
            )
        )

    assert marker_calls == []


def test_successful_metadata_run_persists_pending_episode_marker(
    monkeypatch, tmp_path
):
    class ShowSection(FakeSection):
        title = "TV Shows"
        type = "show"

    class Show:
        type = "show"
        title = "Example"
        year = 2020
        ratingKey = "show-1"
        updatedAt = "updated"
        childCount = 1
        seasonCount = 1
        leafCount = 2

    async def fake_metadata(_item, **_kwargs):
        return {
            "title": "Example",
            "year": 2020,
            "library_name": "TV Shows",
            "library_type": "show",
            "ratingKey": "show-1",
            "updatedAt": "updated",
            "show_path": "Example (2020)",
            "seasons_episodes": {1: [1, 2]},
        }

    async def fake_process_item(**_kwargs):
        return {
            "_incremental_success": True,
            "metadata_pending_count": 1,
        }

    marker_calls = []

    async def record_marker(*args, **kwargs):
        marker_calls.append((args, kwargs))

    monkeypatch.setattr(processing, "get_plex_metadata", fake_metadata)
    monkeypatch.setattr(processing, "process_item", fake_process_item)
    monkeypatch.setattr(processing, "meta_cache_async", record_marker)

    flags = feature_flags()
    flags["metadata_basic"] = True
    asyncio.run(
        processing.process_library(
            ShowSection([Show()]),
            config(tmp_path),
            feature_flags=flags,
            incremental_fingerprint="fingerprint",
        )
    )

    assert marker_calls[-1][1]["metadata_pending_count"] == 1
    assert marker_calls[-1][1]["plex_child_fingerprint"] == (
        processing.child_inventory_fingerprint(Show())
    )
