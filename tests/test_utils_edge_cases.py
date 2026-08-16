import asyncio
from io import BytesIO

from PIL import Image

from modules import utils


def encoded_image(color, size=(16, 16)):
    output = BytesIO()
    Image.new("RGB", size, color=color).save(output, format="JPEG")
    return output.getvalue()


def selection_config():
    return {
        "tmdb": {"fallback": ["fr"]},
        "poster_set": {
            "prefer_vote": 7,
            "vote_relaxed": 4,
            "vote_threshold": 7,
            "max_width": 1800,
            "max_height": 2700,
            "min_width": 1000,
            "min_height": 1500,
        },
        "season_set": {
            "prefer_vote": 6,
            "vote_relaxed": 3,
            "vote_threshold": 6,
            "max_width": 1800,
            "max_height": 2700,
            "min_width": 1000,
            "min_height": 1500,
        },
        "background_set": {
            "prefer_vote": 7,
            "vote_relaxed": 4,
            "vote_threshold": 7,
            "max_width": 1920,
            "max_height": 1080,
            "min_width": 1280,
            "min_height": 720,
        },
    }


def image(path, vote, width, height, language="en"):
    return {
        "file_path": path,
        "vote_average": vote,
        "width": width,
        "height": height,
        "iso_639_1": language,
    }


def test_image_selectors_cover_string_fallback_and_all_relaxation_levels():
    config = selection_config()
    config["tmdb"]["fallback"] = "fr"
    french = image("/fr.jpg", 8, 2000, 3000, "fr")
    assert utils.get_best_poster(config, [french], preferred_language="de") == french
    assert config["tmdb"]["fallback"] == "fr"

    relaxed = image("/relaxed.jpg", 5, 1200, 1800)
    minimum = image("/minimum.jpg", 1, 1200, 1800)
    tiny = image("/tiny.jpg", 1, 100, 100)
    assert utils.get_best_poster(config, [relaxed])["file_path"] == "/relaxed.jpg"
    assert utils.get_best_poster(config, [minimum])["file_path"] == "/minimum.jpg"
    assert utils.get_best_poster(config, [tiny])["file_path"] == "/tiny.jpg"
    assert utils.get_best_poster(config, []) is None

    season_relaxed = image("/season-relaxed.jpg", 4, 1200, 1800)
    season_minimum = image("/season-minimum.jpg", 1, 1200, 1800)
    assert utils.get_best_season(config, [season_relaxed])["file_path"] == (
        "/season-relaxed.jpg"
    )
    assert utils.get_best_season(config, [season_minimum])["file_path"] == (
        "/season-minimum.jpg"
    )
    assert utils.get_best_season(config, [tiny])["file_path"] == "/tiny.jpg"
    assert utils.get_best_season(config, []) is None

    relaxed_background = image("/bg-relaxed.jpg", 5, 1600, 900, None)
    minimum_background = image("/bg-minimum.jpg", 1, 1600, 900, None)
    assert utils.get_best_background(config, [relaxed_background])["file_path"] == (
        "/bg-relaxed.jpg"
    )
    assert utils.get_best_background(config, [minimum_background])["file_path"] == (
        "/bg-minimum.jpg"
    )
    assert utils.get_best_background(config, [tiny])["file_path"] == "/tiny.jpg"
    assert utils.get_best_background(config, []) is None


def test_metadata_diff_helpers_cover_exclusions_lists_and_shape_changes():
    existing = {
        "title": "Same",
        "genres": ["Drama", None, ""],
        "nested": {"value": "old"},
        "ignored": "old",
    }
    new = {
        "title": "Same",
        "genres": ["Drama"],
        "nested": {"value": "new"},
        "ignored": "new",
        "added": "yes",
    }
    changes = utils.smart_meta_update(existing, new, exclude_fields={"ignored"})
    assert set(changes) == {"nested", "added"}
    assert utils.smart_meta_update({"nested": []}, {"nested": {"value": 1}}) == [
        "nested"
    ]
    assert set(utils.recursive_season_diff({"a": [1]}, {"a": [2, 3]})) == {
        "['a']",
    }
    assert utils.get_meta_field([], "title", default="fallback") == "fallback"
    assert utils.format_runtime(None) == ""
    assert utils.format_runtime(60) == "1 hr 0 mins"
    assert utils.format_runtime(0) == "0 mins"


def test_movie_asset_upgrade_decision_matrix(monkeypatch, tmp_path):
    config = selection_config()
    asset = tmp_path / "poster.jpg"
    candidate = tmp_path / "candidate.jpg"
    candidate.write_bytes(encoded_image("blue", (32, 32)))
    cache_state = {"movie": {"poster_average": 2}}
    monkeypatch.setattr(utils, "load_cache", lambda: cache_state)
    monkeypatch.setattr(utils, "stale_image", lambda *_args, **_kwargs: False)

    decision = utils.smart_asset_upgrade(
        config,
        asset,
        {"width": 32, "height": 32, "vote_average": 8},
        new_image_path=candidate,
        cache_key="movie",
    )
    assert decision[0:2] == (True, "NO_EXISTING_ASSET")

    asset.write_bytes(candidate.read_bytes())
    decision = utils.smart_asset_upgrade(
        config,
        asset,
        {"width": 32, "height": 32, "vote_average": 8},
        new_image_path=candidate,
        cache_key="movie",
    )
    assert decision[0:2] == (False, "ALREADY_UP_TO_DATE")

    monkeypatch.setattr(utils, "stale_image", lambda *_args, **_kwargs: True)
    decision = utils.smart_asset_upgrade(
        config,
        asset,
        {"width": 32, "height": 32, "vote_average": 8},
        new_image_path=candidate,
        cache_key="movie",
    )
    assert decision[0:2] == (False, "ALREADY_UP_TO_DATE")
    monkeypatch.setattr(utils, "stale_image", lambda *_args, **_kwargs: False)

    candidate.write_bytes(encoded_image("green", (32, 32)))
    decision = utils.smart_asset_upgrade(
        config,
        asset,
        {"width": 32, "height": 32, "vote_average": 7},
        new_image_path=candidate,
        cache_key="movie",
    )
    assert decision[0:2] == (True, "UPGRADE_THRESHOLD")

    cache_state["movie"]["poster_average"] = 8
    decision = utils.smart_asset_upgrade(
        config,
        asset,
        {"width": 64, "height": 64, "vote_average": 7},
        new_image_path=candidate,
        cache_key="movie",
    )
    assert decision[0:2] == (True, "UPGRADE_DIMENSIONS")

    decision = utils.smart_asset_upgrade(
        config,
        asset,
        {"width": 8, "height": 8, "vote_average": 7},
        new_image_path=candidate,
        cache_key="movie",
    )
    assert decision[0:2] == (False, "NO_UPGRADE_NEEDED")

    assert utils.smart_asset_upgrade(
        config,
        asset,
        {"width": 8, "height": 8, "vote_average": 7},
        cache_key="movie",
    )[0:2] == (False, "NO_IMAGE_FOR_COMPARE")


def test_asset_upgrade_stale_background_and_corrupt_existing(monkeypatch, tmp_path):
    config = selection_config()
    asset = tmp_path / "fanart.jpg"
    candidate = tmp_path / "candidate.jpg"
    candidate.write_bytes(encoded_image("blue"))
    monkeypatch.setattr(utils, "load_cache", lambda: {"movie": {}})

    assert utils.smart_asset_upgrade(
        config,
        asset,
        {"width": 16, "height": 16, "vote_average": 1},
        new_image_path=candidate,
        cache_key="movie",
        asset_type="background",
    )[0:2] == (True, "NO_EXISTING_ASSET")

    asset.write_bytes(encoded_image("red"))
    assert utils.smart_asset_upgrade(
        config,
        asset,
        {"width": 16, "height": 16, "vote_average": 1},
        new_image_path=candidate,
        cache_key="movie",
        asset_type="background",
    )[0:2] == (True, "FORCE_UPGRADE_STALE")

    monkeypatch.setattr(utils, "stale_image", lambda *_args, **_kwargs: False)
    asset.write_bytes(b"not-an-image")
    assert utils.smart_asset_upgrade(
        config,
        asset,
        {"width": 16, "height": 16, "vote_average": 1},
        new_image_path=candidate,
        cache_key="movie",
        asset_type="background",
    )[0:2] == (False, "ERROR_IMAGE_COMPARE")


def test_season_asset_upgrade_decision_matrix(monkeypatch, tmp_path):
    config = selection_config()
    asset = tmp_path / "Season00.jpg"
    candidate = tmp_path / "candidate.jpg"
    asset.write_bytes(encoded_image("red", (16, 16)))
    candidate.write_bytes(encoded_image("blue", (32, 32)))
    cache_state = {
        "show": {
            "seasons": {
                "0": {"season_average": 0, "season_last_upgraded": "recent"}
            }
        }
    }
    monkeypatch.setattr(utils, "load_cache", lambda: cache_state)
    monkeypatch.setattr(utils, "stale_image", lambda *_args, **_kwargs: False)

    decision = utils.smart_season_asset_upgrade(
        config,
        asset,
        {"width": 32, "height": 32, "vote_average": 1},
        new_image_path=candidate,
        cache_key="show",
        season_number=0,
    )
    assert decision[0:2] == (True, "UPGRADE_ZERO_VOTE_SEASON")

    cache_state["show"]["seasons"]["0"]["season_average"] = 5
    decision = utils.smart_season_asset_upgrade(
        config,
        asset,
        {"width": 32, "height": 32, "vote_average": 4},
        new_image_path=candidate,
        cache_key="show",
        season_number=0,
    )
    assert decision[0:2] == (True, "UPGRADE_VOTES_SEASON")

    decision = utils.smart_season_asset_upgrade(
        config,
        asset,
        {"width": 8, "height": 8, "vote_average": 6},
        new_image_path=candidate,
        cache_key="show",
        season_number=0,
    )
    assert decision[0:2] == (True, "UPGRADE_THRESHOLD_SEASON")

    candidate.write_bytes(asset.read_bytes())
    assert utils.smart_season_asset_upgrade(
        config,
        asset,
        {"width": 16, "height": 16, "vote_average": 5},
        new_image_path=candidate,
        cache_key="show",
        season_number=0,
    )[0:2] == (False, "ALREADY_UP_TO_DATE_SEASON")


def test_download_rejects_empty_corrupt_and_failed_atomic_writes(monkeypatch, tmp_path):
    output = tmp_path / "poster.jpg"

    async def empty_response(*_args, **_kwargs):
        return b""

    monkeypatch.setattr(utils, "tmdb_api_request", empty_response)
    assert asyncio.run(
        utils.download_poster({}, "/poster.jpg", output, session=object())
    ) == (False, None, "Empty or rejected response from TMDb")

    async def corrupt_response(*_args, **_kwargs):
        return b"<html>not an image</html>"

    monkeypatch.setattr(utils, "tmdb_api_request", corrupt_response)
    success, status, error = asyncio.run(
        utils.download_poster({}, "/poster.jpg", output, session=object())
    )
    assert success is False
    assert status is None
    assert error
    assert not output.exists()

    existing = encoded_image("red")
    output.write_bytes(existing)

    def disk_full(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(utils, "atomic_write_bytes", disk_full)
    assert asyncio.run(utils.save_poster(encoded_image("blue"), output))[0] is False
    assert output.read_bytes() == existing


def test_download_preserves_http_status_and_save_failure(monkeypatch, tmp_path):
    class RequestFailure(RuntimeError):
        status = 503

    async def failed_request(*_args, **_kwargs):
        raise RequestFailure("unavailable")

    monkeypatch.setattr(utils, "tmdb_api_request", failed_request)
    result = asyncio.run(
        utils.download_poster({}, "/poster.jpg", tmp_path / "one.jpg", session=object())
    )
    assert result == (False, 503, "unavailable")

    async def valid_request(*_args, **_kwargs):
        return encoded_image("green")

    async def failed_save(*_args, **_kwargs):
        return False, "permission denied"

    monkeypatch.setattr(utils, "tmdb_api_request", valid_request)
    monkeypatch.setattr(utils, "save_poster", failed_save)
    result = asyncio.run(
        utils.download_poster({}, "/poster.jpg", tmp_path / "two.jpg", session=object())
    )
    assert result == (False, None, "permission denied")


def test_asset_paths_cover_show_specials_and_unknown_types(tmp_path):
    show = {
        "library_type": "tv",
        "show_dir": str(tmp_path / "show"),
        "show_path": "Example Show (2021)",
    }
    plex = {"settings": {"mode": "plex"}}
    assert utils.get_asset_path(plex, show, "poster") == tmp_path / "show" / "poster.jpg"
    assert utils.get_asset_path(plex, show, "background") == (
        tmp_path / "show" / "fanart.jpg"
    )
    assert utils.get_asset_path(plex, show, "season", season_number=0) == (
        tmp_path / "show" / "Season 00" / "Season00.jpg"
    )

    kometa = {"settings": {"mode": "kometa", "path": str(tmp_path / "kometa")}}
    assert utils.get_asset_path(kometa, show, "season", season_number=0) == (
        tmp_path / "kometa" / "assets" / "tv" / "Example Show (2021)" / "Season00.jpg"
    )
    assert utils.get_asset_path(kometa, show, "unsupported") is None
    assert utils.asset_temp_path(plex, show, extension="png").suffix == ".png"

    unknown = {"library_type": "music"}
    assert str(utils.asset_temp_path(plex, unknown).parent) == "."
