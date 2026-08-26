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
from extensions.formula1.show_artwork import (
    EPISODE_RENDERER_VERSION,
    SHOW_RENDERER_VERSION,
    _asset_reference,
    _candidate_order,
    _checksum,
    _episode_fingerprint,
    _episode_reference,
    _existing_episode_outputs,
    _grade_photo,
    _managed_episode_action,
    _pair_integrity,
    _prune_retained_pairs,
    _prune_source_cache,
    _rounded_flag_badge,
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
        if "commons.wikimedia.org" in url:
            query = parse_qs(urlparse(url).query).get("gsrsearch", [""])[0].casefold()
            if not self.candidates:
                return Response(payload={"query": {"pages": []}})
            for constructor_id, team in (
                ("alpine", "Alpine F1 Team"),
                ("ferrari", "Ferrari"),
                ("mclaren", "McLaren"),
            ):
                if constructor_id in query:
                    return Response(payload=_commons_payload(team, constructor_id))
            return Response(payload={"query": {"pages": []}})
        if "upload.wikimedia.org" in url:
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
        minimum_source_width=800,
        minimum_source_height=450,
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
    with Image.open(background) as image:
        assert image.size == (1280, 720)
    with Image.open(episode) as image:
        assert image.size == (1280, 720)
    assert SHOW_RENDERER_VERSION == 5
    assert EPISODE_RENDERER_VERSION == 1
    assert _asset_reference(config, "2026/test.png").endswith("/2026/test.png")
    assert _episode_reference(config, show.episodes[0]).endswith(
        "/2026/round-01/episodes/episode-01.png"
    )


def test_show_poster_flag_badge_is_rounded_and_undistorted(config, race):
    image = Image.new("RGB", (1000, 1500), (6, 7, 10))
    assert _rounded_flag_badge(
        image, race.country, (790, 1282, 940, 1365)
    )
    assert image.getpixel((790, 1282)) == (6, 7, 10)
    assert image.getpixel((865, 1323)) != (6, 7, 10)
    assert not _rounded_flag_badge(image, "Unknown", (790, 1282, 940, 1365))


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
    assert len(state.show_rotation_history()) == 1
    assert state.episode_round_source(2026, 1)["constructor_id"] == "alpine"
    assert (config["paths"]["reports"] / "formula1-show-artwork-attribution.json").exists()
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
    source.pop("render_fingerprint")
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
    assert first.constructor == rerendered.constructor
    assert rerendered.action == "rerendered"
    assert len(state.show_rotation_history()) == 1
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
        ("show_artwork:\n  trigger: weekly\n", "trigger"),
        ("show_artwork:\n  policy: overwrite\n", "policy"),
        ("providers:\n  commons_url: https://example.com/api\n", "commons_url"),
    ],
)
def test_show_artwork_configuration_is_strict(tmp_path, core, document, message):
    private = tmp_path / "config/formula1"
    private.mkdir(parents=True)
    (private / "formula1.yml").write_text(document, encoding="utf-8")
    with pytest.raises(Formula1ConfigError, match=message):
        load_formula1_config(core, tmp_path / "config", dry_run=True)
