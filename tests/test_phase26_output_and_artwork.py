import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from helper import output_management
from helper import plex_artwork_verification as verification


def _config(tmp_path, *, mode="kometa"):
    return {
        "settings": {"mode": mode, "path": str(tmp_path / "kometa")},
        "plex": {
            "url": "http://plex:32400",
            "token": "token",
            "path_mappings": [f"/plex=>{tmp_path / 'media'}", "invalid"],
        },
        "runtime": {"max_image_mb": 1},
        "output": {"validate_schema": False, "backup_count": 1},
    }


def _item(**updates):
    value = {
        "cache_key": "movie:plex:10",
        "library_name": "Movies",
        "rating_key": "10",
        "media_type": "movie",
        "title": "Example",
        "year": 2020,
        "tmdb_id": "100",
        "edition": None,
    }
    value.update(updates)
    return value


def _claim(path, **updates):
    value = {
        "cache_key": "movie:plex:10",
        "asset_type": "poster",
        "season_number": -1,
        "destination": str(path),
        "checksum": None,
    }
    value.update(updates)
    return value


def test_output_target_resolution_and_managed_roots(monkeypatch, tmp_path):
    config = _config(tmp_path)
    assert output_management._managed_roots(config) == [
        (tmp_path / "kometa" / "assets").resolve()
    ]
    config["settings"]["mode"] = "plex"
    assert output_management._managed_roots(config) == [
        (tmp_path / "media").resolve()
    ]

    monkeypatch.setattr(output_management, "find_media_state", lambda **_kwargs: [])
    with pytest.raises(output_management.OutputManagementError, match="No durable"):
        output_management.resolve_output_targets(config)
    monkeypatch.setattr(
        output_management, "find_media_state", lambda **_kwargs: [_item(), _item()]
    )
    with pytest.raises(output_management.OutputManagementError, match="multiple"):
        output_management.resolve_output_targets(config)
    monkeypatch.setattr(
        output_management, "find_media_state", lambda **_kwargs: [_item()]
    )
    assert output_management.resolve_output_targets(
        config, libraries=["Movies"], rating_keys=["10"], tmdb_ids=["100"]
    )["rating_key"] == "10"


def test_safe_asset_decisions_cover_every_protection(monkeypatch, tmp_path):
    config = _config(tmp_path)
    managed = tmp_path / "kometa" / "assets" / "movie" / "poster.jpg"
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"managed")
    checksum = hashlib.sha256(b"managed").hexdigest()

    forgotten = output_management._safe_asset_decision(
        config, _item(), _claim(managed, checksum=checksum), "forget"
    )
    assert forgotten["status"] == "eligible"
    assert "retained" in forgotten["reason"]

    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    assert output_management._safe_asset_decision(
        config, _item(), _claim(outside, checksum=checksum), "remove"
    )["status"] == "protected"

    link = managed.with_name("link.jpg")
    link.symlink_to(managed)
    assert output_management._safe_asset_decision(
        config, _item(), _claim(link, checksum=checksum), "remove"
    )["reason"] == "destination is a symbolic link"

    absent = managed.with_name("absent.jpg")
    assert output_management._safe_asset_decision(
        config, _item(), _claim(absent, checksum=checksum), "remove"
    )["status"] == "already_absent"

    directory = managed.with_name("directory")
    directory.mkdir()
    assert output_management._safe_asset_decision(
        config, _item(), _claim(directory, checksum=checksum), "remove"
    )["reason"] == "destination is not a regular file"
    assert output_management._safe_asset_decision(
        config, _item(), _claim(managed), "remove"
    )["reason"] == "ownership record has no checksum"

    original_hash = output_management.sha256_file
    monkeypatch.setattr(
        output_management,
        "sha256_file",
        lambda _path: (_ for _ in ()).throw(OSError("read failure")),
    )
    unreadable = output_management._safe_asset_decision(
        config, _item(), _claim(managed, checksum=checksum), "remove"
    )
    assert unreadable["status"] == "protected"
    monkeypatch.setattr(output_management, "sha256_file", original_hash)

    modified = output_management._safe_asset_decision(
        config, _item(), _claim(managed, checksum="different"), "remove"
    )
    assert modified["reason"] == "file was modified after MetaFusion wrote it"
    eligible = output_management._safe_asset_decision(
        config,
        _item(),
        _claim(managed, checksum=checksum, season_number="2"),
        "remove",
    )
    assert eligible["status"] == "eligible"
    assert eligible["season_number"] == 2


def test_metadata_matching_and_decisions(monkeypatch, tmp_path):
    config = _config(tmp_path)
    item = _item()
    assert output_management._metadata_match(item, "Example (2020)", "invalid") is False
    assert output_management._metadata_match(
        item, "Alias", {"match": {"mapping_id": 100}}
    ) is True
    assert output_management._metadata_match(
        _item(edition="Extended"),
        "Alias",
        {"match": {"mapping_id": "100", "edition": "Theatrical"}},
    ) is False
    assert output_management._metadata_match(
        _item(tmdb_id=None), "Example (2020)", {}
    ) is True

    config["settings"]["mode"] = "plex"
    assert output_management._metadata_decision(config, item, "remove")["status"] == "protected"
    config["settings"]["mode"] = "kometa"
    missing = output_management._metadata_decision(config, item, "remove")
    assert missing["status"] == "already_absent"

    path = Path(missing["destination"])
    path.parent.mkdir(parents=True)
    path.write_text("metadata: [invalid\n", encoding="utf-8")
    assert output_management._metadata_decision(config, item, "remove")["status"] == "protected"

    path.write_text("metadata: {}\n", encoding="utf-8")
    assert "found 0" in output_management._metadata_decision(config, item, "remove")["reason"]
    document = {
        "metadata": {
            "First": {"match": {"mapping_id": 100}},
            "Second": {"match": {"mapping_id": "100"}},
        }
    }
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    assert "found 2" in output_management._metadata_decision(config, item, "remove")["reason"]
    document["metadata"].pop("Second")
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    decision = output_management._metadata_decision(config, item, "remove")
    assert decision["status"] == "eligible"
    assert decision["metadata_key"] == "First"


def test_output_plan_filters_and_apply_paths(monkeypatch, tmp_path):
    config = _config(tmp_path)
    item = _item(media_type="tv")
    season = tmp_path / "kometa" / "assets" / "show" / "Season02.jpg"
    season.parent.mkdir(parents=True)
    season.write_bytes(b"season")
    checksum = hashlib.sha256(b"season").hexdigest()
    claims = [
        _claim(season, asset_type="season", season_number="2", checksum=checksum),
        _claim(season, asset_type="season", season_number="3", checksum=checksum),
        _claim(season, asset_type="poster", season_number=-1, checksum=checksum),
    ]
    monkeypatch.setattr(output_management, "load_asset_ownership", lambda _keys: claims)
    monkeypatch.setattr(
        output_management,
        "_metadata_decision",
        lambda *_args: {"output_type": "metadata", "status": "already_absent"},
    )
    with pytest.raises(output_management.OutputManagementError, match="action"):
        output_management.plan_output_management(config, item, action="destroy")
    with pytest.raises(output_management.OutputManagementError, match="type"):
        output_management.plan_output_management(
            config, item, action="preview", output_type="video"
        )
    decisions = output_management.plan_output_management(
        config, item, action="remove", output_type="season", season_number=2
    )
    assert len(decisions) == 1 and decisions[0]["season_number"] == 2
    assert len(output_management.plan_output_management(config, item, action="preview")) == 4

    assert output_management.apply_output_management(
        config, item, decisions, action="preview"
    ) == decisions
    protected = [{"output_type": "poster", "status": "protected"}]
    assert output_management.apply_output_management(
        config, item, protected, action="remove"
    )[0]["status"] == "protected"

    metadata = {
        "output_type": "metadata",
        "status": "eligible",
        "destination": "metadata.yml",
        "metadata_key": "Example",
    }
    assert output_management.apply_output_management(
        config, item, [metadata], action="forget"
    )[0]["status"] == "protected"
    assert output_management.apply_output_management(
        config, item, [metadata], action="remove"
    )[0]["status"] == "protected"

    removed = []
    history = []
    monkeypatch.setattr(
        output_management, "_remove_metadata", lambda *_args: removed.append("metadata")
    )
    monkeypatch.setattr(
        output_management,
        "remove_asset_ownership",
        lambda *args: removed.append(args),
    )
    monkeypatch.setattr(
        output_management,
        "record_cleanup_history",
        lambda *args, **kwargs: history.append((args, kwargs)),
    )
    result = output_management.apply_output_management(
        config,
        item,
        [metadata],
        action="remove",
        acknowledge_metadata_loss=True,
    )
    assert result[0]["status"] == "removed"
    assert removed == ["metadata"] and history

    already_absent = dict(metadata, status="already_absent")
    output_management.apply_output_management(
        config,
        item,
        [already_absent],
        action="remove",
        acknowledge_metadata_loss=True,
    )
    assert removed == ["metadata"]

    asset = season.with_name("poster.jpg")
    asset.write_bytes(b"asset")
    asset_decision = {
        "output_type": "poster",
        "season_number": None,
        "destination": str(asset),
        "checksum": "hash",
        "status": "eligible",
        "reason": "owned",
    }
    forgotten = output_management.apply_output_management(
        config, item, [asset_decision], action="forget"
    )
    assert forgotten[0]["status"] == "forgotten" and asset.exists()
    removed_asset = output_management.apply_output_management(
        config, item, [asset_decision], action="remove"
    )
    assert removed_asset[0]["status"] == "removed" and not asset.exists()


def test_remove_metadata_and_output_report(monkeypatch, tmp_path):
    config = _config(tmp_path)
    item = _item()
    path = tmp_path / "kometa" / "metadata" / "movie_metadata.yml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump({"metadata": {"Example": {"match": {"mapping_id": 100}}}}),
        encoding="utf-8",
    )
    captured = {}
    monkeypatch.setattr(
        output_management,
        "write_kometa_metadata",
        lambda target, document, **kwargs: captured.update(
            target=target, document=document, kwargs=kwargs
        ),
    )
    output_management._remove_metadata(
        config, item, {"destination": str(path), "metadata_key": "Example"}
    )
    assert captured["document"] == {"metadata": {}}
    assert captured["kwargs"]["library_type"] == "movie"

    report = output_management.write_output_management_report(
        item, [], action="preview", base_dir=tmp_path
    )
    assert "no matching generated output" in report.read_text(encoding="utf-8")
    report = output_management.write_output_management_report(
        item,
        [
            {
                "status": "eligible",
                "output_type": "season",
                "season_number": 2,
                "destination": "/asset.jpg",
                "reason": "owned",
            }
        ],
        action="remove",
        base_dir=tmp_path,
    )
    assert "season season 2" in report.read_text(encoding="utf-8")
    assert report.with_suffix(".json").exists()


class _Response:
    def __init__(self, status=200, content=b"image", headers=None, read_error=None):
        self.status = status
        self.content = content
        self.headers = headers or {}
        self.read_error = read_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def read(self):
        if self.read_error:
            raise self.read_error
        return self.content


class _Session:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def test_plex_image_download_and_source_selection(tmp_path):
    config = _config(tmp_path, mode="plex")
    assert asyncio.run(verification._download_plex_image(config, None, _Session())) == (
        None,
        "Plex exposes no selected image endpoint",
    )
    content, error = asyncio.run(
        verification._download_plex_image(
            config, "/library/poster", _Session(_Response(status=404))
        )
    )
    assert content is None and "HTTP 404" in error
    content, error = asyncio.run(
        verification._download_plex_image(
            config,
            "http://other/poster",
            _Session(_Response(headers={"Content-Length": str(2 * 1024 * 1024)})),
        )
    )
    assert content is None and "download limit" in error
    content, error = asyncio.run(
        verification._download_plex_image(
            config, "/library/poster", _Session(_Response(content=b"x" * (1024 * 1024 + 1)))
        )
    )
    assert content is None and "download limit" in error
    session = _Session(_Response(content=b"valid"))
    assert asyncio.run(
        verification._download_plex_image(config, "/library/poster", session)
    ) == (b"valid", None)
    assert session.calls[0][0] == "http://plex:32400/library/poster"
    content, error = asyncio.run(
        verification._download_plex_image(
            config, "/library/poster", _Session(error=TimeoutError())
        )
    )
    assert content is None and error.endswith("TimeoutError")

    meta = {
        "plex_artwork": {
            "poster": "/poster",
            "background": "/background",
            "seasons": {2: "/season2", "3": "/season3"},
        }
    }
    assert verification._plex_source(meta, {"asset_type": "poster"}) == "/poster"
    assert verification._plex_source(meta, {"asset_type": "background"}) == "/background"
    assert verification._plex_source(
        meta, {"asset_type": "season", "season_number": "2"}
    ) == "/season2"
    assert verification._plex_source(
        meta, {"asset_type": "season", "season_number": "3"}
    ) == "/season3"
    assert verification._plex_source(
        meta, {"asset_type": "season", "season_number": "unknown"}
    ) is None
    assert verification._hash_distance("0f", "00") == 4
    assert verification._hash_distance("invalid", "00") is None


def test_verify_plex_artwork_inventory_outcomes(monkeypatch, tmp_path):
    section = SimpleNamespace(
        title="Movies",
        uuid="uuid",
        _server=SimpleNamespace(machineIdentifier="server"),
    )
    items = [
        SimpleNamespace(ratingKey=str(key), title=f"Item {key}")
        for key in range(1, 10)
    ]
    states = [
        {
            "cache_key": f"movie:plex:{key}",
            "server_id": "server",
            "library_uuid": "uuid",
            "library_name": "Movies",
            "rating_key": str(key),
            "title": f"Item {key}",
            "year": 2020,
            "tmdb_id": str(100 + key),
            "imdb_id": f"tt{key}",
            "tvdb_id": None,
        }
        for key in range(2, 10)
    ]
    paths = {}
    claims = []
    for key in range(3, 10):
        path = tmp_path / f"{key}.jpg"
        if key != 3:
            path.write_bytes(f"local-{key}".encode())
        paths[key] = path
        claims.append(
            {
                "cache_key": f"movie:plex:{key}",
                "asset_type": "poster",
                "season_number": -1,
                "destination": str(path),
                "checksum": f"checksum-{key}",
            }
        )

    async def inventory(_section, _runtime):
        return items

    async def metadata(item, **_kwargs):
        return {"plex_artwork": {"poster": f"/poster/{item.ratingKey}"}}

    monkeypatch.setattr(verification, "load_plex_library_inventory", inventory)
    monkeypatch.setattr(verification, "find_media_state", lambda **_kwargs: states)
    monkeypatch.setattr(verification, "load_asset_ownership", lambda _keys: claims)
    monkeypatch.setattr(verification, "get_plex_metadata", metadata)

    def checksum(path):
        key = int(Path(path).stem)
        if key == 4:
            raise OSError("unreadable")
        return "different" if key == 5 else f"checksum-{key}"

    def analyze(content, **_kwargs):
        text = content.decode()
        if text in {"local-4", "plex-8"}:
            raise ValueError("invalid image")
        key = int(text.split("-")[-1])
        local = text.startswith("local")
        hashes = {
            6: ("00", "00"),
            7: ("00", "01"),
            9: ("0000", "ffff"),
        }
        left, right = hashes.get(key, ("00", "ff"))
        return {
            "content_sha256": "same" if key == 6 else text,
            "perceptual_hash": left if local else right,
            "width": 1000,
            "height": 1500,
        }

    async def download(_config, source, _session):
        key = int(source.rsplit("/", 1)[-1])
        if key == 6:
            return b"local-6", None
        if key == 7:
            return b"plex-7", None
        if key == 8:
            return b"plex-8", None
        if key == 9:
            return b"plex-9", None
        return None, "Plex unavailable"

    monkeypatch.setattr(verification, "sha256_file", checksum)
    monkeypatch.setattr(verification, "analyze_image_content", analyze)
    monkeypatch.setattr(verification, "_download_plex_image", download)
    records = asyncio.run(
        verification.verify_plex_artwork(
            [section], _config(tmp_path, mode="plex"), [str(key) for key in range(1, 11)], object()
        )
    )
    status = {record["plex_rating_key"]: record["status"] for record in records}
    assert status == {
        "1": "unmanaged",
        "2": "unmanaged",
        "3": "local_missing",
        "4": "unverifiable",
        "5": "modified",
        "6": "selected",
        "7": "selected",
        "8": "unverifiable",
        "9": "not_selected",
        "10": "not_found",
    }
    selected = next(record for record in records if record["plex_rating_key"] == "7")
    assert selected["exact_match"] is False
    assert selected["perceptual_distance"] == 1


def test_verify_plex_artwork_plex_unavailable_and_report(monkeypatch, tmp_path):
    section = SimpleNamespace(title="Shows", key="2", _server=SimpleNamespace())
    item = SimpleNamespace(ratingKey="20", title="Example")
    destination = tmp_path / "season.jpg"
    destination.write_bytes(b"local")

    async def inventory(_section, _runtime):
        return [item]

    async def metadata(_item, **_kwargs):
        return {"plex_artwork": {"seasons": {1: None}}}

    async def unavailable(*_args):
        return None, "Plex exposes no selected image endpoint"

    monkeypatch.setattr(verification, "load_plex_library_inventory", inventory)
    monkeypatch.setattr(
        verification,
        "find_media_state",
        lambda **_kwargs: [
            {
                "cache_key": "tv:plex:20",
                "server_id": "unknown",
                "library_uuid": "2",
                "rating_key": "20",
                "title": "Example",
            }
        ],
    )
    monkeypatch.setattr(
        verification,
        "load_asset_ownership",
        lambda _keys: [
            {
                "cache_key": "tv:plex:20",
                "asset_type": "season",
                "season_number": "1",
                "destination": str(destination),
                "checksum": "owned",
            }
        ],
    )
    monkeypatch.setattr(verification, "get_plex_metadata", metadata)
    monkeypatch.setattr(verification, "sha256_file", lambda _path: "owned")
    monkeypatch.setattr(
        verification,
        "analyze_image_content",
        lambda *_args, **_kwargs: {
            "content_sha256": "local",
            "perceptual_hash": "00",
            "width": 1000,
            "height": 1500,
        },
    )
    monkeypatch.setattr(verification, "_download_plex_image", unavailable)
    records = asyncio.run(
        verification.verify_plex_artwork(
            [section], _config(tmp_path, mode="plex"), None, object()
        )
    )
    assert records[0]["status"] == "plex_unavailable"

    report = verification.write_plex_artwork_verification_report(
        records, base_dir=tmp_path
    )
    assert "season season 1" in report.read_text(encoding="utf-8")
    assert report.with_suffix(".json").exists()
