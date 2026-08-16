from datetime import datetime, timedelta

from modules.utils import (
    format_runtime,
    get_best_poster,
    get_best_season,
    get_meta_field,
    recursive_season_diff,
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
    recent = datetime.now().isoformat()
    old = (datetime.now() - timedelta(days=31)).isoformat()

    assert stale_image(recent, days=30) is False
    assert stale_image(old, days=30) is True
    assert stale_image("not-a-date", days=30) is True
    assert stale_image(None, days=30) is True
