from datetime import datetime, timezone
from io import BytesIO

from PIL import Image

from modules import utils


def _config():
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


def _encoded(color="red", size=(16, 24)):
    output = BytesIO()
    Image.new("RGB", size, color=color).save(output, format="JPEG")
    return output.getvalue()


def _candidate(path, *, language="en", width=1200, height=1800, vote=5, analysis=None):
    result = {
        "file_path": path,
        "iso_639_1": language,
        "width": width,
        "height": height,
        "vote_average": vote,
    }
    if analysis is not None:
        result["content_analysis"] = analysis
    return result


def test_artwork_explanations_and_acceptance_cover_rejection_reasons():
    config = _config()
    selected = _candidate(
        "/selected.jpg",
        language=None,
        vote=9,
        width=1800,
        height=2700,
        analysis={
            "width": 1800,
            "height": 2700,
            "blank": False,
            "sharpness": 50,
            "perceptual_hash": "same",
        },
    )
    rejected = _candidate(
        "/rejected.jpg",
        language="de",
        vote=1,
        width=100,
        height=100,
        analysis={
            "width": 100,
            "height": 100,
            "blank": True,
            "perceptual_hash": "same",
        },
    )
    reasons = utils.artwork_candidate_explanations(
        config, [selected, rejected], selected, preferred_language="en"
    )[0]["reasons"]
    assert "outside configured language fallback" in reasons
    assert "less suitable aspect ratio" in reasons
    assert "cached blank-image detection" in reasons
    assert "visually duplicates the selected artwork" in reasons

    selected_english = dict(selected, iso_639_1="en")
    fallback = _candidate("/fallback.jpg", language="fr")
    reasons = utils.artwork_candidate_explanations(
        config, [selected_english, fallback], selected_english, preferred_language="en"
    )[0]["reasons"]
    assert "lower language priority" in reasons

    tied_selected = _candidate("/tie-selected.jpg", language="en")
    tied = dict(tied_selected, file_path="/tied.jpg")
    assert utils.artwork_candidate_explanations(
        config, [tied_selected, tied], tied_selected, preferred_language="en"
    )[0]["reasons"] == ["lost deterministic selection tie-break"]

    assert not utils.artwork_candidate_acceptable(config, {"file_path": "/x", "width": "bad"})
    assert not utils.artwork_candidate_acceptable(
        config,
        _candidate(
            "/blank.jpg",
            analysis={"width": 1200, "height": 1800, "blank": True},
        ),
    )
    assert utils.artwork_candidate_acceptable(
        config,
        _candidate(
            "/valid.jpg",
            width=1,
            height=1,
            analysis={"width": 1200, "height": 1800, "blank": False},
        ),
    )


def test_asset_policy_and_stale_time_edge_paths(tmp_path):
    asset = tmp_path / "poster.jpg"
    asset.write_bytes(b"owned")
    assert utils.asset_write_allowed(
        {"assets": {"update_policy": "managed"}},
        "movie",
        asset,
        "poster",
        cached_entry="invalid",
    ) == (False, "no_ownership_record")
    assert utils.stale_image("now", days="invalid") is True
    naive = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()
    assert utils.stale_image(naive, days=30) is False


def test_movie_upgrade_remaining_decisions(monkeypatch, tmp_path):
    config = _config()
    asset = tmp_path / "poster.jpg"
    candidate = tmp_path / "candidate.jpg"
    asset.write_bytes(_encoded("red", (16, 24)))
    candidate.write_bytes(_encoded("blue", (16, 24)))
    monkeypatch.setattr(utils, "stale_image", lambda *_args, **_kwargs: False)

    def decision(cached_votes, vote, width=16, height=24):
        return utils.smart_asset_upgrade(
            config,
            asset,
            {"width": width, "height": height, "vote_average": vote},
            new_image_path=candidate,
            cache_key="movie",
            cached_entry={"poster_average": cached_votes},
        )[0:2]

    assert decision(2, 5) == (True, "UPGRADE_RELAXED")
    assert decision(7, 8) == (True, "UPGRADE_VOTES")

    monkeypatch.setattr(utils, "_md5_file", lambda _path: (_ for _ in ()).throw(OSError("bad")))
    assert decision(2, 5) == (False, "ERROR_IMAGE_COMPARE")


def test_upgrade_quality_guard_uses_confidence_dimensions_and_content(
    monkeypatch, tmp_path
):
    config = _config()
    asset = tmp_path / "poster.jpg"
    candidate = tmp_path / "candidate.jpg"
    asset.write_bytes(_encoded("red", (100, 150)))
    candidate.write_bytes(_encoded("blue", (100, 150)))
    monkeypatch.setattr(utils, "stale_image", lambda *_args, **_kwargs: False)

    lower_average = utils.smart_asset_upgrade(
        config,
        asset,
        {
            "width": 100,
            "height": 150,
            "vote_average": 8,
            "vote_count": 100,
        },
        new_image_path=candidate,
        cache_key="movie",
        cached_entry={"poster_average": 10, "poster_vote_count": 1},
    )
    assert lower_average[0:2] == (False, "QUALITY_GUARD_REJECTED")

    count_tiebreak = utils.smart_asset_upgrade(
        config,
        asset,
        {
            "width": 100,
            "height": 150,
            "vote_average": 8,
            "vote_count": 100,
        },
        new_image_path=candidate,
        cache_key="movie",
        cached_entry={"poster_average": 8, "poster_vote_count": 1},
    )
    assert count_tiebreak[0:2] == (True, "UPGRADE_VOTES")

    candidate.write_bytes(_encoded("green", (120, 120)))
    one_dimension_only = utils.smart_asset_upgrade(
        config,
        asset,
        {
            "width": 120,
            "height": 120,
            "vote_average": 8,
            "vote_count": 100,
        },
        new_image_path=candidate,
        cache_key="movie",
        cached_entry={"poster_average": 8, "poster_vote_count": 100},
    )
    assert one_dimension_only[0:2] == (False, "QUALITY_GUARD_REJECTED")

    candidate.write_bytes(_encoded("blue", (100, 150)))
    content_upgrade = utils.smart_asset_upgrade(
        config,
        asset,
        {
            "width": 100,
            "height": 150,
            "vote_average": 8,
            "vote_count": 100,
            "content_analysis": {
                "width": 100,
                "height": 150,
                "blank": False,
                "sharpness": 100,
            },
        },
        new_image_path=candidate,
        cache_key="movie",
        cached_entry={"poster_average": 8, "poster_vote_count": 100},
    )
    assert content_upgrade[0:2] == (True, "UPGRADE_QUALITY")


def test_season_upgrade_remaining_decisions(monkeypatch, tmp_path):
    config = _config()
    asset = tmp_path / "Season01.jpg"
    candidate = tmp_path / "candidate.jpg"
    candidate.write_bytes(_encoded("blue", (32, 48)))
    missing = utils.smart_season_asset_upgrade(
        config,
        asset,
        {"width": 32, "height": 48, "vote_average": 1},
        new_image_path=candidate,
    )
    assert missing[0:2] == (True, "NO_EXISTING_ASSET_SEASON")

    asset.write_bytes(_encoded("red", (16, 24)))
    monkeypatch.setattr(utils, "stale_image", lambda *_args, **_kwargs: False)

    def decision(cached_votes, vote, width=16, height=24, image_path=candidate):
        return utils.smart_season_asset_upgrade(
            config,
            asset,
            {"width": width, "height": height, "vote_average": vote},
            new_image_path=image_path,
            cache_key="show",
            season_number=1,
            cached_entry={"seasons": {"1": {"season_average": cached_votes}}},
        )[0:2]

    assert decision(0, 0, width=32, height=48) == (
        True,
        "UPGRADE_ZERO_VOTE_SEASON",
    )
    assert decision(2, 4) == (True, "UPGRADE_RELAXED_SEASON")
    assert decision(5, 5, width=32, height=48) == (True, "UPGRADE_DIMENSIONS_SEASON")
    assert decision(5, 1) == (False, "QUALITY_GUARD_REJECTED_SEASON")
    assert decision(5, 1, image_path=None) == (False, "NO_IMAGE_FOR_COMPARE_SEASON")

    monkeypatch.setattr(utils, "stale_image", lambda *_args, **_kwargs: True)
    assert decision(2, 3, width=32, height=48) == (True, "FORCE_UPGRADE_STALE_SEASON")
    assert decision(5, 1, width=8, height=12) == (
        False,
        "STALE_CANDIDATE_DOWNGRADE_SEASON",
    )

    monkeypatch.setattr(utils, "_md5_file", lambda _path: (_ for _ in ()).throw(OSError("bad")))
    assert decision(2, 3) == (False, "ERROR_IMAGE_COMPARE_SEASON")

    monkeypatch.setattr(utils, "_md5_file", lambda path: str(path))
    asset.write_bytes(b"not-an-image")
    assert decision(2, 3) == (False, "ERROR_IMAGE_COMPARE_SEASON")


def test_provider_inference_paths_and_asset_path_failures(monkeypatch, tmp_path):
    calls = []

    async def external(_config, source, *, provider, **_kwargs):
        calls.append((source, provider))
        return None, 404, "missing"

    monkeypatch.setattr(utils, "_download_external_artwork", external)
    import asyncio

    assert asyncio.run(
        utils.download_poster({}, "https://fanart.tv/a.jpg", tmp_path / "a.jpg", session=object())
    )[1] == 404
    assert asyncio.run(
        utils.download_poster({}, "https://plex:32400/a", tmp_path / "b.jpg", session=object())
    )[1] == 404
    assert calls == [
        ("https://fanart.tv/a.jpg", "fanart"),
        ("https://plex:32400/a", "plex"),
    ]

    movie_dir = tmp_path / "not-a-directory"
    movie_dir.write_text("x", encoding="utf-8")
    config = {"settings": {"mode": "plex"}}
    assert utils.get_asset_path(
        config, {"library_type": "movie", "movie_dir": str(movie_dir)}, "poster"
    ) is None
    season_dir = tmp_path / "Season 1"
    season_dir.mkdir()
    meta = {"library_type": "tv", "season_dirs": {"1": str(season_dir)}}
    assert utils.get_asset_path(config, meta, "season", season_number=1) == (
        season_dir / "Season01.jpg"
    )
    assert utils.get_asset_path(config, {"library_type": "tv"}, "season", season_number=1) is None

    dry = {"settings": {"mode": "plex", "dry_run": True}}
    assert utils.asset_temp_path(dry, {"library_type": "movie"}).parent.name == "metafusion-artwork"
    movie = tmp_path / "movie"
    movie.mkdir()
    assert utils.asset_temp_path(
        config, {"library_type": "movie", "movie_dir": str(movie)}
    ).parent == movie
