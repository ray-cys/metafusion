import asyncio
import io
import json
import logging
import os
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from PIL import Image, ImageDraw

from extensions.formula1 import commons as commons_module
from extensions.formula1 import race_background as race_background_module
from extensions.formula1 import show_artwork as show_artwork_module
from extensions.formula1.commons import (
    CommonsCandidate,
    ConstructorData,
    _commons_json,
    _commons_search_url,
    _constructor_aliases,
    _download_bytes,
    _licence_allowed,
    _normalize,
    _plain_text,
    _validate_image,
    acquire_candidate_image,
    load_constructors,
    parse_commons_candidates,
    parse_constructor_payload,
    search_commons,
)
from extensions.formula1.config import Formula1ConfigError, load_formula1_config
from extensions.formula1.inventory import Formula1Episode, Formula1Show
from extensions.formula1.metadata import build_show_entry
from extensions.formula1.provider import RaceData
from extensions.formula1.race_background import (
    RaceBackgroundCandidate,
    _candidate_rejection_reason,
    _category_pages,
    _category_priority,
    _race_background_category_url,
    _race_background_search_url,
    _race_queries,
    _solar_elevation,
    classify_image_environment,
    derive_race_environment,
    environment_compatible,
    image_has_meaningful_colour,
    parse_race_background_candidates,
    search_race_backgrounds,
)
from extensions.formula1.sessions import session_date
from extensions.formula1.show_artwork import (
    EPISODE_RENDERER_VERSION,
    SHOW_RENDERER_VERSION,
    _adaptive_showcase_crop,
    _asset_reference,
    _background_candidate_order,
    _candidate_order,
    _checksum,
    _episode_fingerprint,
    _episode_reference,
    _existing_episode_outputs,
    _grade_photo,
    _managed_episode_action,
    _pair_integrity,
    _poster_photo_profile,
    _poster_showcase_grade,
    _prune_retained_pairs,
    _prune_source_cache,
    _rounded_flag_badge,
    _saliency_focal_point,
    _select_background_source,
    reconcile_episode_posters,
    reconcile_episode_round_artwork,
    render_episode_poster,
    render_show_background,
    render_show_poster,
    run_show_artwork_rotation,
)
from extensions.formula1.state import Formula1State


def _photo_bytes(size=(1600, 900), *, blank=False):
    image = Image.new("RGB", size, (120, 120, 120) if blank else (10, 20, 30))
    if not blank:
        draw = ImageDraw.Draw(image)
        for x in range(0, size[0], 24):
            draw.line((x, 0, size[0] - x, size[1]), fill=(230, 25, 45), width=8)
        draw.rectangle((250, 300, 1350, 650), fill=(245, 245, 245), outline=(0, 0, 0), width=12)
    stream = io.BytesIO()
    image.save(stream, "JPEG", quality=92)
    return stream.getvalue()


def _constructor_payload():
    return {
        "MRData": {
            "ConstructorTable": {
                "Constructors": [
                    {"constructorId": "alpine", "name": "Alpine F1 Team"},
                    {"constructorId": "ferrari", "name": "Ferrari"},
                    {"constructorId": "mclaren", "name": "McLaren"},
                ]
            }
        }
    }


def _commons_payload(team="Alpine F1 Team", constructor_id="alpine", **overrides):
    title_team = overrides.pop("title_team", team.replace(" F1 Team", ""))
    info = {
        "url": f"https://upload.wikimedia.org/source-{constructor_id}.jpg",
        "thumburl": f"https://upload.wikimedia.org/thumb-{constructor_id}.jpg",
        "width": overrides.pop("width", 3200),
        "height": overrides.pop("height", 1800),
        "mime": overrides.pop("mime", "image/jpeg"),
        "sha1": f"sha1-{constructor_id}",
        "extmetadata": {
            "ImageDescription": {"value": f"<p>{team} at the 2026 Grand Prix</p>"},
            "Artist": {"value": overrides.pop("author", "<a>Liauzh</a>")},
            "LicenseShortName": {"value": overrides.pop("licence", "CC BY 4.0")},
            "LicenseUrl": {
                "value": overrides.pop(
                    "licence_url", "https://creativecommons.org/licenses/by/4.0/"
                )
            },
        },
    }
    info.update(overrides)
    return {
        "query": {
            "pages": [
                {
                    "pageid": 100 + len(constructor_id),
                    "title": f"File:2026 Australian GP - {title_team} - Qualifying.jpg",
                    "categories": [{"title": "Category:2026 Formula One cars"}],
                    "imageinfo": [info],
                }
            ]
        }
    }


def _race_background_payload(*, page_id=901, title=None, **overrides):
    info = {
        "url": f"https://upload.wikimedia.org/race-{page_id}.jpg",
        "thumburl": f"https://upload.wikimedia.org/race-thumb-{page_id}.jpg",
        "width": overrides.pop("width", 3200),
        "height": overrides.pop("height", 1800),
        "mime": overrides.pop("mime", "image/jpeg"),
        "sha1": overrides.pop("sha1", f"race-sha1-{page_id}"),
        "extmetadata": {
            "ImageDescription": {
                "value": overrides.pop(
                    "description",
                    "McLaren Formula 1 race car on track at the 2026 "
                    "Australian Grand Prix at Albert Park Grand Prix Circuit",
                )
            },
            "Artist": {"value": overrides.pop("author", "Race Photographer")},
            "LicenseShortName": {"value": overrides.pop("licence", "CC BY 4.0")},
            "LicenseUrl": {
                "value": overrides.pop(
                    "licence_url", "https://creativecommons.org/licenses/by/4.0/"
                )
            },
        },
    }
    info.update(overrides)
    return {
        "query": {
            "pages": [
                {
                    "pageid": page_id,
                    "title": title
                    or f"File:2026 McLaren Formula 1 race car {page_id}.jpg",
                    "categories": [{"title": "Category:2026 Formula One cars"}],
                    "imageinfo": [info],
                }
            ]
        }
    }


class Response:
    def __init__(self, *, status=200, payload=None, data=b"", headers=None):
        self.status = status
        self.payload = payload
        self.data = data
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self, **_kwargs):
        return self.payload

    async def read(self):
        return self.data


class CommonsSession:
    def __init__(self, *, fail=False, candidates=True, image=None):
        self.fail = fail
        self.candidates = candidates
        self.image = image or _photo_bytes()
        self.urls = []

    def get(self, url, **_kwargs):
        self.urls.append(url)
        if self.fail:
            return Response(status=503)
        if url.endswith("constructors.json"):
            return Response(payload=_constructor_payload())
        hostname = urlparse(url).hostname
        if hostname == "commons.wikimedia.org":
            parameters = parse_qs(urlparse(url).query)
            query = parameters.get("gsrsearch", [""])[0].casefold()
            if not self.candidates:
                return Response(payload={"query": {"pages": []}})
            if (
                parameters.get("generator") == ["categorymembers"]
                or "race car" in query
                or "formula one cars" in query
            ):
                page_id = (
                    901
                    if parameters.get("generator") == ["categorymembers"]
                    else 902
                )
                return Response(payload=_race_background_payload(page_id=page_id))
            for constructor_id, team in (
                ("alpine", "Alpine F1 Team"),
                ("ferrari", "Ferrari"),
                ("mclaren", "McLaren"),
            ):
                if constructor_id in query:
                    return Response(payload=_commons_payload(team, constructor_id))
            return Response(payload={"query": {"pages": []}})
        if hostname == "upload.wikimedia.org":
            return Response(data=self.image, headers={"Content-Length": str(len(self.image))})
        return Response(status=404)


@pytest.fixture
def core(tmp_path):
    return {
        "settings": {"mode": "kometa", "path": str(tmp_path / "kometa"), "dry_run": False},
        "runtime": {"plex_retries": 1},
    }


@pytest.fixture(autouse=True)
def no_commons_delay(monkeypatch):
    monkeypatch.setattr(commons_module, "COMMONS_REQUEST_INTERVAL_SECONDS", 0)


@pytest.fixture
def config(tmp_path, core):
    value = load_formula1_config(core, tmp_path / "config")
    value["show_artwork"].update(
        poster_width=600,
        poster_height=900,
        background_width=1280,
        background_height=720,
        episode_width=1280,
        episode_height=720,
        minimum_source_width=800,
        minimum_source_height=450,
        minimum_background_source_width=800,
        minimum_background_source_height=450,
        fallback_background_source_width=800,
        fallback_background_source_height=450,
    )
    return value


@pytest.fixture
def race():
    return RaceData(
        2026,
        1,
        "Australian Grand Prix",
        "albert_park",
        "Albert Park Grand Prix Circuit",
        "Melbourne",
        "Australia",
        "2026-03-08",
        None,
        -37.8,
        144.9,
    )


@pytest.fixture
def show(tmp_path):
    episode = Formula1Episode(
        2026,
        1,
        1,
        "Australian Grand Prix",
        "Race Session",
        "race",
        tmp_path / "race.mkv",
        "episode-1",
        "current",
    )
    return Formula1Show(2026, "F1 2026", "show-2026", [episode])


def test_commons_identity_licence_and_payload_filtering(config):
    roster = parse_constructor_payload(_constructor_payload())
    assert [item.constructor_id for item in roster] == ["alpine", "ferrari", "mclaren"]
    assert parse_constructor_payload({}) == []
    invalid_roster = _constructor_payload()
    invalid_roster["MRData"]["ConstructorTable"]["Constructors"].append(
        {"constructorId": "", "name": "Missing"}
    )
    assert len(parse_constructor_payload(invalid_roster)) == 3
    assert "racing bulls" in _constructor_aliases(ConstructorData("rb", "RB F1 Team"))
    assert _normalize("McLarén Racing") == "mclaren racing"
    assert _plain_text("<b>One &amp; Two</b>") == "One & Two"
    assert _licence_allowed("CC BY 4.0")
    assert _licence_allowed("Public domain")
    assert not _licence_allowed("CC BY-SA 4.0")
    candidates = parse_commons_candidates(
        _commons_payload(), 2026, roster[0], roster, config
    )
    assert len(candidates) == 1
    assert candidates[0].author == "Liauzh"
    assert CommonsCandidate.from_dict(candidates[0].as_dict()) == candidates[0]
    assert parse_commons_candidates(
        _commons_payload(licence="CC BY-SA 4.0"), 2026, roster[0], roster, config
    ) == []
    assert parse_commons_candidates(
        _commons_payload(author=""), 2026, roster[0], roster, config
    ) == []
    assert parse_commons_candidates(
        _commons_payload(licence_url=""), 2026, roster[0], roster, config
    ) == []
    assert parse_commons_candidates(
        _commons_payload(title_team="Ferrari"), 2026, roster[0], roster, config
    ) == []
    assert parse_commons_candidates(
        _commons_payload(title_team="Alpine Ferrari"), 2026, roster[0], roster, config
    ) == []
    skiing = _commons_payload()
    skiing["query"]["pages"][0]["title"] = "File:Alpine skiing at the 2026 Olympics.jpg"
    assert parse_commons_candidates(skiing, 2026, roster[0], roster, config) == []
    unsafe_url = _commons_payload()
    unsafe_url["query"]["pages"][0]["imageinfo"][0]["thumburl"] = "https://example.com/car.jpg"
    unsafe_url["query"]["pages"][0]["imageinfo"][0]["url"] = ""
    assert parse_commons_candidates(unsafe_url, 2026, roster[0], roster, config) == []
    no_page = _commons_payload()
    no_page["query"]["pages"][0]["pageid"] = 0
    assert parse_commons_candidates(no_page, 2026, roster[0], roster, config) == []
    assert parse_commons_candidates(
        _commons_payload(width=640), 2026, roster[0], roster, config
    ) == []
    dictionary_pages = _commons_payload()
    dictionary_pages["query"]["pages"] = {"1": dictionary_pages["query"]["pages"][0]}
    assert parse_commons_candidates(dictionary_pages, 2026, roster[0], roster, config)
    assert parse_commons_candidates({"query": {"pages": "bad"}}, 2026, roster[0], roster, config) == []


def test_race_background_identity_licence_and_season_filtering(config):
    candidates = parse_race_background_candidates(_race_background_payload(), 2026, config)
    assert len(candidates) == 1
    assert candidates[0].vehicle_name.startswith("2026 McLaren Formula 1 race car")
    assert RaceBackgroundCandidate.from_dict(candidates[0].as_dict()) == candidates[0]
    assert parse_race_background_candidates(_race_background_payload(), 2027, config) == []
    assert parse_race_background_candidates(
        _race_background_payload(title="File:2026 Formula 1 Medical Car.jpg"), 2026, config
    ) == []
    assert parse_race_background_candidates(
        _race_background_payload(
            title="File:Aston Martin Formula 1 race car - Museum 2026.jpg"
        ),
        2026,
        config,
    ) == []
    assert parse_race_background_candidates(
        _race_background_payload(
            title="File:2026 Formula 1 Safety Car.jpg",
            description="Formula 1 safety car at the 2026 Australian Grand Prix",
        ),
        2026,
        config,
    ) == []
    showcar = _race_background_payload(
        title="File:2026 Red Bull Formula 1 race car.jpg",
        description=(
            "Red Bull Formula 1 race car static display at the 2026 Australian Grand Prix"
        ),
    )
    showcar["query"]["pages"][0]["categories"] = [
        {"title": "Category:2026 Singapore Grand Prix"},
        {"title": "Category:Red Bull Formula One showcars"},
    ]
    assert parse_race_background_candidates(showcar, 2026, config) == []
    plural = _race_background_payload(
        title="File:F1 season vehicle at Suzuka 2026.jpg",
        description="F1 vehicle on track at the 2026 Japanese Grand Prix",
    )
    plural["query"]["pages"][0]["categories"] = [
        {"title": "Category:2026 Formula One cars"},
        {"title": "Category:2026 Japanese Grand Prix"},
    ]
    assert parse_race_background_candidates(plural, 2026, config)
    localized_formula = _race_background_payload(
        title="File:2026 Motorsport Formel 1 Großer Preis der Niederlande.jpg",
        description="Motorsport, Formel 1, on track during the 2026 Dutch Grand Prix race",
    )
    localized_formula["query"]["pages"][0]["categories"] = [
        {"title": "Category:Formula One cars at the 2026 Dutch Grand Prix"}
    ]
    assert parse_race_background_candidates(localized_formula, 2026, config)
    assert parse_race_background_candidates(
        _race_background_payload(licence="CC BY-SA 4.0"), 2026, config
    ) == []
    assert parse_race_background_candidates(
        _race_background_payload(width=640), 2026, config
    ) == []
    unsafe_url = _race_background_payload()
    unsafe_url["query"]["pages"][0]["imageinfo"][0]["thumburl"] = (
        "https://example.com/race-car.jpg"
    )
    unsafe_url["query"]["pages"][0]["imageinfo"][0]["url"] = ""
    assert parse_race_background_candidates(unsafe_url, 2026, config) == []
    assert "race+car" in _race_background_search_url(
        config, 'intitle:2026 "Formula 1" "race car"'
    )
    assert "iiurlwidth=3840" in _race_background_search_url(config, "F1")
    assert "gsroffset=30" in _race_background_search_url(config, "F1", offset=30)
    assert "generator=categorymembers" in _race_background_category_url(config)
    assert "gcmcontinue=next" in _race_background_category_url(
        config, continuation="next"
    )


def test_race_background_search_cache_and_stale_fallback(config):
    state = Formula1State(config["paths"]["database"])
    session = CommonsSession()
    candidates, source = asyncio.run(
        search_race_backgrounds(session, state, config, 2026, logging.getLogger("safety"))
    )
    assert candidates and source == "wikimedia-commons-race-background"
    assert asyncio.run(
        search_race_backgrounds(session, state, config, 2026, logging.getLogger("safety"))
    )[1] == "cache"
    state.connection.execute(
        "UPDATE provider_cache SET expires_at='2020-01-01T00:00:00+00:00'"
    )
    state.connection.commit()
    stale, source = asyncio.run(
        search_race_backgrounds(
            CommonsSession(fail=True),
            state,
            config,
            2026,
            logging.getLogger("safety-stale"),
        )
    )
    assert stale and source == "stale-cache"
    state.connection.execute("DELETE FROM provider_cache")
    state.connection.commit()
    with pytest.raises(RuntimeError, match="Commons API request failed"):
        asyncio.run(
            search_race_backgrounds(
                CommonsSession(fail=True),
                state,
                config,
                2026,
                logging.getLogger("safety-failed"),
            )
        )
    state.close()


def test_race_background_selection_rejects_empty_and_invalid_sources(
    config, monkeypatch
):
    state = Formula1State(config["paths"]["database"])
    assert _background_candidate_order([], None, 1) == []

    async def empty_search(*_args, **_kwargs):
        return [], "test"

    monkeypatch.setattr(show_artwork_module, "search_race_backgrounds", empty_search)
    with pytest.raises(RuntimeError, match="colour Formula 1 race-action"):
        asyncio.run(
            _select_background_source(
                None, state, config, RaceData(
                    2026, 1, "Australian Grand Prix", "albert_park",
                    "Albert Park Grand Prix Circuit", "Melbourne", "Australia",
                    "2026-03-08", None, -37.8, 144.9,
                ), None, 1, logging.getLogger("empty")
            )
        )

    candidate = parse_race_background_candidates(_race_background_payload(), 2026, config)[0]

    async def candidate_search(*_args, **_kwargs):
        return [candidate], "test"

    async def invalid_image(*_args, **_kwargs):
        raise RuntimeError("invalid race image")

    monkeypatch.setattr(show_artwork_module, "search_race_backgrounds", candidate_search)
    monkeypatch.setattr(show_artwork_module, "acquire_candidate_image", invalid_image)
    with pytest.raises(RuntimeError, match="invalid race image"):
        asyncio.run(
            _select_background_source(
                None, state, config, RaceData(
                    2026, 1, "Australian Grand Prix", "albert_park",
                    "Albert Park Grand Prix Circuit", "Melbourne", "Australia",
                    "2026-03-08", None, -37.8, 144.9,
                ), None, 1, logging.getLogger("invalid")
            )
        )

    night_race = RaceData(
        2027, 18, "Singapore Grand Prix", "marina_bay",
        "Marina Bay Street Circuit", "Marina Bay", "Singapore",
        "2027-10-03", None, 1.2914, 103.864,
        circuit_profile="floodlit urban street circuit",
        race_time_utc="12:00:00Z",
    )
    night_candidate = parse_race_background_candidates(
        _race_background_payload(
            title="File:2027 Singapore Grand Prix McLaren Formula 1 race car at night.jpg",
            description=(
                "McLaren Formula 1 race car on track at the floodlit 2027 Singapore "
                "Grand Prix on the Marina Bay Street Circuit"
            ),
        ),
        night_race,
        config,
    )[0]
    monochrome = config["paths"]["show_image_cache"] / "monochrome.jpg"
    monochrome.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1600, 900), (35, 35, 35)).save(monochrome)
    bright = config["paths"]["show_image_cache"] / "daytime.jpg"
    bright.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1600, 900), (220, 205, 185)).save(bright)

    async def night_search(*_args, **_kwargs):
        return [night_candidate], "test"

    async def bright_image(*_args, **_kwargs):
        return bright, "test"

    async def monochrome_image(*_args, **_kwargs):
        return monochrome, "test"

    monkeypatch.setattr(show_artwork_module, "search_race_backgrounds", night_search)
    monkeypatch.setattr(
        show_artwork_module, "acquire_candidate_image", monochrome_image
    )
    with pytest.raises(RuntimeError, match="monochrome image rejected"):
        asyncio.run(
            _select_background_source(
                None,
                state,
                config,
                night_race,
                None,
                18,
                logging.getLogger("monochrome"),
            )
        )
    monkeypatch.setattr(show_artwork_module, "acquire_candidate_image", bright_image)
    with pytest.raises(RuntimeError, match="expected night scene"):
        asyncio.run(
            _select_background_source(
                None, state, config, night_race, None, 18, logging.getLogger("mismatch")
            )
        )
    state.close()


def test_background_uses_colour_4k_team_car_as_last_resort(
    config, race, monkeypatch
):
    state = Formula1State(config["paths"]["database"])
    roster = parse_constructor_payload(_constructor_payload())
    team_candidate = parse_commons_candidates(
        _commons_payload(width=3840, height=2160),
        2026,
        roster[0],
        roster,
        config,
    )[0]
    source = config["paths"]["show_image_cache"] / "fallback-4k.jpg"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(_photo_bytes((3840, 2160)))

    async def empty_search(*_args, **_kwargs):
        return [], "test"

    async def fallback_image(*_args, **_kwargs):
        return source, "cache"

    monkeypatch.setattr(show_artwork_module, "search_race_backgrounds", empty_search)
    monkeypatch.setattr(show_artwork_module, "acquire_candidate_image", fallback_image)
    selected, path, providers, _image_hash = asyncio.run(
        _select_background_source(
            None,
            state,
            config,
            race,
            None,
            1,
            logging.getLogger("4k-fallback"),
            team_candidate,
        )
    )
    assert selected.match_tier == "current_season_team_car_fallback"
    assert "4k-source" in selected.evidence
    assert path == source
    assert providers["search"] == "show-poster-source"

    async def failed_search(*_args, **_kwargs):
        raise RuntimeError("Commons temporarily unavailable")

    monkeypatch.setattr(show_artwork_module, "search_race_backgrounds", failed_search)
    recovered = asyncio.run(
        _select_background_source(
            None,
            state,
            config,
            race,
            None,
            1,
            logging.getLogger("fallback-after-search-failure"),
            team_candidate,
        )
    )
    assert recovered[0].match_tier == "current_season_team_car_fallback"

    monochrome = config["paths"]["show_image_cache"] / "fallback-monochrome.jpg"
    Image.new("RGB", (1600, 900), (45, 45, 45)).save(monochrome)

    async def monochrome_image(*_args, **_kwargs):
        return monochrome, "cache"

    monkeypatch.setattr(show_artwork_module, "acquire_candidate_image", monochrome_image)
    with pytest.raises(RuntimeError, match="fallback was monochrome"):
        asyncio.run(
            _select_background_source(
                None,
                state,
                config,
                race,
                None,
                1,
                logging.getLogger("monochrome-fallback"),
                team_candidate,
            )
        )

    async def missing_image(*_args, **_kwargs):
        return config["paths"]["show_image_cache"] / "does-not-exist.jpg", "test"

    monkeypatch.setattr(show_artwork_module, "acquire_candidate_image", missing_image)
    with pytest.raises(RuntimeError, match="not cached after validation"):
        asyncio.run(
            _select_background_source(
                None,
                state,
                config,
                race,
                None,
                1,
                logging.getLogger("missing-fallback"),
                team_candidate,
            )
        )

    monkeypatch.setattr(show_artwork_module, "acquire_candidate_image", fallback_image)
    config["show_artwork"]["fallback_background_source_width"] = 4096
    with pytest.raises(RuntimeError, match="minimum fallback source floor"):
        asyncio.run(
            _select_background_source(
                None,
                state,
                config,
                race,
                None,
                1,
                logging.getLogger("undersized-fallback"),
                team_candidate,
            )
        )
    state.close()


def test_rotation_persists_restores_and_reports_team_car_background_fallback(
    config, show, race, monkeypatch
):
    state = Formula1State(config["paths"]["database"])

    async def empty_background_search(*_args, **_kwargs):
        return [], "test"

    monkeypatch.setattr(
        show_artwork_module, "search_race_backgrounds", empty_background_search
    )
    session = CommonsSession()
    rotated = asyncio.run(
        run_show_artwork_rotation(
            session,
            state,
            config,
            show,
            race,
            "M0 0 L10 10",
            logging.getLogger("fallback-rotation"),
        )
    )
    assert rotated.action == "rotated"
    current = state.show_rotation("show:2026")
    assert (
        current["source"]["background_candidate"]["match_tier"]
        == "current_season_team_car_fallback"
    )
    background = Path(current["background_destination"])
    assert background.is_file()
    background.unlink()
    restored = asyncio.run(
        run_show_artwork_rotation(
            session,
            state,
            config,
            show,
            race,
            None,
            logging.getLogger("fallback-restore"),
        )
    )
    assert restored.action == "restored"
    assert background.is_file()

    background.unlink()
    cached_source = Path(current["source"]["background_photo_cache"])
    grayscale = Image.new("RGB", (1600, 900), (55, 55, 55))
    grayscale_draw = ImageDraw.Draw(grayscale)
    grayscale_draw.line((0, 0, 1599, 899), fill=(235, 235, 235), width=18)
    grayscale_draw.rectangle((300, 250, 1350, 700), outline=(5, 5, 5), width=20)
    grayscale.save(cached_source)
    monochrome = asyncio.run(
        run_show_artwork_rotation(
            session,
            state,
            config,
            show,
            race,
            None,
            logging.getLogger("saved-monochrome-rejected"),
        )
    )
    assert monochrome.action == "repair-failed"
    assert "saved background source was monochrome" in monochrome.issue

    cached_source.write_bytes(_photo_bytes())

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("provider offline")

    monkeypatch.setattr(show_artwork_module, "acquire_candidate_image", unavailable)
    failed = asyncio.run(
        run_show_artwork_rotation(
            session,
            state,
            config,
            show,
            race,
            None,
            logging.getLogger("fallback-repair-failed"),
        )
    )
    assert failed.action == "repair-failed"
    assert "provider offline" in failed.issue
    state.close()


def test_race_environment_and_exact_circuit_candidate_ranking(tmp_path, config):
    singapore = RaceData(
        2027, 18, "Singapore Grand Prix", "marina_bay",
        "Marina Bay Street Circuit", "Marina Bay", "Singapore",
        "2027-10-03", None, 1.2914, 103.864,
        circuit_profile="A floodlit urban street circuit beside the harbour.",
        race_time_utc="12:00:00Z",
    )
    environment = derive_race_environment(singapore)
    assert environment.mode == "night"
    assert "floodlit" in environment.scene_terms
    assert _solar_elevation(replace(singapore, latitude=None)) is None
    assert _solar_elevation(replace(singapore, race_time_utc="not-a-time")) is None

    exact = _race_background_payload(
        title="File:2027 Singapore Grand Prix McLaren Formula 1 race car at night.jpg",
        description=(
            "McLaren Formula 1 race car at the floodlit 2027 Singapore Grand Prix "
            "on track at the Marina Bay Street Circuit"
        ),
    )
    candidates = parse_race_background_candidates(exact, singapore, config)
    assert candidates[0].match_tier == "exact_event_action_race_car"
    assert candidates[0].race_key == environment.race_key
    assert "environment" in candidates[0].evidence

    unrelated = _race_background_payload(
        title="File:2027 British Grand Prix McLaren Formula 1 race car.jpg",
        description="McLaren Formula 1 race car at Silverstone during the 2027 British Grand Prix",
    )
    assert parse_race_background_candidates(unrelated, singapore, config) == []

    aerial_track = _race_background_payload(
        title="File:2027 Singapore Grand Prix aerial circuit view.jpg",
        description=(
            "Aerial view of the empty Formula 1 circuit at the 2027 Singapore Grand Prix"
        ),
    )
    aerial_track["query"]["pages"][0]["categories"] = [
        {"title": "Category:2027 Singapore Grand Prix"}
    ]
    assert parse_race_background_candidates(aerial_track, singapore, config) == []

    historical_atmosphere = _race_background_payload(
        page_id=903,
        title="File:2018 Marina Bay Street Circuit at night.jpg",
        description="Floodlit motorsport circuit atmosphere in Singapore",
    )
    historical_atmosphere["query"]["pages"][0]["categories"] = [
        {"title": "Category:Marina Bay Street Circuit"}
    ]
    assert parse_race_background_candidates(
        historical_atmosphere, singapore, config
    ) == []

    historical_car = _race_background_payload(
        page_id=907,
        title="File:2014 McLaren Formula 1 race car at Marina Bay.jpg",
        description=(
            "McLaren Formula 1 race car on track during the race at "
            "Marina Bay Street Circuit"
        ),
    )
    historical_car["query"]["pages"][0]["categories"] = [
        {"title": "Category:Marina Bay Street Circuit"}
    ]
    candidates = parse_race_background_candidates(historical_car, singapore, config)
    assert candidates[0].match_tier == "historical_circuit_action_race_car"
    assert "historical-exact-circuit-race-car" in candidates[0].evidence

    commons_named_car = _race_background_payload(
        page_id=909,
        title="File:2012 Singapore GP - Ferrari.jpg",
        description="Singapore GP Final Race 1",
    )
    commons_page = commons_named_car["query"]["pages"][0]
    commons_page["categories"] = [
        {"title": "Category:2012 Singapore Grand Prix"},
        {"title": "Category:Ferrari F2012 of Felipe Massa"},
    ]
    commons_page["_metafusion_category_context"] = [
        "Category:Marina Bay Street Circuit",
        "Category:2012 Singapore Grand Prix",
    ]
    candidates = parse_race_background_candidates(
        commons_named_car, singapore, config
    )
    assert candidates[0].match_tier == "historical_circuit_action_race_car"
    assert "formula-one-event-category" in candidates[0].evidence
    assert "formula-one-chassis-category" in candidates[0].evidence
    assert "active-race" in candidates[0].evidence

    exact_static = _race_background_payload(
        page_id=910,
        title="File:2027 McLaren Formula 1 race car at Marina Bay.jpg",
        description="McLaren Formula 1 race car at the Marina Bay Street Circuit",
    )
    exact_static_candidates = parse_race_background_candidates(
        exact_static, singapore, config
    )
    assert exact_static_candidates == []
    ordered = _background_candidate_order(
        candidates, None, singapore.round_number
    )
    assert ordered[0].match_tier == "historical_circuit_action_race_car"

    year_neutral_atmosphere = _race_background_payload(
        page_id=904,
        title="File:Marina Bay Street Circuit floodlights.jpg",
        description="Night view of the Marina Bay Street Circuit",
    )
    year_neutral_atmosphere["query"]["pages"][0]["categories"] = [
        {"title": "Category:Marina Bay Street Circuit"}
    ]
    assert parse_race_background_candidates(
        year_neutral_atmosphere, singapore, config
    ) == []

    event_only_historical = _race_background_payload(
        page_id=905,
        title="File:2018 Singapore Grand Prix at night.jpg",
        description="Formula 1 floodlit atmosphere at the Singapore Grand Prix",
    )
    event_only_historical["query"]["pages"][0]["categories"] = [
        {"title": "Category:2018 Singapore Grand Prix"}
    ]
    assert parse_race_background_candidates(
        event_only_historical, singapore, config
    ) == []

    future_atmosphere = _race_background_payload(
        page_id=906,
        title="File:2028 Marina Bay Street Circuit at night.jpg",
        description="Formula 1 floodlit atmosphere at Marina Bay Street Circuit",
    )
    assert parse_race_background_candidates(future_atmosphere, singapore, config) == []

    locality_atmosphere = _race_background_payload(
        page_id=908,
        title="File:Singapore motorsport atmosphere at night.jpg",
        description="Floodlit motorsport atmosphere at Marina Bay, Singapore",
    )
    locality_atmosphere["query"]["pages"][0]["categories"] = [
        {"title": "Category:Motorsport in Singapore"}
    ]
    assert parse_race_background_candidates(
        locality_atmosphere, singapore, config
    ) == []

    queries = _race_queries(singapore, environment)
    assert any(
        '"Marina Bay Street Circuit" F1 "track action"' in query
        for query in queries
    )
    assert not any("atmosphere" in query for query in queries)
    rejected_atmosphere = _race_background_payload(
        title="File:2027 Singapore Grand Prix driver portrait.jpg",
        description="Formula 1 driver portrait at the 2027 Singapore Grand Prix",
    )
    rejected_atmosphere["query"]["pages"][0]["categories"] = [
        {"title": "Category:2027 Singapore Grand Prix"}
    ]
    assert parse_race_background_candidates(rejected_atmosphere, singapore, config) == []

    dark = tmp_path / "night.jpg"
    twilight = tmp_path / "twilight.jpg"
    bright = tmp_path / "day.jpg"
    colour = tmp_path / "colour.jpg"
    Image.new("RGB", (1600, 900), (24, 30, 42)).save(dark)
    Image.new("RGB", (1600, 900), (110, 115, 120)).save(twilight)
    Image.new("RGB", (1600, 900), (205, 210, 215)).save(bright)
    colour_image = Image.new("RGB", (1600, 900), (42, 42, 42))
    ImageDraw.Draw(colour_image).rectangle((500, 250, 1400, 750), fill=(220, 25, 45))
    colour_image.save(colour)
    assert classify_image_environment(dark) == "night"
    assert classify_image_environment(twilight) == "twilight"
    assert classify_image_environment(bright) == "day"
    assert not image_has_meaningful_colour(bright)
    assert image_has_meaningful_colour(colour)
    assert environment_compatible("night", "night")
    assert not environment_compatible("night", "day")
    assert environment_compatible("day", "twilight")
    assert not environment_compatible("day", "night")


def test_race_background_category_traversal_preserves_circuit_evidence(config):
    race = RaceData(
        2027, 18, "Singapore Grand Prix", "marina_bay",
        "Marina Bay Street Circuit", "Marina Bay", "Singapore",
        "2027-10-03", None, 1.2914, 103.864,
        circuit_profile="A floodlit urban street circuit beside the harbour.",
        race_time_utc="12:00:00Z",
    )

    class CategorySession:
        def __init__(self):
            self.urls = []

        def get(self, url, **_kwargs):
            self.urls.append(url)
            parameters = parse_qs(urlparse(url).query)
            if parameters.get("generator") != ["categorymembers"]:
                return Response(payload={"query": {"pages": []}})
            category = parameters.get("gcmtitle", [""])[0]
            if category == "Category:Marina Bay Street Circuit":
                return Response(
                    payload={
                        "query": {
                            "pages": [
                                {
                                    "pageid": 1401,
                                    "ns": 14,
                                    "title": "Category:2014 at Marina Bay Street Circuit",
                                }
                            ]
                        }
                    }
                )
            if category == "Category:2014 at Marina Bay Street Circuit":
                payload = _race_background_payload(
                    page_id=1402,
                    title="File:2014 Formula 1 race car on track.jpg",
                    description="Formula 1 race car during a night race",
                )
                payload["query"]["pages"][0]["ns"] = 6
                payload["query"]["pages"][0]["categories"] = []
                return Response(payload=payload)
            return Response(payload={"query": {"pages": []}})

    session = CategorySession()
    pages = asyncio.run(_category_pages(session, config, race))
    assert len(pages) == 1
    assert pages[0]["categories"] == []
    assert pages[0]["_metafusion_category_context"] == [
        "Category:Marina Bay Street Circuit",
        "Category:2014 at Marina Bay Street Circuit",
    ]
    candidates = parse_race_background_candidates(
        {"query": {"pages": pages}}, race, config
    )
    assert candidates[0].match_tier == "historical_circuit_action_race_car"
    assert "commons-category" in candidates[0].evidence
    assert any("gcmnamespace=6%7C14" in url for url in session.urls)


def test_category_ancestry_does_not_turn_unrelated_people_into_atmosphere(config):
    race = RaceData(
        2027, 12, "Dutch Grand Prix", "zandvoort",
        "Circuit Zandvoort", "Zandvoort", "Netherlands",
        "2027-08-29", None, 52.3888, 4.5409,
    )
    payload = _race_background_payload(
        page_id=1501,
        title="File:Cyclist wearing Pelforth Sauvage Lejeune jersey.jpg",
        description="A person at a cycling event in Zandvoort",
    )
    page = payload["query"]["pages"][0]
    page["categories"] = [{"title": "Category:Cycling in the Netherlands"}]
    page["_metafusion_category_context"] = [
        "Category:Circuit Zandvoort",
        "Category:Events at Circuit Zandvoort",
    ]
    diagnostics = []
    assert parse_race_background_candidates(payload, race, config, diagnostics) == []
    assert diagnostics == [
        {
            "title": "File:Cyclist wearing Pelforth Sauvage Lejeune jersey.jpg",
            "reason": "event-circuit-location-mismatch",
        }
    ]


def test_old_background_candidate_records_are_marked_ineligible(config):
    candidate = parse_race_background_candidates(
        _race_background_payload(), 2026, config
    )[0]
    assert candidate.eligibility_version == 6
    legacy = candidate.as_dict()
    legacy.pop("eligibility_version")
    assert RaceBackgroundCandidate.from_dict(legacy).eligibility_version == 1


def test_race_background_diagnostics_explain_rejections(config):
    diagnostics = []
    assert parse_race_background_candidates(
        _race_background_payload(licence="CC BY-SA 4.0"),
        2026,
        config,
        diagnostics,
    ) == []
    assert diagnostics == [
        {
            "title": "File:2026 McLaren Formula 1 race car 901.jpg",
            "reason": "incompatible-or-unknown-licence",
        }
    ]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    (
        ({"mime": "text/html"}, "unsupported-media-type"),
        ({"page_id": 0}, "missing-page-identity"),
        ({"width": 1600, "height": 1600}, "incompatible-aspect-ratio"),
        ({"provider_identity": False}, "not-formula-one-race-action"),
        (
            {"identity": "black and white formula 1 race car on track"},
            "rejected-subject-or-series",
        ),
        ({"author": ""}, "missing-required-author"),
        ({"licence_url": "http://example.test/licence"}, "missing-required-licence-url"),
    ),
)
def test_race_background_rejection_reason_contract(overrides, expected):
    values = {
        "mime": "image/jpeg",
        "page_id": 1,
        "width": 3200,
        "height": 1800,
        "identity_allowed": True,
        "provider_identity": True,
        "identity": "formula 1 race car",
        "licence": "CC BY 4.0",
        "attribution_required": True,
        "author": "Photographer",
        "licence_url": "https://creativecommons.org/licenses/by/4.0/",
        "image_url": "https://upload.wikimedia.org/example.jpg",
        "minimum_width": 1600,
        "minimum_height": 900,
    }
    values.update(overrides)
    assert _candidate_rejection_reason(**values) == expected


def test_race_background_category_priority_and_rejected_seed(config, monkeypatch):
    assert _category_priority("Category:2027 Singapore Grand Prix", 2027) == 0
    assert _category_priority("Category:2028 Singapore Grand Prix", 2027) == 101
    assert _category_priority("Category:Singapore Grand Prix", 2027) == 60
    monkeypatch.setattr(
        race_background_module,
        "_race_category_seeds",
        lambda _race: ("Category:Formula 2",),
    )
    session = CommonsSession(candidates=False)
    race = RaceData(
        2027, 18, "Singapore Grand Prix", "marina_bay",
        "Marina Bay Street Circuit", "Marina Bay", "Singapore",
        "2027-10-03", None, 1.2914, 103.864,
    )
    assert asyncio.run(_category_pages(session, config, race)) == []
    assert session.urls == []


def test_race_background_search_logs_rejection_reasons(config, caplog):
    class RejectedSession(CommonsSession):
        def get(self, url, **_kwargs):
            self.urls.append(url)
            payload = _race_background_payload(licence="CC BY-SA 4.0")
            missing_page = _race_background_payload(page_id=0)["query"]["pages"][0]
            payload["query"]["pages"].append(missing_page)
            return Response(payload=payload)

    state = Formula1State(config["paths"]["database"])
    with caplog.at_level(logging.DEBUG):
        candidates, source = asyncio.run(
            search_race_backgrounds(
                RejectedSession(),
                state,
                config,
                2026,
                logging.getLogger("rejection-reasons"),
            )
        )
    assert candidates == []
    assert source == "wikimedia-commons-race-background"
    assert "incompatible-or-unknown-licence=1" in caplog.text
    assert "Wikimedia race-background rejected" in caplog.text
    state.close()
    assert _saliency_focal_point(Image.new("RGB", (1600, 900), (0, 0, 0))) == (
        0.62,
        0.5,
    )


def test_future_constructor_identity_requires_no_hard_coded_alias(config):
    constructor = ConstructorData("nova_racing", "Nova Racing Formula One Team")
    payload = _commons_payload(team="Nova Racing", constructor_id="nova_racing")
    page = payload["query"]["pages"][0]
    page["title"] = "File:2031 New Harbour GP - Nova Racing - Qualifying.jpg"
    page["categories"] = [{"title": "Category:2031 Formula One cars"}]
    page["imageinfo"][0]["extmetadata"]["ImageDescription"] = {
        "value": "Nova Racing at the 2031 New Harbour Grand Prix"
    }
    candidates = parse_commons_candidates(payload, 2031, constructor, [constructor], config)
    assert candidates and candidates[0].constructor_id == "nova_racing"


def test_commons_provider_cache_stale_and_url(config):
    state = Formula1State(config["paths"]["database"])
    session = CommonsSession()
    roster, source = asyncio.run(
        load_constructors(session, state, config, 2026, logging.getLogger("roster"))
    )
    assert source == "jolpica" and len(roster) == 3
    assert asyncio.run(
        load_constructors(session, state, config, 2026, logging.getLogger("roster"))
    )[1] == "cache"
    candidates, source = asyncio.run(
        search_commons(
            session, state, config, 2026, roster[0], roster, logging.getLogger("commons")
        )
    )
    assert candidates and source == "wikimedia-commons"
    assert asyncio.run(
        search_commons(
            session, state, config, 2026, roster[0], roster, logging.getLogger("commons")
        )
    )[1] == "cache"
    assert "gsrsearch" in _commons_search_url(config, 2026, roster[0])
    assert "iiurlwidth=3840" in _commons_search_url(config, 2026, roster[0])
    assert "intitle%3AGP" not in _commons_search_url(config, 2026, roster[0], broad=True)
    assert "gsroffset=30" in _commons_search_url(
        config, 2026, roster[0], broad=True, offset=30
    )
    state.connection.execute("UPDATE provider_cache SET expires_at='2020-01-01T00:00:00+00:00'")
    state.connection.commit()
    stale_roster, source = asyncio.run(
        load_constructors(CommonsSession(fail=True), state, config, 2026, logging.getLogger("stale"))
    )
    assert stale_roster and source == "stale-cache"
    stale_candidates, source = asyncio.run(
        search_commons(
            CommonsSession(fail=True),
            state,
            config,
            2026,
            roster[0],
            roster,
            logging.getLogger("stale"),
        )
    )
    assert stale_candidates and source == "stale-cache"
    state.connection.execute("DELETE FROM provider_cache")
    state.connection.commit()
    class EmptyRosterSession:
        def get(self, _url, **_kwargs):
            return Response(payload={})

    with pytest.raises(RuntimeError, match="no valid teams"):
        asyncio.run(
            load_constructors(
                EmptyRosterSession(), state, config, 2026, logging.getLogger("empty")
            )
        )
    with pytest.raises(RuntimeError):
        asyncio.run(
            load_constructors(
                CommonsSession(fail=True), state, config, 2026, logging.getLogger("failed")
            )
        )
    with pytest.raises(RuntimeError):
        asyncio.run(
            search_commons(
                CommonsSession(fail=True),
                state,
                config,
                2026,
                roster[0],
                roster,
                logging.getLogger("failed"),
            )
        )
    state.close()


def test_image_acquisition_validation_and_cache(config, monkeypatch):
    candidate = parse_commons_candidates(
        _commons_payload(),
        2026,
        ConstructorData("alpine", "Alpine F1 Team"),
        [ConstructorData("alpine", "Alpine F1 Team")],
        config,
    )[0]
    session = CommonsSession()
    destination, source = asyncio.run(acquire_candidate_image(session, config, candidate))
    assert destination.exists() and source == "wikimedia-commons"
    assert asyncio.run(acquire_candidate_image(session, config, candidate))[1] == "cache"
    no_hash_destination, _source = asyncio.run(
        acquire_candidate_image(session, config, replace(candidate, source_sha1=""))
    )
    assert len(no_hash_destination.stem) == 64
    destination.write_bytes(b"obsolete lower-resolution cache")
    refreshed, source = asyncio.run(acquire_candidate_image(session, config, candidate))
    assert refreshed == destination and source == "wikimedia-commons"
    with Image.open(refreshed) as refreshed_image:
        assert refreshed_image.size == (1600, 900)
    with pytest.raises(RuntimeError, match="blank"):
        _validate_image(_photo_bytes(blank=True), config)
    with pytest.raises(RuntimeError, match="dimensions"):
        _validate_image(_photo_bytes((400, 400)), config)
    with pytest.raises(RuntimeError, match="decoded"):
        _validate_image(b"not an image", config)
    with pytest.raises(RuntimeError, match="size safety"):
        _validate_image(b"", config)
    monkeypatch.setattr(
        "extensions.formula1.commons.ImageStat.Stat",
        lambda _image: type("LowSharpness", (), {"stddev": [0]})(),
    )
    with pytest.raises(RuntimeError, match="sharpness"):
        _validate_image(_photo_bytes(), config)
    monkeypatch.undo()
    class OversizedPixels:
        size = (10_000, 10_000)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def verify(self):
            return None

    monkeypatch.setattr(commons_module.Image, "open", lambda *_args, **_kwargs: OversizedPixels())
    with pytest.raises(RuntimeError, match="pixel safety"):
        _validate_image(_photo_bytes(), config)
    monkeypatch.undo()
    bad_session = CommonsSession(image=b"not an image")
    destination.unlink()
    with pytest.raises(RuntimeError, match="decoded"):
        asyncio.run(acquire_candidate_image(bad_session, config, candidate))


def test_download_safety_retries_and_failures(config):
    class SequenceSession:
        def __init__(self, responses):
            self.responses = list(responses)

        def get(self, _url, **_kwargs):
            return self.responses.pop(0)

    image = _photo_bytes()
    recovered = asyncio.run(
        _download_bytes(
            SequenceSession([Response(status=503), Response(data=image)]), "https://image", 2
        )
    )
    assert recovered == image
    with pytest.raises(RuntimeError, match="download failed"):
        asyncio.run(_download_bytes(SequenceSession([Response(status=503)]), "https://image", 1))
    with pytest.raises(RuntimeError, match="download failed"):
        asyncio.run(
            _download_bytes(
                SequenceSession(
                    [Response(data=image, headers={"Content-Length": str(30 * 1024 * 1024)})]
                ),
                "https://image",
                1,
            )
        )
    oversized = b"x" * (25 * 1024 * 1024 + 1)
    with pytest.raises(RuntimeError, match="download failed"):
        asyncio.run(
            _download_bytes(SequenceSession([Response(data=oversized)]), "https://image", 1)
        )


def test_commons_json_rate_limit_and_response_validation(monkeypatch):
    class SequenceSession:
        def __init__(self, responses):
            self.responses = list(responses)

        def get(self, _url, **_kwargs):
            return self.responses.pop(0)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(commons_module.asyncio, "sleep", no_sleep)
    payload = {"query": {"pages": []}}
    assert asyncio.run(
        _commons_json(
            SequenceSession(
                [Response(status=429, headers={"Retry-After": "2"}), Response(payload=payload)]
            ),
            "https://commons",
            2,
        )
    ) == payload
    for response in (
        Response(status=503),
        Response(payload=[]),
        Response(payload={"error": {"code": "maxlag"}}),
    ):
        with pytest.raises(RuntimeError, match="Commons API request failed"):
            asyncio.run(_commons_json(SequenceSession([response]), "https://commons", 1))


def test_renderers_use_branding_and_preserve_dimensions(tmp_path, config, show, race):
    branding = config["paths"]["branding"]
    branding.mkdir(parents=True)
    Image.new("RGBA", (240, 80), (230, 20, 40, 255)).save(branding / "logo.png")
    photo = tmp_path / "car.jpg"
    photo.write_bytes(_photo_bytes())
    poster = tmp_path / "poster.png"
    background = tmp_path / "background.png"
    episode = tmp_path / "episode.png"
    assert len(render_show_poster(show, race, "M0 0 L100 100", photo, config, poster)) == 64
    assert len(render_show_background(show, race, photo, config, background)) == 64
    assert len(
        render_episode_poster(
            show.episodes[0], race, "M0 0 L100 100", photo, config, episode
        )
    ) == 64
    with Image.open(poster) as image:
        assert image.size == (600, 900)
        assert image.info["MetaFusion renderer"] == "show-poster-v16"
        assert (
            image.info["MetaFusion design"]
            == "adaptive-concept-a-v4-subject-exposure"
        )
    with Image.open(background) as image:
        assert image.size == (1280, 720)
        # The source photograph is full bleed; the left is only gently shaded
        # for Plex title legibility and no red technical border is rendered.
        left_luminance = sum(
            image.resize((16, 9))
            .crop((0, 0, 7, 9))
            .convert("L")
            .get_flattened_data()
        )
        full_luminance = sum(
            image.resize((16, 9)).convert("L").get_flattened_data()
        )
        right_luminance = sum(
            image.resize((16, 9))
            .crop((9, 0, 16, 9))
            .convert("L")
            .get_flattened_data()
        )
        left_average = left_luminance / (7 * 9)
        right_average = right_luminance / (7 * 9)
        assert left_average > 35
        assert right_average > left_average
        assert 45 < full_luminance / (16 * 9) < 170
        assert image.getpixel((0, 3))[0] < 180
    with Image.open(episode) as image:
        assert image.size == (1280, 720)
    assert SHOW_RENDERER_VERSION == 16
    assert EPISODE_RENDERER_VERSION == 1
    assert _asset_reference(config, "2026/test.png").endswith("/2026/test.png")
    assert _episode_reference(config, show.episodes[0]).endswith(
        "/2026/round-01/episodes/episode-01.png"
    )


def test_default_show_background_is_4k_while_episode_cards_remain_full_hd(
    tmp_path, core, show, race
):
    config = load_formula1_config(core, tmp_path / "default-config")
    assert config["show_artwork"]["background_width"] == 3840
    assert config["show_artwork"]["background_height"] == 2160
    assert config["show_artwork"]["episode_width"] == 1920
    assert config["show_artwork"]["episode_height"] == 1080
    assert config["show_artwork"]["minimum_background_source_width"] == 3840
    assert config["show_artwork"]["minimum_background_source_height"] == 2160
    assert config["show_artwork"]["fallback_background_source_width"] == 1600
    assert config["show_artwork"]["fallback_background_source_height"] == 900
    photo = tmp_path / "source-4k.jpg"
    photo.write_bytes(_photo_bytes((3840, 2160)))
    destination = tmp_path / "background-4k.png"
    render_show_background(show, race, photo, config, destination)
    with Image.open(destination) as rendered:
        assert rendered.size == (3840, 2160)


def test_background_prefers_4k_but_accepts_previous_source_floor(
    tmp_path, core, show, race, monkeypatch
):
    config = load_formula1_config(core, tmp_path / "fallback-config")
    state = Formula1State(config["paths"]["database"])
    source = config["paths"]["show_image_cache"] / "fallback-2560.jpg"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(_photo_bytes((2560, 1440)))
    team_candidate = CommonsCandidate(
        page_id=8001,
        title="File:2026 Formula One race car.jpg",
        page_url="https://commons.wikimedia.org/?curid=8001",
        image_url="https://upload.wikimedia.org/fallback-2560.jpg",
        width=2560,
        height=1440,
        mime="image/jpeg",
        source_sha1="fallback2560sha1",
        author="Test author",
        licence="CC BY 4.0",
        licence_url="https://creativecommons.org/licenses/by/4.0/",
        constructor_id="mclaren",
        constructor_name="McLaren",
        score=10.0,
    )

    async def empty_search(*_args, **_kwargs):
        return [], "test"

    async def fallback_image(*_args, **_kwargs):
        return source, "cache"

    monkeypatch.setattr(show_artwork_module, "search_race_backgrounds", empty_search)
    monkeypatch.setattr(show_artwork_module, "acquire_candidate_image", fallback_image)
    selected, selected_path, providers, _image_hash = asyncio.run(
        _select_background_source(
            None,
            state,
            config,
            race,
            None,
            race.round_number,
            logging.getLogger("fallback-resolution"),
            team_candidate,
        )
    )
    assert "fallback-resolution-source" in selected.evidence
    assert "4k-source" not in selected.evidence
    assert selected_path == source
    assert providers["match_tier"] == "current_season_team_car_fallback"

    destination = tmp_path / "rendered-fallback-4k.png"
    render_show_background(show, race, selected_path, config, destination)
    with Image.open(destination) as rendered:
        assert rendered.size == (3840, 2160)
    state.close()


def test_background_cache_upgrades_to_4k_when_provider_source_can_supply_it(
    tmp_path, core
):
    config = load_formula1_config(core, tmp_path / "cache-upgrade-config")
    constructor = ConstructorData("alpine", "Alpine F1 Team")
    candidate = parse_commons_candidates(
        _commons_payload(width=3840, height=2160),
        2026,
        constructor,
        [constructor],
        config,
    )[0]
    session = CommonsSession()
    first_path, first_source = asyncio.run(
        show_artwork_module._acquire_background_image(session, config, candidate)
    )
    assert first_source == "wikimedia-commons"
    with Image.open(first_path) as first_image:
        assert first_image.size == (1600, 900)

    session.image = _photo_bytes((3840, 2160))
    upgraded_path, upgraded_source = asyncio.run(
        show_artwork_module._acquire_background_image(session, config, candidate)
    )
    assert upgraded_path == first_path
    assert upgraded_source == "wikimedia-commons"
    with Image.open(upgraded_path) as upgraded_image:
        assert upgraded_image.size == (3840, 2160)
    assert asyncio.run(
        show_artwork_module._acquire_background_image(session, config, candidate)
    )[1] == "cache"


def test_background_candidate_order_prefers_4k_within_the_same_match_tier(config):
    fallback = parse_race_background_candidates(
        _race_background_payload(page_id=930, width=1600, height=900),
        2026,
        config,
    )[0]
    preferred = parse_race_background_candidates(
        _race_background_payload(page_id=931, width=3840, height=2160),
        2026,
        config,
    )[0]
    fallback = replace(fallback, score=999.0, evidence=("fallback-resolution-source",))
    preferred = replace(preferred, score=1.0, evidence=("4k-source",))
    ordered = _background_candidate_order([fallback, preferred], None, 1)
    assert ordered == [preferred, fallback]
    current = {"source": {"background_candidate": {"page_id": fallback.page_id}}}
    assert _background_candidate_order([fallback, preferred], current, 1) == [
        preferred,
        fallback,
    ]


def test_show_poster_flag_badge_is_rounded_and_undistorted(config, race):
    image = Image.new("RGB", (1000, 1500), (6, 7, 10))
    assert _rounded_flag_badge(
        image, race.country, (790, 1282, 940, 1365)
    )
    assert image.getpixel((790, 1282)) == (6, 7, 10)
    assert image.getpixel((865, 1323)) != (6, 7, 10)
    assert max(image.getpixel((865, 1282))) < 230
    assert not _rounded_flag_badge(image, "Unknown", (790, 1282, 940, 1365))


def test_show_poster_flag_uses_dark_adaptive_keyline_on_light_background(race):
    image = Image.new("RGB", (1000, 1500), (245, 245, 245))
    assert _rounded_flag_badge(
        image,
        replace(race, country="Japan").country,
        (790, 1282, 940, 1365),
    )
    assert image.getpixel((790, 1282)) == (245, 245, 245)
    assert max(image.getpixel((865, 1282))) < 220


def test_post_race_press_conference_episode_card_is_rendered(
    tmp_path, config, show, race
):
    episode = replace(
        show.episodes[0],
        episode_number=15,
        program_title="Post-Race Press Conference",
        program_kind="post_race_press_conference",
    )
    photo = tmp_path / "car.jpg"
    photo.write_bytes(_photo_bytes())
    destination = tmp_path / "press-conference.png"
    checksum = render_episode_poster(
        episode,
        race,
        "M0 0 L100 100",
        photo,
        config,
        destination,
    )
    assert len(checksum) == 64
    with Image.open(destination) as image:
        assert image.size == (1280, 720)
    assert session_date(episode, race, config)[0] == race.race_date
    assert _episode_fingerprint(
        episode, race, "M0 0 L100 100", "source", config
    ) != _episode_fingerprint(
        show.episodes[0], race, "M0 0 L100 100", "source", config
    )


def test_photo_grade_compresses_bright_background_and_episode_ownership(
    tmp_path, config, show, race
):
    bright = Image.new("RGB", (1600, 900), (250, 245, 235))
    draw = ImageDraw.Draw(bright)
    draw.rectangle((700, 350, 1450, 720), fill=(220, 20, 45))
    graded = _grade_photo(bright, (1280, 720), centering=(0.62, 0.5), strong=True)
    assert sum(graded.getpixel((100, 100))) < sum(bright.getpixel((100, 100))) * 0.8
    assert graded.getpixel((1000, 450))[0] > graded.getpixel((1000, 450))[1] * 2

    state = Formula1State(config["paths"]["database"])
    destination = tmp_path / "episode.png"
    fingerprint = _episode_fingerprint(
        show.episodes[0], race, "M0 0", "source", config
    )
    assert _managed_episode_action(
        state, show.episodes[0], destination, fingerprint
    ) == "create"
    destination.write_bytes(b"manual")
    assert _managed_episode_action(
        state, show.episodes[0], destination, fingerprint
    ) == "preserve-manual"
    state.save_artwork(
        show.episodes[0].logical_key,
        destination,
        fingerprint,
        _checksum(destination),
    )
    assert _managed_episode_action(
        state, show.episodes[0], destination, fingerprint
    ) == "unchanged"
    assert _managed_episode_action(
        state, show.episodes[0], destination, "changed"
    ) == "update"
    destination.write_bytes(b"manual edit")
    assert _managed_episode_action(
        state, show.episodes[0], destination, "changed"
    ) == "preserve-manual"
    state.close()


def test_show_poster_grade_adapts_exposure_without_clipping_team_colours():
    dark = Image.new("RGB", (1600, 900), (36, 38, 42))
    ImageDraw.Draw(dark).rectangle((650, 300, 1450, 720), fill=(78, 12, 20))
    legacy_dark = _grade_photo(dark, (1280, 720), contain=True)
    showcased_dark = _poster_showcase_grade(dark, (1280, 720))
    assert sum(showcased_dark.getpixel((900, 450))) > sum(
        legacy_dark.getpixel((900, 450))
    ) * 1.35
    assert showcased_dark.getpixel((900, 450))[0] > (
        showcased_dark.getpixel((900, 450))[1] * 3
    )

    bright = Image.new("RGB", (1600, 900), (248, 245, 238))
    ImageDraw.Draw(bright).rectangle((650, 300, 1450, 720), fill=(225, 24, 44))
    showcased_bright = _poster_showcase_grade(bright, (1280, 720))
    assert max(showcased_bright.getpixel((100, 100))) < 240
    assert showcased_bright.getpixel((900, 450))[0] > (
        showcased_bright.getpixel((900, 450))[1] * 3
    )

    backlit = Image.new("RGB", (1600, 900), (248, 245, 238))
    ImageDraw.Draw(backlit).rounded_rectangle(
        (650, 300, 1450, 720), 40, fill=(62, 18, 35)
    )
    showcased_backlit = _poster_showcase_grade(backlit, (1280, 720))
    assert showcased_backlit.getpixel((900, 450))[0] >= 58
    assert max(showcased_backlit.getpixel((100, 100))) < 240


def test_show_poster_profile_preserves_car_position_and_visual_lead_room():
    left = Image.new("RGB", (1600, 900), (195, 200, 205))
    right = left.copy()
    ImageDraw.Draw(left).rounded_rectangle(
        (120, 300, 760, 680), 30, fill=(220, 20, 40), outline=(5, 5, 5), width=16
    )
    ImageDraw.Draw(right).rounded_rectangle(
        (840, 300, 1480, 680), 30, fill=(220, 20, 40), outline=(5, 5, 5), width=16
    )

    left_profile = _poster_photo_profile(left)
    right_profile = _poster_photo_profile(right)
    assert left_profile.focal_x < 0.4
    assert left_profile.composition_x == 0.38
    assert right_profile.focal_x > 0.6
    assert right_profile.composition_x == 0.62
    assert left_profile.shadow_luminance < left_profile.median_luminance

    left_crop = _adaptive_showcase_crop(left, (600, 350), left_profile)
    right_crop = _adaptive_showcase_crop(right, (600, 350), right_profile)
    assert left_crop.size == right_crop.size == (600, 350)
    assert left_crop.getpixel((100, 200))[0] > 180
    assert right_crop.getpixel((500, 200))[0] > 180


def test_show_poster_profile_and_crop_have_safe_blank_and_portrait_fallbacks():
    blank = Image.new("RGB", (1600, 900), (20, 20, 20))
    profile = _poster_photo_profile(blank)
    assert profile.focal_x == 0.5
    assert profile.subject_box == (0.08, 0.12, 0.92, 0.92)
    assert _adaptive_showcase_crop(blank, (600, 350), profile).size == (600, 350)
    assert _adaptive_showcase_crop(blank, (600, 750), profile).size == (600, 750)
    portrait_blank = Image.new("RGB", (700, 1200), (20, 20, 20))
    assert _adaptive_showcase_crop(
        portrait_blank, (600, 350), _poster_photo_profile(portrait_blank)
    ).size == (600, 350)

    portrait = Image.new("RGB", (700, 1200), (185, 185, 185))
    ImageDraw.Draw(portrait).rectangle(
        (80, 500, 620, 680), fill=(30, 90, 220), outline=(4, 4, 4), width=12
    )
    portrait_profile = _poster_photo_profile(portrait)
    cropped = _adaptive_showcase_crop(portrait, (600, 350), portrait_profile)
    assert cropped.size == (600, 350)
    assert cropped.getpixel((300, 175))[2] > cropped.getpixel((300, 175))[0] * 2


def test_renderer_failure_cleanup_and_dry_attribution(
    tmp_path, config, show, race, monkeypatch
):
    photo = tmp_path / "car.jpg"
    photo.write_bytes(_photo_bytes())
    monkeypatch.setattr(
        show_artwork_module,
        "atomic_replace_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("install")),
    )
    with pytest.raises(OSError, match="install"):
        render_show_poster(show, race, None, photo, config, tmp_path / "poster.png")
    assert not list(tmp_path.glob("*.png"))
    config["dry_run"] = True
    show_artwork_module._attribution_reports(config, [])
    assert not config["paths"]["reports"].exists()


def test_attribution_skips_incomplete_legacy_round_source(config):
    show_artwork_module._attribution_reports(
        config,
        [],
        [{"season_year": 2026, "round_number": 1, "source": {}}],
    )
    report = json.loads(
        (
            config["paths"]["reports"]
            / "formula1-show-artwork-attribution.json"
        ).read_text()
    )
    assert report == {"records": []}


def test_source_selection_skips_provider_and_image_failures(
    config, monkeypatch
):
    state = Formula1State(config["paths"]["database"])
    roster = parse_constructor_payload(_constructor_payload())

    async def roster_loader(*_args, **_kwargs):
        return roster, "test"

    searches = []

    async def search(_session, _state, _config, year, constructor, _roster, _logger):
        searches.append((year, constructor.constructor_id))
        if constructor.constructor_id == "alpine":
            raise RuntimeError("search failed")
        return parse_commons_candidates(
            _commons_payload(constructor.name, constructor.constructor_id),
            year,
            constructor,
            roster,
            config,
        ), "test"

    async def acquire(_session, _config, candidate):
        if candidate.constructor_id == "ferrari":
            raise RuntimeError("bad pixels")
        return Path("/cached/car.jpg"), "test"

    monkeypatch.setattr(show_artwork_module, "load_constructors", roster_loader)
    monkeypatch.setattr(show_artwork_module, "search_commons", search)
    monkeypatch.setattr(show_artwork_module, "acquire_candidate_image", acquire)
    candidate, path, sources = asyncio.run(
        show_artwork_module._select_source(
            None, state, config, 2026, None, 1, logging.getLogger("selection")
        )
    )
    assert candidate.constructor_id == "mclaren"
    assert path == Path("/cached/car.jpg")
    assert sources["image"] == "test"
    assert [value[1] for value in searches] == ["alpine", "ferrari", "mclaren"]
    state.close()


def test_race_triggered_rotation_state_restore_manual_and_attribution(
    tmp_path, config, show, race, monkeypatch
):
    state = Formula1State(config["paths"]["database"])
    session = CommonsSession()
    first = asyncio.run(
        run_show_artwork_rotation(
            session, state, config, show, race, "M0 0 L100 100", logging.getLogger("rotation")
        )
    )
    assert first.action == "rotated" and first.constructor == "Alpine F1 Team"
    assert first.episode_actions == {1: "create"}
    assert first.episode_references[1].endswith(
        "/2026/round-01/episodes/episode-01.png"
    )
    assert first.photo_path and first.source_identity
    assert (
        config["paths"]["assets"]
        / "2026/round-01/episodes/episode-01.png"
    ).is_file()
    current = state.show_rotation("show:2026")
    assert _pair_integrity(current) == "managed"
    assert current["source"]["candidate"]["constructor_id"] == "alpine"
    assert "race car" in current["source"]["background_candidate"]["title"]
    assert first.background_vehicle and "race car" in first.background_vehicle
    assert len(state.show_rotation_history()) == 1
    assert state.episode_round_source(2026, 1)["constructor_id"] == "alpine"
    attribution_path = (
        config["paths"]["reports"] / "formula1-show-artwork-attribution.json"
    )
    assert attribution_path.exists()
    background_record = next(
        record
        for record in json.loads(attribution_path.read_text())["records"]
        if record["scope"] == "show_background"
    )
    assert background_record["match_tier"] == "exact_event_action_race_car"
    assert background_record["race_key"].startswith("2026:01:albert-park")
    assert background_record["observed_environment"] in {"day", "twilight", "night"}
    assert "Liauzh" in (
        config["paths"]["reports"] / "formula1-show-artwork-attribution.txt"
    ).read_text()
    unchanged = asyncio.run(
        run_show_artwork_rotation(
            session, state, config, show, race, None, logging.getLogger("rotation")
        )
    )
    assert unchanged.action == "unchanged"
    assert unchanged.episode_actions == {1: "unchanged"}
    with monkeypatch.context() as patch:
        async def unavailable(*_args, **_kwargs):
            raise RuntimeError("offline")

        patch.setattr(show_artwork_module, "acquire_candidate_image", unavailable)
        unavailable_result = asyncio.run(
            run_show_artwork_rotation(
                session, state, config, show, race, None, logging.getLogger("unavailable")
            )
        )
    assert unavailable_result.action == "unchanged"
    assert "source unavailable" in unavailable_result.issue

    second_race = RaceData(**{**race.__dict__, "round_number": 2, "name": "Chinese Grand Prix"})
    second = asyncio.run(
        run_show_artwork_rotation(
            session,
            state,
            config,
            show,
            second_race,
            "M0 0 L50 100",
            logging.getLogger("rotation"),
        )
    )
    assert second.action == "rotated" and second.constructor == "Ferrari"
    assert len(state.show_rotation_history()) == 2
    assert state.episode_round_source(2026, 2)["constructor_id"] == "ferrari"
    current = state.show_rotation("show:2026")
    Path(current["background_destination"]).unlink()
    restored = asyncio.run(
        run_show_artwork_rotation(
            session,
            state,
            config,
            show,
            second_race,
            None,
            logging.getLogger("rotation"),
        )
    )
    assert restored.action == "restored"
    assert len(state.show_rotation_history()) == 2
    current = state.show_rotation("show:2026")
    Path(current["poster_destination"]).write_bytes(b"manual")
    assert _pair_integrity(current) == "manual"
    third_race = RaceData(**{**race.__dict__, "round_number": 3, "name": "Japanese Grand Prix"})
    preserved = asyncio.run(
        run_show_artwork_rotation(
            session, state, config, show, third_race, None, logging.getLogger("rotation")
        )
    )
    assert preserved.action == "preserve-manual" and preserved.issue
    state.close()


def test_historical_episode_rounds_receive_persistent_distinct_sources(
    config, show, race
):
    state = Formula1State(config["paths"]["database"])
    session = CommonsSession()
    round_one = asyncio.run(
        reconcile_episode_round_artwork(
            session,
            state,
            config,
            show,
            race,
            "M0 0 L10 10",
            logging.getLogger("round-one"),
        )
    )
    assert round_one.constructor == "Alpine F1 Team"
    assert round_one.actions == {1: "create"}

    second_episode = replace(
        show.episodes[0],
        round_number=2,
        event_name="Chinese Grand Prix",
        plex_rating_key="episode-2",
    )
    show.episodes.append(second_episode)
    second_race = RaceData(
        **{
            **race.__dict__,
            "round_number": 2,
            "name": "Chinese Grand Prix",
            "circuit_id": "shanghai",
            "circuit": "Shanghai International Circuit",
            "locality": "Shanghai",
            "country": "China",
        }
    )
    round_two = asyncio.run(
        reconcile_episode_round_artwork(
            session,
            state,
            config,
            show,
            second_race,
            "M0 0 L20 20",
            logging.getLogger("round-two"),
        )
    )
    assert round_two.constructor == "Ferrari"
    assert round_two.actions == {1: "create"}
    assert state.episode_round_source(2026, 1)["constructor_id"] == "alpine"
    assert state.episode_round_source(2026, 2)["constructor_id"] == "ferrari"

    repeat = asyncio.run(
        reconcile_episode_round_artwork(
            session,
            state,
            config,
            show,
            second_race,
            "M0 0 L20 20",
            logging.getLogger("round-two-repeat"),
        )
    )
    assert repeat.constructor == "Ferrari"
    assert repeat.actions == {1: "unchanged"}

    destination = (
        config["paths"]["assets"]
        / "2026/round-02/episodes/episode-01.png"
    )
    destination.write_bytes(b"manual episode card")
    manual = asyncio.run(
        reconcile_episode_round_artwork(
            session,
            state,
            config,
            show,
            second_race,
            "M0 0 L30 30",
            logging.getLogger("round-two-manual"),
        )
    )
    assert manual.actions == {1: "preserve-manual"}
    assert destination.read_bytes() == b"manual episode card"
    state.close()


def test_existing_episode_output_inventory_preserves_unknown_and_modified(
    tmp_path, config, show, race
):
    state = Formula1State(config["paths"]["database"])
    first = show.episodes[0]
    second = replace(first, episode_number=2, plex_rating_key="episode-2")
    show.episodes.append(second)
    for episode, content in ((first, b"first"), (second, b"second")):
        destination = (
            config["paths"]["assets"]
            / f"2026/round-01/episodes/episode-{episode.episode_number:02d}.png"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    first_destination = (
        config["paths"]["assets"] / "2026/round-01/episodes/episode-01.png"
    )
    state.save_artwork(first.logical_key, first_destination, "fp", _checksum(first_destination))
    first_destination.write_bytes(b"manual")
    references, actions = _existing_episode_outputs(state, config, show, race)
    assert set(references) == {1, 2}
    assert actions == {1: "preserve-manual", 2: "preserve-manual"}
    state.close()


def test_missing_only_episode_backfill_preserves_existing_and_creates_missing(
    tmp_path, config, show, race
):
    state = Formula1State(config["paths"]["database"])
    existing = show.episodes[0]
    missing = replace(existing, episode_number=2, plex_rating_key="episode-2")
    show.episodes.append(missing)
    destination = (
        config["paths"]["assets"] / "2026/round-01/episodes/episode-01.png"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"manual")
    photo = tmp_path / "car.jpg"
    photo.write_bytes(_photo_bytes())
    references, actions = reconcile_episode_posters(
        state,
        config,
        show,
        race,
        "M0 0 L10 10",
        photo,
        "source",
        missing_only=True,
    )
    assert set(references) == {1, 2}
    assert actions == {1: "preserve-manual", 2: "create"}
    assert (
        config["paths"]["assets"] / "2026/round-01/episodes/episode-02.png"
    ).is_file()
    state.close()


def test_same_round_renderer_change_rerenders_without_team_rotation(
    config, show, race, monkeypatch
):
    state = Formula1State(config["paths"]["database"])
    session = CommonsSession()
    first = asyncio.run(
        run_show_artwork_rotation(
            session, state, config, show, race, "M0 0 L10 10", logging.getLogger("first")
        )
    )
    current = state.show_rotation("show:2026")
    background = Path(current["background_destination"])
    background_content = background.read_bytes()
    background_checksum = current["background_checksum"]
    source = current["source"]
    source.pop("poster_render_fingerprint")
    state.connection.execute(
        "UPDATE show_rotation_state SET source=? WHERE logical_key='show:2026'",
        (json.dumps(source, sort_keys=True),),
    )
    state.connection.commit()

    async def background_must_not_be_acquired(*_args, **_kwargs):
        raise AssertionError("poster-only maintenance acquired a background")

    monkeypatch.setattr(
        show_artwork_module, "_acquire_background_image", background_must_not_be_acquired
    )
    config["dry_run"] = True
    planned = asyncio.run(
        run_show_artwork_rotation(
            session, state, config, show, race, "M0 0 L10 10", logging.getLogger("plan")
        )
    )
    assert planned.action == "rerender-planned"
    config["dry_run"] = False
    rerendered = asyncio.run(
        run_show_artwork_rotation(
            session, state, config, show, race, "M0 0 L10 10", logging.getLogger("rerender")
        )
    )
    assert first.constructor == rerendered.constructor
    assert rerendered.action == "rerendered"
    assert len(state.show_rotation_history()) == 1
    current = state.show_rotation("show:2026")
    assert current["source"]["poster_render_fingerprint"]
    assert current["source"]["background_render_fingerprint"]
    assert current["background_checksum"] == background_checksum
    assert background.read_bytes() == background_content
    state.close()


def test_background_renderer_change_remains_an_independent_full_rerender(
    config, show, race
):
    state = Formula1State(config["paths"]["database"])
    session = CommonsSession()
    first = asyncio.run(
        run_show_artwork_rotation(
            session, state, config, show, race, "M0 0 L10 10", logging.getLogger("first")
        )
    )
    current = state.show_rotation("show:2026")
    source = current["source"]
    source["background_render_fingerprint"] = "older-background-renderer"
    source["background_candidate"]["eligibility_version"] = 3
    source["background_candidate"]["subject_type"] = "circuit_atmosphere"
    source["background_candidate"]["match_tier"] = "exact_event_atmosphere"
    state.connection.execute(
        "UPDATE show_rotation_state SET source=? WHERE logical_key='show:2026'",
        (json.dumps(source, sort_keys=True),),
    )
    state.connection.commit()

    rerendered = asyncio.run(
        run_show_artwork_rotation(
            session, state, config, show, race, "M0 0 L10 10", logging.getLogger("rerender")
        )
    )
    assert rerendered.action == "rerendered"
    assert rerendered.constructor == first.constructor
    current = state.show_rotation("show:2026")
    assert current["source"]["background_candidate"]["eligibility_version"] == 6
    assert (
        current["source"]["background_candidate"]["match_tier"]
        == "exact_event_action_race_car"
    )
    state.close()


def test_missing_poster_is_restored_without_background_acquisition(
    config, show, race, monkeypatch
):
    state = Formula1State(config["paths"]["database"])
    session = CommonsSession()
    asyncio.run(
        run_show_artwork_rotation(
            session,
            state,
            config,
            show,
            race,
            "M0 0 L10 10",
            logging.getLogger("first"),
        )
    )
    current = state.show_rotation("show:2026")
    poster = Path(current["poster_destination"])
    background = Path(current["background_destination"])
    background_content = background.read_bytes()
    background_checksum = current["background_checksum"]
    poster.unlink()

    async def background_must_not_be_acquired(*_args, **_kwargs):
        raise AssertionError("missing-poster repair acquired a background")

    monkeypatch.setattr(
        show_artwork_module,
        "_acquire_background_image",
        background_must_not_be_acquired,
    )
    restored = asyncio.run(
        run_show_artwork_rotation(
            session,
            state,
            config,
            show,
            race,
            "M0 0 L10 10",
            logging.getLogger("restore"),
        )
    )
    assert restored.action == "restored"
    assert restored.poster_renderer_version == 16
    assert restored.poster_checksum == _checksum(poster)
    assert background.read_bytes() == background_content
    current = state.show_rotation("show:2026")
    assert current["background_checksum"] == background_checksum
    with Image.open(poster) as image:
        assert image.info["MetaFusion renderer"] == "show-poster-v16"
        assert (
            image.info["MetaFusion design"]
            == "adaptive-concept-a-v4-subject-exposure"
        )
    state.close()


def test_manual_background_does_not_block_managed_poster_renderer_update(
    config, show, race
):
    state = Formula1State(config["paths"]["database"])
    session = CommonsSession()
    asyncio.run(
        run_show_artwork_rotation(
            session, state, config, show, race, "M0 0 L10 10", logging.getLogger("first")
        )
    )
    current = state.show_rotation("show:2026")
    background = Path(current["background_destination"])
    background.write_bytes(b"manual background")
    source = current["source"]
    source.pop("poster_render_fingerprint")
    state.connection.execute(
        "UPDATE show_rotation_state SET source=? WHERE logical_key='show:2026'",
        (json.dumps(source, sort_keys=True),),
    )
    state.connection.commit()

    result = asyncio.run(
        run_show_artwork_rotation(
            session, state, config, show, race, "M0 0 L10 10", logging.getLogger("rerender")
        )
    )
    assert result.action == "rerendered"
    assert "manually modified" in result.issue
    assert background.read_bytes() == b"manual background"
    state.close()


def test_rotation_and_source_cache_retention_are_safe(config, show, race):
    config["show_artwork"]["retention_pairs_per_season"] = 1
    config["show_artwork"]["source_cache_retention_days"] = 1
    state = Formula1State(config["paths"]["database"])
    session = CommonsSession()
    asyncio.run(
        run_show_artwork_rotation(
            session, state, config, show, race, "M0 0 L10 10", logging.getLogger("retain-1")
        )
    )
    first = state.show_rotation("show:2026")
    Path(first["poster_destination"]).parent.joinpath("keep.txt").write_text("manual")
    old_cache = config["paths"]["show_image_cache"] / "old.jpg"
    old_cache.write_bytes(_photo_bytes())
    os.utime(old_cache, (1, 1))
    current_cache = config["paths"]["show_image_cache"] / "current.jpg"
    current_cache.write_bytes(_photo_bytes())
    os.utime(current_cache, (1, 1))
    assert _prune_source_cache(config, current_cache) >= 1
    assert current_cache.exists() and not old_cache.exists()
    second_race = RaceData(**{**race.__dict__, "round_number": 2, "name": "Chinese Grand Prix"})
    result = asyncio.run(
        run_show_artwork_rotation(
            session,
            state,
            config,
            show,
            second_race,
            "M0 0 L20 20",
            logging.getLogger("retain-2"),
        )
    )
    assert result.pairs_pruned == 1
    assert len(state.show_rotation_history()) == 1
    assert not Path(first["poster_destination"]).exists()
    state.close()


def test_pair_retention_preserves_unknown_current_and_modified_records(
    tmp_path, config
):
    config["show_artwork"]["retention_pairs_per_season"] = 1

    class State:
        def __init__(self, history):
            self.history = history
            self.removed = []

        def show_rotation_history(self):
            return self.history

        def remove_show_rotation_history(self, value):
            self.removed.append(value)

    current_poster = tmp_path / "current/poster.png"
    current_background = tmp_path / "current/background.png"
    current_poster.parent.mkdir()
    current_poster.write_bytes(b"poster")
    current_background.write_bytes(b"background")
    unknown = {
        "id": 1,
        "logical_key": "show:2026",
        "source": {},
        "poster_destination": str(tmp_path / "unknown/poster.png"),
        "background_destination": str(tmp_path / "unknown/background.png"),
    }
    current_history = {
        "id": 2,
        "logical_key": "show:2026",
        "source": {
            "generated_checksums": {
                "poster": _checksum(current_poster),
                "background": _checksum(current_background),
            }
        },
        "poster_destination": str(current_poster),
        "background_destination": str(current_background),
    }
    state = State([unknown, current_history])
    current = {
        "poster_destination": str(current_poster),
        "background_destination": str(current_background),
    }
    assert _prune_retained_pairs(state, config, "show:2026", current) == 0
    state.history = [current_history, unknown]
    assert _prune_retained_pairs(state, config, "show:2026", current) == 0
    modified_poster = tmp_path / "modified/poster.png"
    modified_background = tmp_path / "modified/background.png"
    modified_poster.parent.mkdir()
    modified_poster.write_bytes(b"manual")
    modified_background.write_bytes(b"background")
    modified = {
        "id": 3,
        "logical_key": "show:2026",
        "source": {
            "generated_checksums": {
                "poster": "not-the-current-checksum",
                "background": _checksum(modified_background),
            }
        },
        "poster_destination": str(modified_poster),
        "background_destination": str(modified_background),
    }
    state.history = [modified, current_history]
    assert _prune_retained_pairs(state, config, "show:2026", current) == 0
    assert state.removed == []


def test_older_round_does_not_replace_or_repair_newer_show_artwork(
    config, show, race
):
    state = Formula1State(config["paths"]["database"])
    session = CommonsSession()
    newer = RaceData(**{**race.__dict__, "round_number": 2, "name": "Chinese Grand Prix"})
    asyncio.run(
        run_show_artwork_rotation(
            session, state, config, show, newer, None, logging.getLogger("newer")
        )
    )
    managed = asyncio.run(
        run_show_artwork_rotation(
            session, state, config, show, race, None, logging.getLogger("older")
        )
    )
    assert managed.action == "unchanged"
    current = state.show_rotation("show:2026")
    Path(current["background_destination"]).unlink()
    missing = asyncio.run(
        run_show_artwork_rotation(
            session, state, config, show, race, None, logging.getLogger("older-missing")
        )
    )
    assert missing.action == "preserved" and "newer round" in missing.issue
    state.close()


def test_rotation_missing_dry_run_order_and_metadata(tmp_path, config, show, race):
    roster = parse_constructor_payload(_constructor_payload())
    assert [item.constructor_id for item in _candidate_order(roster, None, 2)] == [
        "ferrari",
        "mclaren",
        "alpine",
    ]
    assert _candidate_order([], None, 1) == []
    state = Formula1State(config["paths"]["database"])
    missing = asyncio.run(
        run_show_artwork_rotation(
            CommonsSession(candidates=False),
            state,
            config,
            show,
            race,
            None,
            logging.getLogger("missing"),
        )
    )
    assert missing.action == "missing" and missing.issue
    config["dry_run"] = True
    dry_state = Formula1State(":memory:")
    planned = asyncio.run(
        run_show_artwork_rotation(
            CommonsSession(), dry_state, config, show, race, None, logging.getLogger("dry")
        )
    )
    assert planned.action == "rotate-planned"
    assert not config["paths"]["show_assets"].exists()
    generated, _seasons, _episodes = build_show_entry(
        show,
        [race],
        {},
        config,
        {"poster": planned.poster_reference, "background": planned.background_reference},
    )
    assert generated["file_poster"] == planned.poster_reference
    assert generated["file_background"] == planned.background_reference
    dry_state.close()
    state.close()


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("show_artwork:\n  poster_width: 601\n", "2:3"),
        ("show_artwork:\n  background_width: 1281\n", "16:9"),
        ("show_artwork:\n  episode_width: 1281\n", "episode dimensions"),
        ("show_artwork:\n  trigger: weekly\n", "trigger"),
        ("show_artwork:\n  policy: overwrite\n", "policy"),
        (
            "show_artwork:\n  fallback_background_source_width: 4000\n",
            "fallback background source dimensions",
        ),
        ("providers:\n  commons_url: https://example.com/api\n", "commons_url"),
    ],
)
def test_show_artwork_configuration_is_strict(tmp_path, core, document, message):
    private = tmp_path / "config/formula1"
    private.mkdir(parents=True)
    (private / "formula1.yml").write_text(document, encoding="utf-8")
    with pytest.raises(Formula1ConfigError, match=message):
        load_formula1_config(core, tmp_path / "config", dry_run=True)
