import asyncio
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from PIL import Image

from modules import utils as utils_module
from modules.utils import (
    asset_temp_path,
    download_poster,
    format_runtime,
    get_asset_path,
    get_best_background,
    get_best_poster,
    get_best_season,
    get_meta_field,
    recursive_season_diff,
    save_poster,
    smart_meta_update,
    stale_image,
)


def image_config():
    return {
        "tmdb": {"fallback": ["fr"]},
        "poster_set": {
            "prefer_vote": 7,
            "vote_relaxed": 4,
            "max_width": 1800,
            "max_height": 2700,
            "min_width": 1000,
            "min_height": 1500,
        },
        "season_set": {
            "prefer_vote": 6,
            "vote_relaxed": 3,
            "max_width": 1800,
            "max_height": 2700,
            "min_width": 1000,
            "min_height": 1500,
        },
    }


def encoded_image(color):
    output = BytesIO()
    Image.new("RGB", (8, 8), color=color).save(output, format="JPEG")
    return output.getvalue()


def test_poster_and_season_selection_respect_language_and_thresholds():
    images = [
        {
            "iso_639_1": "fr",
            "vote_average": 9,
            "width": 2000,
            "height": 3000,
            "file_path": "/fr.jpg",
        },
        {
            "iso_639_1": "en",
            "vote_average": 8,
            "width": 2000,
            "height": 3000,
            "file_path": "/en.jpg",
        },
    ]

    assert get_best_poster(image_config(), images, preferred_language="en")["file_path"] == "/en.jpg"
    assert get_best_season(image_config(), images, preferred_language="de")["file_path"] == "/fr.jpg"


def test_metadata_helpers_detect_nested_changes_and_format_runtime():
    old = {"title": "Example", "genres": ["Drama"], "seasons": {"1": {"title": "One"}}}
    new = {"title": "Example", "genres": ["Comedy"], "seasons": {"1": {"title": "First"}}}

    assert smart_meta_update(old, new) == ["genres", "seasons"]
    assert "['1']['title']" in recursive_season_diff(old["seasons"], new["seasons"])
    assert get_meta_field(new, "title") == "Example"
    assert get_meta_field(new, "title", path=["missing"]) is None
    assert format_runtime(125) == "2 hrs 5 mins"
    assert format_runtime(1) == "1 min"
    assert format_runtime("invalid") == "invalid"


def test_stale_image_handles_recent_old_and_invalid_dates():
    recent = datetime.now(timezone.utc).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()

    assert stale_image(recent, days=30) is False
    assert stale_image(old, days=30) is True
    assert stale_image("not-a-date", days=30) is True
    assert stale_image(None, days=30) is True
    assert stale_image(recent.replace("+00:00", "Z"), days=30) is False


def test_background_selection_and_asset_paths_cover_plex_and_kometa(tmp_path):
    config = image_config()
    config["background_set"] = {
        "prefer_vote": 7,
        "vote_relaxed": 4,
        "max_width": 1920,
        "max_height": 1080,
        "min_width": 1280,
        "min_height": 720,
    }
    images = [
        {"vote_average": 8, "width": 1920, "height": 1080, "file_path": "/best.jpg"},
        {"vote_average": 3, "width": 3840, "height": 2160, "file_path": "/large.jpg"},
    ]
    assert get_best_background(config, images)["file_path"] == "/best.jpg"

    movie = {
        "library_type": "movie",
        "movie_dir": str(tmp_path / "movie"),
        "movie_path": "Example (2020)",
    }
    plex_config = {"settings": {"mode": "plex"}}
    assert get_asset_path(plex_config, movie, "poster") == tmp_path / "movie" / "poster.jpg"
    assert get_asset_path(plex_config, movie, "background") == tmp_path / "movie" / "fanart.jpg"

    kometa_config = {"settings": {"mode": "kometa", "path": str(tmp_path / "kometa")}}
    assert get_asset_path(kometa_config, movie, "poster") == (
        tmp_path / "kometa" / "assets" / "movie" / "Example (2020)" / "poster.jpg"
    )
    assert asset_temp_path(kometa_config, movie).parent == tmp_path / "kometa" / "assets" / "movie"


def test_save_and_download_poster_handle_success_duplicate_and_missing_session(monkeypatch, tmp_path):
    path = tmp_path / "poster.jpg"
    first_image = encoded_image("red")
    second_image = encoded_image("blue")
    assert asyncio.run(save_poster(first_image, path)) == (True, None)
    assert asyncio.run(save_poster(first_image, path)) == ("ALREADY_UP_TO_DATE", None)

    assert asyncio.run(download_poster({}, "/poster.jpg", path, session=None)) == (
        False,
        None,
        "HTTP session failed",
    )

    async def fake_request(*_args, **_kwargs):
        return second_image

    monkeypatch.setattr(utils_module, "tmdb_api_request", fake_request)
    success, status, error = asyncio.run(
        download_poster({}, "/poster.jpg", path, session=object())
    )
    assert (success, status, error) == (True, 200, None)
    assert path.read_bytes() == second_image


def test_download_cancellation_propagates_without_partial_output(monkeypatch, tmp_path):
    state = {}

    async def blocked_request(*_args, **_kwargs):
        state["started"].set()
        await asyncio.Event().wait()

    monkeypatch.setattr(utils_module, "tmdb_api_request", blocked_request)

    async def scenario():
        state["started"] = asyncio.Event()
        output = tmp_path / "cancelled.jpg"
        task = asyncio.create_task(
            download_poster({}, "/poster.jpg", output, session=object())
        )
        await state["started"].wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return output

    output = asyncio.run(scenario())
    assert not output.exists()
