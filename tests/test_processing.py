import asyncio

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


def test_process_library_propagates_item_failures(monkeypatch, tmp_path):
    async def fake_metadata(item, **_kwargs):
        return metadata_for(item)

    async def fake_process_item(**_kwargs):
        raise RuntimeError("builder failed")

    monkeypatch.setattr(processing, "get_plex_metadata", fake_metadata)
    monkeypatch.setattr(processing, "process_item", fake_process_item)

    with pytest.raises(processing.LibraryProcessingError, match="items failed"):
        asyncio.run(
            processing.process_library(
                FakeSection([1]),
                config(tmp_path),
                feature_flags=feature_flags(),
            )
        )


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
