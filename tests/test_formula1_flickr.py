import asyncio
from types import SimpleNamespace

import pytest

from extensions.formula1 import show_artwork as show_artwork_module
from extensions.formula1.commons import CommonsCandidate, ConstructorData
from extensions.formula1.config import Formula1ConfigError, load_formula1_config
from extensions.formula1.flickr import (
    FlickrError,
    _allowed_licences,
    _background_queries,
    _common_photo_fields,
    _flickr_json,
    _licences,
    _mime,
    _photo_dimensions,
    _safe_image_url,
    _search,
    _team_queries,
    parse_flickr_background_candidates,
    parse_flickr_team_candidates,
    search_flickr_backgrounds,
    search_flickr_team_photos,
)


def _core(tmp_path):
    return {"settings": {"mode": "kometa", "path": str(tmp_path / "kometa")}}


def _licence_payload():
    return {
        "licenses": {
            "license": [
                {
                    "id": "4",
                    "name": "Attribution License",
                    "url": "https://creativecommons.org/licenses/by/2.0/",
                },
                {
                    "id": "5",
                    "name": "Attribution-ShareAlike License",
                    "url": "https://creativecommons.org/licenses/by-sa/2.0/",
                },
                {
                    "id": "0",
                    "name": "All Rights Reserved",
                    "url": "https://www.flickr.com/help/general/",
                },
            ]
        }
    }


def _photo(**overrides):
    value = {
        "id": "54900123456",
        "owner": "owner-nsid",
        "ownername": "Race Photographer",
        "secret": "abc123",
        "title": "2026 Ferrari Formula 1 race car at the Australian Grand Prix",
        "description": {
            "_content": (
                "Ferrari F1 car racing on track at Albert Park Grand Prix Circuit "
                "during qualifying action"
            )
        },
        "tags": "f1 formula1 race car racing action albert park melbourne",
        "license": "4",
        "url_o": "https://live.staticflickr.com/65535/54900123456_abc_o.jpg",
        "width_o": "4096",
        "height_o": "2304",
        "originalformat": "jpg",
        "views": "40000",
    }
    value.update(overrides)
    return value


def _race():
    return SimpleNamespace(
        year=2026,
        round_number=1,
        name="Australian Grand Prix",
        circuit_id="albert_park",
        circuit="Albert Park Grand Prix Circuit",
        locality="Melbourne",
        country="Australia",
        circuit_profile="temporary urban circuit",
        race_date="2026-03-08",
        race_time_utc="05:00:00Z",
        latitude=-37.8497,
        longitude=144.968,
    )


class MemoryState:
    def __init__(self, current=None, stale=None):
        self.current = current
        self.stale = stale
        self.saved = None

    def cache_get(self, _provider, _key, allow_expired=False):
        return self.stale if allow_expired else self.current

    def cache_put(self, provider, key, payload, hours):
        self.saved = (provider, key, payload, hours)


class Response:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self, **_kwargs):
        return self.payload


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def test_flickr_config_uses_environment_before_yaml(tmp_path, monkeypatch):
    private = tmp_path / "config/formula1"
    private.mkdir(parents=True)
    (private / "formula1.yml").write_text(
        "providers:\n  flickr_api_key: yaml-key\n", encoding="utf-8"
    )
    monkeypatch.setenv("FORMULA1_FLICKR_API_KEY", "environment-key")
    config = load_formula1_config(_core(tmp_path), tmp_path / "config", dry_run=True)
    assert config["providers"]["flickr_enabled"] is True
    assert config["providers"]["flickr_api_key"] == "environment-key"


def test_flickr_config_is_optional_and_endpoint_is_pinned(tmp_path, monkeypatch):
    monkeypatch.delenv("FORMULA1_FLICKR_API_KEY", raising=False)
    config = load_formula1_config(_core(tmp_path), tmp_path / "config", dry_run=True)
    assert config["providers"]["flickr_enabled"] is False
    private = tmp_path / "bad/formula1"
    private.mkdir(parents=True)
    (private / "formula1.yml").write_text(
        "providers:\n  flickr_url: https://example.com/rest\n", encoding="utf-8"
    )
    try:
        load_formula1_config(_core(tmp_path), tmp_path / "bad", dry_run=True)
    except Formula1ConfigError as error:
        assert "official Flickr" in str(error)
    else:
        raise AssertionError("unsafe Flickr endpoint was accepted")


def test_flickr_licence_registry_excludes_sharealike_and_reserved():
    assert _allowed_licences(_licence_payload()) == {
        "4": {
            "name": "Attribution License",
            "url": "https://creativecommons.org/licenses/by/2.0/",
        }
    }


def test_flickr_team_candidate_requires_year_team_and_formula_one(tmp_path):
    config = load_formula1_config(_core(tmp_path), tmp_path / "config", dry_run=True)
    ferrari = ConstructorData("ferrari", "Ferrari")
    mclaren = ConstructorData("mclaren", "McLaren")
    licences = _allowed_licences(_licence_payload())
    payload = {
        "photos": {
            "photo": [
                _photo(),
                _photo(id="2", title="Ferrari road car", tags="ferrari road car"),
                _photo(id="3", title="2025 Ferrari F1 race car"),
                _photo(id="4", title="2026 McLaren Formula 1 race car"),
                _photo(id="5", license="0"),
            ]
        }
    }
    candidates = parse_flickr_team_candidates(
        payload, 2026, ferrari, [ferrari, mclaren], config, licences
    )
    assert [candidate.page_id for candidate in candidates] == [54900123456]
    assert candidates[0].provider == "flickr"


def test_flickr_background_requires_event_or_circuit_and_action(tmp_path):
    config = load_formula1_config(_core(tmp_path), tmp_path / "config", dry_run=True)
    licences = _allowed_licences(_licence_payload())
    payload = {
        "photos": {
            "photo": [
                _photo(),
                _photo(
                    id="2",
                    title="2026 Formula 1 race car action",
                    description={"_content": "F1 car racing at an unknown circuit"},
                    tags="formula1 f1 race car racing action",
                ),
                _photo(
                    id="3",
                    title="2026 Australian Grand Prix safety car",
                    tags="formula1 safety car albert park race action",
                ),
            ]
        }
    }
    diagnostics = []
    candidates = parse_flickr_background_candidates(
        payload, _race(), config, licences, diagnostics
    )
    assert [candidate.page_id for candidate in candidates] == [54900123456]
    assert candidates[0].provider == "flickr"
    assert {value["reason"] for value in diagnostics} == {
        "event-circuit-year-mismatch",
        "rejected-subject-or-series",
    }


def test_flickr_background_accepts_capture_date_as_safe_year_evidence(tmp_path):
    config = load_formula1_config(_core(tmp_path), tmp_path / "config", dry_run=True)
    licences = _allowed_licences(_licence_payload())
    photo = _photo(
        title="Ferrari Formula 1 race car at the Australian Grand Prix",
        description={
            "_content": "F1 car racing on track at Albert Park during qualifying action"
        },
        tags="f1 formula1 race car racing action albert park melbourne",
        datetaken="2026-03-07 15:22:10",
    )

    candidates = parse_flickr_background_candidates(
        {"photos": {"photo": [photo]}}, _race(), config, licences
    )

    assert len(candidates) == 1
    assert "capture-season" in candidates[0].evidence


def test_flickr_queries_are_role_specific_and_extensive():
    race_queries = _background_queries(_race())
    team_queries = _team_queries(2026, ConstructorData("ferrari", "Ferrari"))
    assert len(race_queries) >= 8
    assert any("wheel to wheel" in query for query in race_queries)
    assert any("track atmosphere" in query for query in race_queries)
    assert len(team_queries) >= 5
    assert all("Ferrari" in query for query in team_queries)


def test_flickr_static_image_hosts_are_restricted():
    assert _safe_image_url("https://live.staticflickr.com/1/photo.jpg")
    assert _safe_image_url("https://farm66.staticflickr.com/1/photo.jpg")
    assert not _safe_image_url("https://example.com/photo.jpg")
    assert not _safe_image_url("http://live.staticflickr.com/1/photo.jpg")


def test_flickr_photo_size_mime_and_common_field_fallbacks():
    licences = _allowed_licences(_licence_payload())
    assert _photo_dimensions({"url_l": "large", "width_l": 1600, "height_l": 900}) == (
        "large",
        1600,
        900,
    )
    assert _photo_dimensions({}) == ("", 0, 0)
    assert _mime({"originalformat": "png"}, "photo") == "image/png"
    assert _mime({"originalformat": "webp"}, "photo") == "image/webp"
    assert _mime({}, "photo.gif") == ""
    assert _common_photo_fields(_photo(license="0"), licences) is None
    assert _common_photo_fields(_photo(ownername=""), licences)["author"] == "owner-nsid"


def test_flickr_transport_error_never_exposes_api_key():
    class BrokenSession:
        def get(self, *_args, **_kwargs):
            raise OSError("https://api.flickr.test/?api_key=top-secret")

    config = {
        "providers": {
            "flickr_url": "https://api.flickr.com/services/rest",
            "flickr_api_key": "top-secret",
            "retries": 1,
        }
    }
    with pytest.raises(FlickrError) as raised:
        asyncio.run(_flickr_json(BrokenSession(), config, "method", {}))
    assert "top-secret" not in str(raised.value)


@pytest.mark.parametrize(
    "response",
    [
        Response(429, {}),
        Response(503, {}),
        Response(200, []),
        Response(200, {"stat": "fail", "code": 100}),
    ],
)
def test_flickr_http_and_payload_failures_are_sanitized(response, monkeypatch):
    monkeypatch.setattr("extensions.formula1.flickr.FLICKR_REQUEST_INTERVAL_SECONDS", 0)
    config = {
        "providers": {
            "flickr_url": "https://api.flickr.com/services/rest",
            "flickr_api_key": "secret",
            "retries": 1,
        }
    }
    with pytest.raises(FlickrError, match="bounded retries"):
        asyncio.run(_flickr_json(Session(response), config, "method", {}))


def test_flickr_request_success_and_retry(monkeypatch):
    monkeypatch.setattr("extensions.formula1.flickr.FLICKR_REQUEST_INTERVAL_SECONDS", 0)
    config = {
        "providers": {
            "flickr_url": "https://api.flickr.com/services/rest",
            "flickr_api_key": "secret",
            "retries": 2,
        }
    }
    session = Session(OSError("temporary"), Response(200, {"stat": "ok", "value": 1}))
    assert asyncio.run(_flickr_json(session, config, "method", {}))["value"] == 1
    assert session.calls[-1][1]["params"]["api_key"] == "secret"


def test_flickr_licence_cache_live_and_stale(monkeypatch):
    payload = _licence_payload()
    config = {"providers": {"flickr_cache_hours": 24}}
    logger = SimpleNamespace(warning=lambda *_args: None)
    allowed, source = asyncio.run(_licences(None, MemoryState(payload), config, logger))
    assert source == "cache" and "4" in allowed

    async def live(*_args):
        return payload

    monkeypatch.setattr("extensions.formula1.flickr._flickr_json", live)
    state = MemoryState()
    allowed, source = asyncio.run(_licences(None, state, config, logger))
    assert source == "flickr" and state.saved[-1] == 24

    async def broken(*_args):
        raise FlickrError("unavailable")

    monkeypatch.setattr("extensions.formula1.flickr._flickr_json", broken)
    allowed, source = asyncio.run(_licences(None, MemoryState(stale=payload), config, logger))
    assert source == "stale-cache" and "4" in allowed
    with pytest.raises(FlickrError):
        asyncio.run(_licences(None, MemoryState(), config, logger))

    async def empty(*_args):
        return {"licenses": {"license": []}}

    monkeypatch.setattr("extensions.formula1.flickr._flickr_json", empty)
    with pytest.raises(FlickrError, match="no permitted"):
        asyncio.run(_licences(None, MemoryState(), config, logger))


def test_flickr_background_rejection_reasons_and_recent_tier(tmp_path):
    config = load_formula1_config(_core(tmp_path), tmp_path / "config", dry_run=True)
    licences = _allowed_licences(_licence_payload())
    base = _photo()
    photos = [
        _photo(id="10", license="0"),
        _photo(id="11", width_o="700", height_o="400"),
        _photo(id="12", width_o="2000", height_o="2000"),
        _photo(
            id="13",
            title="2026 Australian Grand Prix concert",
            description={"_content": "Music at Albert Park"},
            tags="concert albert park",
        ),
        _photo(
            **{
                **base,
                "id": "14",
                "title": "2024 Formula 1 race car Albert Park Grand Prix Circuit",
                "description": {"_content": "F1 car racing on track at Albert Park"},
            }
        ),
    ]
    diagnostics = []
    candidates = parse_flickr_background_candidates(
        {"photos": {"photo": photos}}, _race(), config, licences, diagnostics
    )
    assert candidates[0].match_tier == "recent_circuit_action_race_car"
    assert {item["reason"] for item in diagnostics} >= {
        "unsafe-url-identity-or-licence",
        "undersized",
        "incompatible-aspect-ratio",
        "not-formula-one-race-action",
    }


def test_flickr_search_cache_live_dedupe_and_stale(monkeypatch):
    config = {"providers": {"flickr_cache_hours": 24}}
    payload = {"photos": {"photo": [{"id": "1"}, {"id": "1"}, {"id": ""}]}}
    parser = lambda value: value["photos"]["photo"]
    logger = SimpleNamespace(warning=lambda *_args: None)
    cached, source = asyncio.run(
        _search(None, MemoryState(payload), config, "key", (), {"4": {}}, parser, logger)
    )
    assert source == "cache" and len(cached) == 3

    async def live(*_args):
        return payload

    monkeypatch.setattr("extensions.formula1.flickr._flickr_json", live)
    state = MemoryState()
    found, source = asyncio.run(
        _search(None, state, config, "key", ("one", "two"), {"4": {}}, parser, logger)
    )
    assert source == "flickr" and [item["id"] for item in found] == ["1"]
    assert state.saved is not None

    refreshed_state = MemoryState(
        current={"photos": {"photo": [{"id": "cached"}]}}
    )
    refreshed, source = asyncio.run(
        _search(
            None,
            refreshed_state,
            config,
            "key",
            ("one",),
            {"4": {}},
            parser,
            logger,
            refresh=True,
        )
    )
    assert source == "flickr" and [item["id"] for item in refreshed] == ["1"]

    async def broken(*_args):
        raise FlickrError("offline")

    monkeypatch.setattr("extensions.formula1.flickr._flickr_json", broken)
    found, source = asyncio.run(
        _search(None, MemoryState(stale=payload), config, "key", ("one",), {"4": {}}, parser, logger)
    )
    assert source == "stale-cache" and len(found) == 3
    with pytest.raises(FlickrError):
        asyncio.run(
            _search(None, MemoryState(), config, "key", ("one",), {"4": {}}, parser, logger)
        )


def test_flickr_public_search_wrappers(monkeypatch, tmp_path):
    config = load_formula1_config(_core(tmp_path), tmp_path / "config", dry_run=True)
    config["providers"].update(flickr_enabled=True, flickr_api_key="key")
    licences = _allowed_licences(_licence_payload())
    constructor = ConstructorData("ferrari", "Ferrari")
    logger = SimpleNamespace(debug=lambda *_args: None, warning=lambda *_args: None)

    async def licence_result(*_args):
        return licences, "cache"

    async def search_result(*_args, **_kwargs):
        parser = _args[-2]
        return parser({"photos": {"photo": [_photo(), _photo(id="99", license="0")]}}), "cache"

    monkeypatch.setattr("extensions.formula1.flickr._licences", licence_result)
    monkeypatch.setattr("extensions.formula1.flickr._search", search_result)
    teams, team_source = asyncio.run(
        search_flickr_team_photos(
            None, None, config, 2026, constructor, [constructor], logger
        )
    )
    backgrounds, background_source = asyncio.run(
        search_flickr_backgrounds(None, None, config, _race(), logger)
    )
    assert teams[0].provider == "flickr" and "licences=cache" in team_source
    assert backgrounds[0].provider == "flickr" and "licences=cache" in background_source
    config["providers"]["flickr_enabled"] = False
    assert asyncio.run(
        search_flickr_team_photos(None, None, config, 2026, constructor, [constructor], logger)
    ) == ([], "disabled")
    assert asyncio.run(
        search_flickr_backgrounds(None, None, config, _race(), logger)
    ) == ([], "disabled")


def test_show_source_prefers_flickr_before_commons(monkeypatch, tmp_path):
    constructor = ConstructorData("ferrari", "Ferrari")
    candidate = CommonsCandidate(
        page_id=1,
        title="2026 Ferrari F1 race car",
        page_url="https://www.flickr.com/photos/owner/1",
        image_url="https://live.staticflickr.com/1/photo.jpg",
        width=3200,
        height=1800,
        mime="image/jpeg",
        source_sha1="source-sha1",
        author="Photographer",
        licence="Attribution License",
        licence_url="https://creativecommons.org/licenses/by/2.0/",
        constructor_id="ferrari",
        constructor_name="Ferrari",
        score=100,
        provider="flickr",
    )
    calls = []

    async def roster(*_args):
        return [constructor], "cache"

    async def flickr(*_args):
        calls.append("flickr")
        return [candidate], "flickr"

    async def commons(*_args):
        calls.append("commons")
        raise AssertionError("Commons should not run after a valid Flickr match")

    async def acquire(*_args):
        return tmp_path / "photo.jpg", "flickr"

    monkeypatch.setattr(show_artwork_module, "load_constructors", roster)
    monkeypatch.setattr(show_artwork_module, "search_flickr_team_photos", flickr)
    monkeypatch.setattr(show_artwork_module, "search_commons", commons)
    monkeypatch.setattr(show_artwork_module, "acquire_candidate_image", acquire)
    selected, _path, sources = asyncio.run(
        show_artwork_module._select_source(
            object(), object(), {}, 2026, None, 1, SimpleNamespace()
        )
    )
    assert selected.provider == "flickr"
    assert sources["search"] == "flickr"
    assert calls == ["flickr"]
