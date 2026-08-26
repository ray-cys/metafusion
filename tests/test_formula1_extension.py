import asyncio
import logging
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from PIL import Image

from extensions.formula1 import config as formula1_config_module
from extensions.formula1 import state as formula1_state_module
from extensions.formula1 import verification as formula1_verification_module
from extensions.formula1.artwork import (
    COUNTRY_FLAG_CODES,
    FLAG_ALPHA,
    FLAG_ASSET_ROOT,
    RENDERER_VERSION,
    _country_key,
    _fit,
    _flag_overlay,
    _font,
    _render_background,
    artwork_fingerprint,
    branding_fingerprint,
    country_flag_asset,
    fitted_font,
    render_round_poster,
    svg_path_points,
    validate_branding,
)
from extensions.formula1.config import (
    Formula1ConfigError,
    formula1_requested,
    load_formula1_config,
    sync_formula1_template,
)
from extensions.formula1.facts import (
    FACT_CACHE_VERSION,
    CircuitFacts,
    _cached_facts,
    _canonical_venue,
    _circuit_history,
    _circuit_profile,
    _decode_json_text,
    _first_grand_prix_year,
    _identity_matches,
    _load_official_facts,
    _select_event_slug,
    enrich_race_facts,
    parse_editorial_sections,
    parse_event_slugs,
    parse_official_facts,
)
from extensions.formula1.inventory import (
    canonical_event,
    canonical_program,
    discover_formula1_inventory,
    event_matches_schedule,
    parse_episode_filename,
)
from extensions.formula1.logging import create_formula1_logger, run_identifier
from extensions.formula1.metadata import (
    _merge_preserved_fields,
    _normalize_show_order,
    _ordered_fields,
    build_show_entry,
    write_show_metadata,
)
from extensions.formula1.provider import (
    _get,
    _load_shape_manifest,
    _manifest_names,
    _number,
    _response_json,
    _select_shape_slug,
    load_circuit_path,
    load_schedule,
    parse_schedule,
)
from extensions.formula1.runner import (
    _authoritative_children,
    _managed_artwork_action,
    _write_issues,
    partition_formula1_sections,
    run_formula1_extension,
)
from extensions.formula1.show_artwork import ShowArtworkResult
from extensions.formula1.state import Formula1State, Formula1StateError
from extensions.formula1.verification import (
    _selected_path,
    _verify_artwork,
    queue_application_verification,
    verify_due_applications,
)
from modules.kometa import validate_generated_metadata, validate_metadata_document


class Episode:
    def __init__(self, index, path, key=None):
        self.index = index
        self.locations = [str(path)] if path else []
        self.ratingKey = key or f"e{index}"


class Season:
    def __init__(self, index, episodes):
        self.index = index
        self._episodes = episodes

    def episodes(self):
        return self._episodes


class Show:
    def __init__(self, title, seasons, year=None, key="show1"):
        self.title = title
        self.year = year
        self.ratingKey = key
        self._seasons = seasons

    def seasons(self):
        return self._seasons


class Section:
    type = "show"

    def __init__(self, title, shows):
        self.title = title
        self._shows = shows

    def all(self):
        return self._shows


class Response:
    def __init__(self, status=200, payload=None, text=""):
        self.status = status
        self.payload = payload
        self.body = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self, **_kwargs):
        return self.payload

    async def text(self):
        return self.body


OFFICIAL_CALENDAR = '<a href="/en/racing/2026/australia">Australia</a>'
OFFICIAL_FACTS = (
    r"\"trackLength\":\"5.278\","
    r"\"scheduledLapCount\":\"58\","
    r"\"scheduledDistance\":\"306.124\","
    r"\"circuitOfficialName\":\"Albert Park Grand Prix Circuit\","
    r"\"circuitLocation\":\"Melbourne\","
    r"\"heading\":\"When was the Albert Park Circuit built?\","
    r"\"content\":\"The circuit was created using existing roads around Albert Park.\","
    r"\"heading\":\"When was its first Grand Prix?\","
    r"\"content\":\"Albert Park first hosted the race in 1996.\","
    r"\"heading\":\"What’s the circuit like?\","
    r"\"content\":\"A fast, flowing, high-speed circuit with heavy-braking zones.\""
)


class Session:
    def __init__(
        self,
        schedule,
        svg,
        *,
        fail=False,
        calendar=OFFICIAL_CALENDAR,
        facts=OFFICIAL_FACTS,
        manifest=None,
    ):
        self.schedule = schedule
        self.svg = svg
        self.fail = fail
        self.calendar = calendar
        self.facts = facts
        self.manifest = manifest or [{"name": "madring-1.svg"}]
        self.urls = []

    def get(self, url, **_kwargs):
        self.urls.append(url)
        if self.fail:
            return Response(503)
        if "api.github.com" in url:
            return Response(payload=self.manifest)
        if "formula1.com" in url:
            is_calendar = url.rstrip("/").endswith("/2026")
            return Response(text=self.calendar if is_calendar else self.facts)
        if url.endswith(".svg"):
            return Response(text=self.svg)
        return Response(payload=self.schedule)


@pytest.fixture
def core(tmp_path):
    return {
        "settings": {"mode": "kometa", "path": str(tmp_path / "kometa"), "dry_run": False},
        "runtime": {"plex_retries": 1, "plex_retry_delay": 0},
    }


@pytest.fixture
def schedule_payload():
    return {
        "MRData": {
            "RaceTable": {
                "Races": [
                    {
                        "round": "1",
                        "raceName": "Australian Grand Prix",
                        "date": "2026-03-08",
                        "Sprint": {"date": "2026-03-07"},
                        "FirstPractice": {"date": "2026-03-06"},
                        "Qualifying": {"date": "2026-03-07"},
                        "Circuit": {
                            "circuitId": "albert_park",
                            "circuitName": "Albert Park Grand Prix Circuit",
                            "Location": {
                                "locality": "Melbourne",
                                "country": "Australia",
                                "lat": "-37.8497",
                                "long": "144.968",
                            },
                        },
                    }
                ]
            }
        }
    }


def test_opt_in_is_strict_and_partition_isolated(core):
    sections = [Section("Movies", []), Section("Formula 1", [])]
    assert not formula1_requested(core, {})
    assert not formula1_requested({"settings": {"mode": "plex"}}, {"FORMULA1_ENABLED": "true"})
    regular, formula = partition_formula1_sections(sections, core, {"FORMULA1_ENABLED": "yes"})
    assert [item.title for item in regular] == ["Movies"]
    assert [item.title for item in formula] == ["Formula 1"]


def test_config_is_private_validated_and_dry_run_safe(tmp_path, core):
    root = tmp_path / "config"
    config = load_formula1_config(core, root)
    assert (root / "formula1" / "formula1_template.yml").exists()
    assert config["paths"]["database"].name == "formula1.sqlite3"
    assert config["metadata"] == {
        "enabled": True,
        "original_title": "Formula Internationale",
        "originally_available": "1950-05-13",
        "content_rating": "PG-13",
        "studio": "F1TV",
        "tagline": "We race as one.",
        "genre": ["Sport"],
        "round_prefix": True,
        "shorten_gp": False,
    }
    assert sync_formula1_template(root / "formula1") is False
    config_path = root / "formula1" / "formula1.yml"
    config_path.write_text("artwork:\n  width: 999\n", encoding="utf-8")
    with pytest.raises(Formula1ConfigError, match="2:3"):
        load_formula1_config(core, root)
    dry_root = tmp_path / "dry"
    load_formula1_config(core, dry_root, dry_run=True)
    assert not dry_root.exists()


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("- not-a-map\n", "root"),
        ("artwork:\n  width: nope\n", "integer"),
        ("providers:\n  cache_hours: nope\n", "numeric"),
        ("library:\n  name: '  '\n", "must not be empty"),
        ("library:\n  naming_profile: bad\n", "naming_profile"),
        ("artwork:\n  policy: overwrite\n", "policy"),
        ("providers:\n  jolpica_url: http://unsafe\n", "jolpica"),
        ("providers:\n  formula1_url: http://unsafe\n", "formula1_url"),
        ("providers:\n  circuit_svg_url: http://unsafe\n", "circuit_svg_url"),
        ("providers:\n  circuit_manifest_url: http://unsafe\n", "circuit_manifest_url"),
        ("logging:\n  level: TRACE\n", "logging.level"),
        ("logging:\n  console: noisy\n", "logging.console"),
    ],
)
def test_config_rejects_each_unsafe_surface(tmp_path, core, document, message):
    root = tmp_path / "config/formula1"
    root.mkdir(parents=True)
    (root / "formula1.yml").write_text(document, encoding="utf-8")
    with pytest.raises(Formula1ConfigError, match=message):
        load_formula1_config(core, tmp_path / "config", dry_run=True)


def test_config_rejects_invalid_yaml_and_numeric_ranges(tmp_path, core):
    root = tmp_path / "config/formula1"
    root.mkdir(parents=True)
    active = root / "formula1.yml"
    active.write_text("[broken", encoding="utf-8")
    with pytest.raises(Formula1ConfigError, match="Unable to read"):
        load_formula1_config(core, tmp_path / "config", dry_run=True)
    for document in (
        "providers:\n  retries: 99\n",
        "cleanup:\n  grace_hours: 99999\n",
        "logging:\n  retention: 0\n",
    ):
        active.write_text(document, encoding="utf-8")
        with pytest.raises(Formula1ConfigError, match="between"):
            load_formula1_config(core, tmp_path / "config", dry_run=True)


def test_config_path_and_template_failure_cleanup(tmp_path, core, monkeypatch):
    from extensions.formula1.config import _resolve_under

    with pytest.raises(Formula1ConfigError, match="must stay under"):
        _resolve_under(tmp_path, tmp_path.parent / "escape", "test")
    root = tmp_path / "formula1"
    monkeypatch.setattr(
        formula1_config_module,
        "atomic_replace_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace")),
    )
    with pytest.raises(OSError, match="replace"):
        sync_formula1_template(root)
    assert not list(root.glob("*.tmp"))


def test_branding_validation_fingerprints_and_text_fit(tmp_path, core):
    config = load_formula1_config(core, tmp_path / "config")
    warnings = validate_branding(config)
    assert len(warnings) == 3
    branding = config["paths"]["branding"]
    branding.mkdir(parents=True)
    logo = branding / "logo.png"
    Image.new("RGBA", (240, 80), (200, 20, 40, 255)).save(logo)
    before = branding_fingerprint(config)
    Image.new("RGBA", (260, 90), (220, 20, 40, 255)).save(logo)
    assert branding_fingerprint(config)["logo"]["sha256"] != before["logo"]["sha256"]
    assert len(validate_branding(config)) == 2
    (branding / "font-bold.ttf").write_bytes(b"not a font")
    with pytest.raises(ValueError, match="font is unreadable"):
        validate_branding(config)
    fitted = fitted_font(None, "A very long circuit name that must fit", 60, 12, 150)
    assert fitted.getbbox("test")[2] > 0
    logo.unlink()
    Image.new("RGBA", (20, 10), (255, 0, 0, 255)).save(logo)
    (branding / "font-bold.ttf").unlink()
    with pytest.raises(ValueError, match="logo is unreadable or unsuitable"):
        validate_branding(config)


def test_event_schedule_match_and_unknown_program_warning(
    tmp_path, core, schedule_payload
):
    race = parse_schedule(schedule_payload, 2026)[0]
    assert event_matches_schedule("Australia Grand Prix", race)
    assert not event_matches_schedule("Bahrain Grand Prix", race)
    media = tmp_path / "S01E01 - Australia Grand Prix - Studio.Special.mkv"
    section = Section("Formula 1", [Show("F1 2026", [Season(1, [Episode(1, media)])])])
    config = load_formula1_config(core, tmp_path / "config")
    result = asyncio.run(
        discover_formula1_inventory(
            section,
            {"runtime": core["runtime"], "formula1": config},
            logging.getLogger("unknown-program"),
        )
    )
    assert result.shows[0].episodes[0].program_kind == "other"
    assert any("Unrecognized programme label" in issue for issue in result.issues)


@pytest.mark.parametrize(
    ("filename", "episode", "profile", "event", "kind"),
    [
        (
            "S01E02 - Australia Grand Prix - FP1.mkv",
            2,
            "current",
            "Australian Grand Prix",
            "practice1",
        ),
        (
            "01x05 - Bahrein GP - Qualifying Session.mkv",
            5,
            "kometa",
            "Bahrain Grand Prix",
            "qualifying",
        ),
        (
            "01x06 - British GP - Post-Qualyfing Analysis.mkv",
            6,
            "kometa",
            "British Grand Prix",
            "post_qualifying",
        ),
    ],
)
def test_dual_filename_parser(filename, episode, profile, event, kind):
    parsed = parse_episode_filename(filename, 1, episode)
    assert parsed["profile"] == profile
    assert parsed["event"] == event
    assert parsed["program_kind"] == kind


def test_parser_rejects_mismatch_and_normalizes_unknown():
    with pytest.raises(ValueError, match="does not match"):
        parse_episode_filename("S02E01 - Japan GP - Race.mkv", expected_season=1)
    with pytest.raises(ValueError, match="unsupported"):
        parse_episode_filename("random.mkv")
    assert canonical_event("USA GP") == "United States Grand Prix"
    assert canonical_program("Drivers Parade") == ("Drivers Parade", "other")
    with pytest.raises(ValueError, match="episode"):
        parse_episode_filename("S01E02 - Japan GP - Race.mkv", expected_episode=1)
    assert parse_episode_filename("S01E01 - Japan GP - Race.mkv", profile="current")["season"] == 1


def test_inventory_ignores_non_race_season_and_all_ambiguous_duplicates(tmp_path):
    path = tmp_path / "S01E01 - Australia GP - Race.mkv"
    duplicate = tmp_path / "01x01 - Australian GP - Highlights.mkv"
    section = Section(
        "Formula 1",
        [
            Show("F1 2026", [Season(0, [Episode(1, path)]), Season(1, [Episode(1, path)])]),
            Show("Formula 1 2026", [Season(1, [Episode(1, duplicate)])], key="show2"),
            Show("No year", []),
        ],
    )
    result = asyncio.run(
        discover_formula1_inventory(
            section,
            {
                "runtime": {"plex_retries": 1},
                "formula1": {"library": {"naming_profile": "auto"}},
            },
            logging.getLogger("f1-test-inventory"),
        )
    )
    assert result.shows == []
    assert any("Duplicate" in issue for issue in result.issues)
    assert any("championship year" in issue for issue in result.issues)


def test_inventory_reports_invalid_paths_and_uses_plex_fallbacks(tmp_path):
    class PartEpisode(Episode):
        locations = []

        def __init__(self, index, path):
            self.index = index
            self.ratingKey = "part"
            self._path = path

        def iterParts(self):
            return [type("Part", (), {"file": str(self._path)})()]

    valid = tmp_path / "S01E01 - Japan GP - Race.mkv"
    section = Section(
        "Formula 1",
        [
            Show(
                "Championship",
                [
                    Season(-1, []),
                    Season(0, [Episode(1, tmp_path / "ignored.mkv")]),
                    Season(1, [PartEpisode(1, valid), Episode(2, None), Episode(3, tmp_path / "bad.mkv")]),
                ],
                year=2026,
            )
        ],
    )
    result = asyncio.run(
        discover_formula1_inventory(
            section,
            {
                "runtime": {"plex_retries": 1},
                "formula1": {"library": {"naming_profile": "auto"}},
            },
            logging.getLogger("inventory-fallbacks"),
        )
    )
    assert len(result.shows[0].episodes) == 1
    assert any("invalid season" in issue for issue in result.issues)
    assert any("Missing media path" in issue for issue in result.issues)
    assert any("unsupported" in issue for issue in result.issues)


def test_state_cache_bindings_cleanup_and_schema(tmp_path):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state = Formula1State(tmp_path / "formula1.sqlite3")
    state.cache_put("test", "key", {"ok": True}, 1, now=now)
    assert state.cache_get("test", "key", now=now) == {"ok": True}
    assert state.cache_get("test", "key", now=now + timedelta(hours=2)) is None
    assert state.cache_get("test", "key", allow_expired=True, now=now + timedelta(hours=2)) == {
        "ok": True
    }
    state.bind("2026:r01:e01", "1", "/media/test.mkv", "Race", "current", now=now)
    state.save_artwork("2026:r01", "/art/poster.png", "fp", "sum", now=now)
    state.save_episode_round_source(
        2026,
        1,
        "alpine",
        {"candidate": {"constructor_name": "Alpine F1 Team"}},
        now=now,
    )
    state.start_run("run", now=now)
    state.finish_run("run", "success", {"ok": True}, now=now)
    assert state.artwork("2026:r01")["fingerprint"] == "fp"
    assert state.episode_round_source(2026, 1)["constructor_id"] == "alpine"
    assert len(state.episode_round_sources()) == 1
    assert (
        state.reconcile_bindings(set(), cleanup=True, confirmation_scans=2, grace_hours=1, now=now)
        == []
    )
    stale = state.reconcile_bindings(
        set(), cleanup=True, confirmation_scans=2, grace_hours=1, now=now + timedelta(hours=2)
    )
    assert stale[0]["logical_key"] == "2026:r01:e01"
    state.remove_binding("2026:r01:e01", {"reason": "test"}, now=now)
    assert state.bindings() == {}
    state.close()
    connection = sqlite3.connect(tmp_path / "bad.sqlite3")
    connection.execute("CREATE TABLE schema_info(version INTEGER NOT NULL)")
    connection.execute("INSERT INTO schema_info VALUES (999)")
    connection.commit()
    connection.close()
    with pytest.raises(Formula1StateError, match="schema"):
        Formula1State(tmp_path / "bad.sqlite3")


def test_state_wraps_sqlite_initialization_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        formula1_state_module.sqlite3,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.Error("broken")),
    )
    with pytest.raises(Formula1StateError, match="Unable to initialize"):
        Formula1State(tmp_path / "broken.sqlite3")

    class BrokenConnection:
        row_factory = None
        closed = False

        def execute(self, *_args):
            raise sqlite3.Error("pragma failed")

        def close(self):
            self.closed = True

    connection = BrokenConnection()
    monkeypatch.setattr(
        formula1_state_module.sqlite3,
        "connect",
        lambda *_args, **_kwargs: connection,
    )
    with pytest.raises(Formula1StateError, match="Unable to initialize"):
        Formula1State(tmp_path / "pragma.sqlite3")
    assert connection.closed is True


def test_provider_validates_caches_and_falls_back(tmp_path, core, schedule_payload):
    config = load_formula1_config(core, tmp_path / "config")
    state = Formula1State(config["paths"]["database"])
    svg = '<svg><path d="M0 0 L100 0 L100 100 Z"/></svg>'
    session = Session(schedule_payload, svg)
    races, source = asyncio.run(
        load_schedule(session, state, config, 2026, logging.getLogger("provider"))
    )
    assert source == "jolpica" and races[0].sprint
    assert (
        asyncio.run(load_schedule(session, state, config, 2026, logging.getLogger("provider")))[1]
        == "cache"
    )
    path, source = asyncio.run(
        load_circuit_path(session, state, config, "albert_park", logging.getLogger("provider"))
    )
    assert path.startswith("M0") and source == "f1-circuits-svg"
    assert asyncio.run(
        load_circuit_path(session, state, config, "unknown", logging.getLogger("provider"))
    ) == (None, "unmapped")
    assert len(parse_schedule(schedule_payload, 2026)) == 1
    state.close()


def test_official_facts_discovery_validation_and_cache(tmp_path, core, schedule_payload):
    config = load_formula1_config(core, tmp_path / "config")
    state = Formula1State(config["paths"]["database"])
    race = parse_schedule(schedule_payload, 2026)[0]
    assert parse_event_slugs(OFFICIAL_CALENDAR, 2026) == ["australia"]
    assert parse_event_slugs("nothing", 2026) == []
    assert _select_event_slug(race, ["australia"]) == "australia"
    facts = parse_official_facts(OFFICIAL_FACTS)
    assert facts == CircuitFacts(
        5.278,
        58,
        306.124,
        "Albert Park Grand Prix Circuit",
        "Melbourne",
        1996,
        "fast, flowing, heavy-braking zones",
        "The circuit was created from existing roads.",
    )
    sections = parse_editorial_sections(OFFICIAL_FACTS)
    assert sections["when-was-its-first-grand-prix"].endswith("1996.")
    contextual = OFFICIAL_FACTS.replace(
        "Albert Park first hosted the race in 1996.",
        "After discussions in the 1960s and 1970s, Albert Park first hosted the race in 1996.",
    )
    assert parse_official_facts(contextual).first_grand_prix_year == 1996
    assert parse_editorial_sections("unrelated page") == {}
    assert _cached_facts(None) is None
    assert _cached_facts({}) is None
    normalized = _cached_facts(
        {
            "circuit_length_km": "5.278",
            "lap_count": "58",
            "race_distance_km": "306.124",
        }
    )
    assert normalized == CircuitFacts(5.278, 58, 306.124)
    assert _cached_facts({**asdict(normalized), "circuit_length_km": "bad"}) is None
    assert _cached_facts({**asdict(normalized), "lap_count": 58.5}) is None
    assert _cached_facts({**asdict(normalized), "circuit_length_km": 1}) is None
    assert _cached_facts({**asdict(normalized), "race_distance_km": 500}) is None
    assert _cached_facts({**asdict(normalized), "race_distance_km": 200}) is None
    assert _identity_matches(race, facts)
    assert _identity_matches(
        race, CircuitFacts(5.278, 58, 306.124, "Albert Park Circuit", "Wrong City")
    )
    assert _identity_matches(race, CircuitFacts(5.278, 58, 306.124))
    assert not _identity_matches(
        race, CircuitFacts(5.278, 58, 306.124, "Wrong Raceway", "Wrong City")
    )
    circuit, locality = _canonical_venue(
        race,
        CircuitFacts(5.278, 58, 306.124, "Albert Park Circuit", "Australia"),
    )
    assert circuit == "Albert Park Circuit" and locality == "Melbourne"
    assert _canonical_venue(race, CircuitFacts(5.278, 58, 306.124, "TBC", "to be confirmed")) == (
        race.circuit,
        race.locality,
    )
    visible = (
        "Circuit Length</dt><dd><span>5.278km</span></dd>"
        "Number of Laps</dt><dd>58</dd>"
        "Race Distance</dt><dd>306.124km</dd>"
    )
    assert parse_official_facts(visible).lap_count == 58
    with pytest.raises(RuntimeError, match="omitted"):
        parse_official_facts("Circuit Length")
    with pytest.raises(RuntimeError, match="implausible"):
        parse_official_facts(
            OFFICIAL_FACTS.replace(r"\"scheduledLapCount\":\"58", r"\"scheduledLapCount\":\"1")
        )
    with pytest.raises(RuntimeError, match="inconsistent"):
        parse_official_facts(
            OFFICIAL_FACTS.replace(
                r"\"scheduledDistance\":\"306.124", r"\"scheduledDistance\":\"200"
            )
        )
    session = Session(schedule_payload, "<svg/>")
    enriched, statistics = asyncio.run(
        enrich_race_facts(session, state, config, [race], {1}, logging.getLogger("facts"))
    )
    assert statistics == {
        "resolved": 1,
        "missing": 0,
        "stale": 0,
        "canonicalized": 0,
        "profiles_resolved": 1,
        "profiles_missing": 0,
        "issues": [],
    }
    assert enriched[0].circuit_length_km == 5.278
    assert state.cache_get(
        "formula1.com", f"facts:v{FACT_CACHE_VERSION}:2026:australia"
    ) is not None
    cached, cached_statistics = asyncio.run(
        enrich_race_facts(session, state, config, [race], {1}, logging.getLogger("facts-cache"))
    )
    assert cached[0].lap_count == 58 and cached_statistics["resolved"] == 1
    state.connection.execute(
        "DELETE FROM provider_cache WHERE provider='formula1.com' AND cache_key LIKE 'facts:%'"
    )
    state.connection.commit()
    canonical_document = OFFICIAL_FACTS.replace(
        "Albert Park Grand Prix Circuit", "Albert Park Circuit"
    )
    canonical, canonical_statistics = asyncio.run(
        enrich_race_facts(
            Session(schedule_payload, "", facts=canonical_document),
            state,
            config,
            [race],
            {1},
            logging.getLogger("facts-canonical"),
        )
    )
    assert canonical[0].circuit == "Albert Park Circuit"
    assert canonical_statistics["canonicalized"] == 1
    wrong_facts = OFFICIAL_FACTS.replace("Melbourne", "Wrong City").replace(
        "Albert Park Grand Prix Circuit", "Wrong Raceway"
    )
    with pytest.raises(RuntimeError, match="identity"):
        asyncio.run(
            _load_official_facts(
                Session(schedule_payload, "", facts=wrong_facts),
                state,
                config,
                race,
                "wrong-event",
            )
        )
    state.close()


def test_official_editorial_profile_fallbacks_are_conservative():
    assert _decode_json_text("broken\\") == "broken\\"
    assert _first_grand_prix_year({"when-was-the-first-grand-prix": "First race in 1980."}) == 1980
    assert (
        _first_grand_prix_year(
            {"when-was-its-first-grand-prix": "The 1981 event was the first Grand Prix."}
        )
        == 1981
    )
    assert _first_grand_prix_year({"when-was-its-first-grand-prix": "It happened in 1982."}) == 1982
    assert _first_grand_prix_year({"when-was-its-first-grand-prix": "No date supplied."}) is None
    assert _circuit_profile({"whats-the-circuit-like": "A unique driving challenge."}) is None
    assert _circuit_profile({}) is None

    assert _circuit_history({"when-was-example-built": "In 1962, it was a test track."}) == (
        "The circuit dates to 1962 and originated as a manufacturer test track."
    )
    assert _circuit_history(
        {"when-was-example-built": "Roads on a man-made island formed the circuit."}
    ) == "The circuit was created from roads on a man-made island."
    assert _circuit_history(
        {"when-was-example-built": "The circuit was developed from an airfield."}
    ) == "The circuit was developed from an airfield."
    assert _circuit_history(
        {"when-was-example-built": "It became a purpose-built motorsport venue."}
    ) == "The circuit was developed as a purpose-built venue."
    assert _circuit_history(
        {"when-was-example-built": "Construction began in 1997 before completion."}
    ) == "The circuit saw construction begin in 1997."
    assert _circuit_history({"when-was-example-built": "A story without usable facts."}) is None
    assert _circuit_history({}) is None


def test_official_facts_fail_safe_identity_and_stale_paths(tmp_path, core, schedule_payload):
    config = load_formula1_config(core, tmp_path / "config")
    state = Formula1State(config["paths"]["database"])
    race = parse_schedule(schedule_payload, 2026)[0]
    unknown = type(race)(**{**race.__dict__, "circuit_id": "new_place", "country": "Nowhere"})
    assert _select_event_slug(unknown, ["one", "two"]) is None
    assert _select_event_slug(unknown, ["nowhere"]) == "nowhere"
    token_match = type(race)(
        **{
            **race.__dict__,
            "circuit_id": "new_place",
            "circuit": "Alpha New Raceway",
            "locality": "Elsewhere",
            "country": "Nowhere",
        }
    )
    assert _select_event_slug(token_match, ["alpha-new", "unrelated-place"]) == "alpha-new"
    unchanged, statistics = asyncio.run(
        enrich_race_facts(
            Session(schedule_payload, "", calendar='<a href="/en/racing/2026/one">One</a>'),
            state,
            config,
            [unknown],
            {1},
            logging.getLogger("facts-identity"),
        )
    )
    assert unchanged == [unknown] and statistics["missing"] == 1
    state.connection.execute("DELETE FROM provider_cache")
    state.connection.commit()
    state.cache_put(
        "formula1.com",
        "events:2026",
        {"slugs": ["australia"]},
        1,
        now=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    state.cache_put(
        "formula1.com",
        f"facts:v{FACT_CACHE_VERSION}:2026:australia",
        {
            "circuit_length_km": 5.278,
            "lap_count": 58,
            "race_distance_km": 306.124,
            "circuit": None,
            "locality": None,
        },
        1,
        now=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    stale, statistics = asyncio.run(
        enrich_race_facts(
            Session(schedule_payload, "", fail=True),
            state,
            config,
            [race],
            {1},
            logging.getLogger("facts-versioned-stale"),
        )
    )
    assert stale[0].lap_count == 58 and statistics["stale"] == 1
    state.connection.execute("DELETE FROM provider_cache")
    state.connection.commit()
    state.cache_put(
        "formula1.com",
        "events:2026",
        {"slugs": ["australia"]},
        1,
        now=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    state.cache_put(
        "formula1.com",
        "facts:2026:australia",
        {
            "circuit_length_km": 5.278,
            "lap_count": 58,
            "race_distance_km": 306.124,
            "circuit": None,
            "locality": None,
        },
        1,
        now=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    stale, statistics = asyncio.run(
        enrich_race_facts(
            Session(schedule_payload, "", fail=True),
            state,
            config,
            [race],
            {1},
            logging.getLogger("facts-stale"),
        )
    )
    assert stale[0].lap_count == 58 and statistics["stale"] == 1
    state.connection.execute("DELETE FROM provider_cache")
    state.connection.commit()
    failed, statistics = asyncio.run(
        enrich_race_facts(
            Session(schedule_payload, "", fail=True),
            state,
            config,
            [race],
            {1},
            logging.getLogger("facts-failed"),
        )
    )
    assert failed == [race] and statistics["missing"] == 1
    recovered, statistics = asyncio.run(
        enrich_race_facts(
            Session(schedule_payload, "", calendar=""),
            state,
            config,
            [race],
            {1},
            logging.getLogger("facts-no-calendar"),
        )
    )
    assert recovered[0].lap_count == 58 and statistics["resolved"] == 1
    assert (
        asyncio.run(
            enrich_race_facts(Session({}, ""), state, config, [], set(), logging.getLogger("none"))
        )[0]
        == []
    )
    state.close()


def test_future_circuit_shape_manifest_is_learned(tmp_path, core, schedule_payload):
    config = load_formula1_config(core, tmp_path / "config")
    state = Formula1State(config["paths"]["database"])
    race = parse_schedule(schedule_payload, 2026)[0]
    future = type(race)(
        **{
            **race.__dict__,
            "circuit_id": "madring",
            "circuit": "Madring",
            "locality": "Madrid",
            "country": "Spain",
        }
    )
    assert _manifest_names([{"name": "madring-1.svg"}, {}, "bad"]) == ["madring-1.svg"]
    assert _manifest_names({}) == []
    assert _select_shape_slug(future, ["madring-1.svg"]) == "madring-1"
    session = Session(
        schedule_payload,
        '<svg><path d="M0 0 L10 10"/></svg>',
        manifest=[{"name": "madring-1.svg"}],
    )
    path, source = asyncio.run(
        load_circuit_path(session, state, config, future, logging.getLogger("future-shape"))
    )
    assert path == "M0 0 L10 10" and source == "f1-circuits-svg"
    assert state.cache_get("f1-circuits-svg", "shape-binding:madring")["slug"] == "madring-1"
    assert asyncio.run(_load_shape_manifest(session, state, config)) == ["madring-1.svg"]
    fuzzy = type(future)(
        **{**future.__dict__, "circuit_id": "unknown", "circuit": "New Harbor Circuit"}
    )
    assert _select_shape_slug(fuzzy, ["new-harbor-1.svg", "other-1.svg"]) == "new-harbor-1"
    assert _select_shape_slug(fuzzy, ["unrelated-1.svg"]) is None
    state.close()


def test_future_shape_manifest_failure_and_stale_fallback(tmp_path, core, schedule_payload):
    config = load_formula1_config(core, tmp_path / "config")
    config["providers"]["retries"] = 2
    state = Formula1State(config["paths"]["database"])
    race = parse_schedule(schedule_payload, 2026)[0]
    future = type(race)(**{**race.__dict__, "circuit_id": "future", "circuit": "Future Circuit"})
    empty = Session(schedule_payload, "", manifest=[])
    empty.manifest = []
    with pytest.raises(RuntimeError, match="manifest request failed"):
        asyncio.run(_load_shape_manifest(empty, state, config))
    state.cache_put(
        "f1-circuits-svg",
        "manifest",
        {"names": ["future-1.svg"]},
        1,
        now=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    assert asyncio.run(_load_shape_manifest(Session({}, "", fail=True), state, config)) == [
        "future-1.svg"
    ]
    state.connection.execute("DELETE FROM provider_cache")
    state.connection.commit()
    assert asyncio.run(
        load_circuit_path(
            Session({}, "", fail=True),
            state,
            config,
            future,
            logging.getLogger("manifest-failed"),
        )
    ) == (None, "unmapped")
    state.close()


def test_provider_failure_boundaries_and_stale_fallback(tmp_path, core, schedule_payload):
    config = load_formula1_config(core, tmp_path / "config")
    config["providers"]["retries"] = 2
    state = Formula1State(config["paths"]["database"])
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    state.cache_put("jolpica", "schedule:2026", schedule_payload, 1, now=old)
    state.cache_put("f1-circuits-svg", "circuit:melbourne-2", {"path": "M0 0 L1 1"}, 1, now=old)
    failed = Session(schedule_payload, "", fail=True)
    races, source = asyncio.run(
        load_schedule(failed, state, config, 2026, logging.getLogger("stale"))
    )
    assert races and source == "stale-cache"
    path, source = asyncio.run(
        load_circuit_path(failed, state, config, "albert_park", logging.getLogger("stale"))
    )
    assert path and source == "stale-cache"
    state.connection.execute("DELETE FROM provider_cache")
    state.connection.commit()
    with pytest.raises(RuntimeError, match="unsupported"):
        asyncio.run(load_schedule(failed, state, config, 1900, logging.getLogger("bad-year")))
    with pytest.raises(RuntimeError, match="provider request failed"):
        asyncio.run(load_schedule(failed, state, config, 2026, logging.getLogger("failed")))
    assert (
        asyncio.run(
            load_circuit_path(failed, state, config, "albert_park", logging.getLogger("missing"))
        )[1]
        == "unavailable"
    )
    invalid = {
        "MRData": {
            "RaceTable": {"Races": [{"round": "bad"}, {"round": 1, "Results": [{"laps": "bad"}]}]}
        }
    }
    assert parse_schedule(invalid, 2026)[0].lap_count is None
    assert _number("bad") is None
    state.close()


def test_provider_response_and_text_safety(schedule_payload):
    assert asyncio.run(_response_json(Response(payload=schedule_payload))) == schedule_payload
    with pytest.raises(RuntimeError, match="HTTP"):
        asyncio.run(_response_json(Response(404)))
    with pytest.raises(TypeError, match="invalid JSON"):
        asyncio.run(_response_json(Response(payload=[])))
    oversized = Session(schedule_payload, "x" * 1_000_001)
    with pytest.raises(RuntimeError, match="safety limit"):
        asyncio.run(_get(oversized, "https://test.svg", retries=1, json_response=False))


def test_provider_rejects_empty_schedule_and_invalid_svg(tmp_path, core):
    config = load_formula1_config(core, tmp_path / "config")
    state = Formula1State(config["paths"]["database"])
    empty = Session({"MRData": {"RaceTable": {"Races": []}}}, "<svg/>")
    with pytest.raises(RuntimeError, match="no valid races"):
        asyncio.run(load_schedule(empty, state, config, 2026, logging.getLogger("empty")))
    assert (
        asyncio.run(
            load_circuit_path(empty, state, config, "albert_park", logging.getLogger("svg"))
        )[1]
        == "unavailable"
    )
    state.close()


def test_metadata_and_artwork_rendering(tmp_path, core, schedule_payload):
    config = load_formula1_config(core, tmp_path / "config")
    config["artwork"].update(width=600, height=900)
    race = parse_schedule(schedule_payload, 2026)[0]
    points = svg_path_points("M0 0 h100 v100 l-100 0 z")
    assert len(points) >= 4
    destination = tmp_path / "poster.png"
    checksum = render_round_poster(race, "M0 0 C20 100 80 100 100 0 Z", config, destination)
    assert len(checksum) == 64
    with Image.open(destination) as image:
        assert image.size == (600, 900)
        assert sum(image.getpixel((580, 850))) < sum(image.getpixel((580, 100)))
    episode_path = tmp_path / "S01E01 - Australia GP - Race.mkv"
    inventory = __import__(
        "extensions.formula1.inventory", fromlist=["Formula1Episode", "Formula1Show"]
    )
    episode = inventory.Formula1Episode(
        2026, 1, 1, "Australian Grand Prix", "Race Session", "race", episode_path, "1", "current"
    )
    show = inventory.Formula1Show(2026, "F1 2026", "show", [episode])
    generated, seasons, episodes = build_show_entry(
        show,
        [race],
        {1: "/config/poster.png"},
        config,
        episode_poster_references={(1, 1): "/config/episode.png"},
    )
    assert generated["match"]["title"] == ["F1 2026", "Formula 1 (2026)"]
    assert generated["title"] == "Formula 1 (2026)"
    assert generated["sort_title"] == "Formula 1 (2026)"
    assert generated["original_title"] == "Formula Internationale"
    assert generated["originally_available"] == "1950-05-13"
    assert generated["tagline"] == "We race as one."
    assert generated["genre"] == ["Sport"]
    assert "visible_library" not in generated
    assert "to be confirmed" not in generated["seasons"][1]["summary"]
    assert "Circuit length" not in generated["seasons"][1]["summary"]
    assert generated["seasons"][1]["episodes"][1]["originally_available"] == "2026-03-08"
    assert generated["seasons"][1]["episodes"][1]["file_poster"] == "/config/episode.png"
    assert seasons == {1} and episodes == {1: {1}}
    assert validate_generated_metadata(generated, "show")
    profiled_race = type(race)(
        **{
            **race.__dict__,
            "circuit_length_km": 5.278,
            "lap_count": 58,
            "race_distance_km": 306.124,
            "first_grand_prix_year": 1996,
            "circuit_profile": "fast, flowing, heavy-braking zones",
            "circuit_history": "The circuit was created from existing roads.",
        }
    )
    profiled, _seasons, _episodes = build_show_entry(
        show, [profiled_race], {1: "/config/poster.png"}, config
    )
    summary = profiled["seasons"][1]["summary"]
    episode_summary = profiled["seasons"][1]["episodes"][1]["summary"]
    expected_facts = (
        "The circuit measures 5.278 km; the scheduled race runs for 58 laps "
        "and covers 306.124 km."
    )
    assert expected_facts in summary
    assert expected_facts in episode_summary
    assert "The circuit was created from existing roads" in summary
    assert "first hosted a Formula 1 Grand Prix in 1996" in summary
    assert "Formula1.com circuit profile: fast, flowing" in summary
    for field, value, expected in (
        ("circuit_length_km", 5.278, "Circuit length: 5.278 km"),
        ("lap_count", 58, "Lap count: 58"),
        ("race_distance_km", 306.124, "Race distance: 306.124 km"),
    ):
        partial_race = type(race)(**{**race.__dict__, field: value})
        partial = build_show_entry(
            show, [partial_race], {1: "/config/poster.png"}, config
        )[0]
        assert expected in partial["seasons"][1]["summary"]
    path, changed, _diagnostics = write_show_metadata(
        show,
        [profiled_race],
        {1: "/config/poster.png"},
        config,
        show_artwork={
            "poster": "/config/show-poster.png",
            "background": "/config/show-background.png",
        },
        episode_poster_references={(1, 1): "/config/episode.png"},
    )
    persisted = yaml.safe_load(path.read_text())
    assert changed and validate_metadata_document(persisted, "tv")
    persisted_show = persisted["metadata"]["F1 2026"]
    assert list(persisted_show) == [
        "match",
        "title",
        "sort_title",
        "original_title",
        "originally_available",
        "content_rating",
        "studio",
        "tagline",
        "summary",
        "genre",
        "file_poster",
        "file_background",
        "f1_season",
        "round_prefix",
        "shorten_gp",
        "seasons",
    ]
    persisted_season = persisted_show["seasons"][1]
    assert list(persisted_season) == ["title", "summary", "file_poster", "episodes"]
    assert list(persisted_season["episodes"][1]) == [
        "title",
        "originally_available",
        "summary",
        "file_poster",
    ]
    assert expected_facts in persisted_season["summary"]
    assert expected_facts in persisted_season["episodes"][1]["summary"]
    assert (
        write_show_metadata(show, [profiled_race], {1: "/config/poster.png"}, config)[1]
        is False
    )
    canonical_show = inventory.Formula1Show(
        2026, "Formula 1 (2026)", "show", [episode]
    )
    assert (
        write_show_metadata(
            canonical_show,
            [profiled_race],
            {1: "/config/poster.png"},
            config,
            previous_title="F1 2026",
        )[1]
        is False
    )
    assert artwork_fingerprint(race, "M0 0", config) == artwork_fingerprint(race, "M0 0", config)
    assert artwork_fingerprint(race, "M0 0", config) == artwork_fingerprint(
        profiled_race, "M0 0", config
    )


def test_artwork_path_commands_fonts_logo_and_fallback(
    tmp_path, core, schedule_payload, monkeypatch
):
    commands = "M1 1 T2 2 H3 V4 C4 5 5 6 6 7 S7 8 8 9 Q9 10 10 11 A1 1 0 0 0 12 13 Z"
    assert len(svg_path_points(commands)) > 20
    assert svg_path_points("1 2") == []
    assert svg_path_points("M") == []
    assert _fit([], (0, 0, 1, 1)) == []
    fallback = object()
    monkeypatch.setattr(
        "extensions.formula1.artwork.ImageFont.truetype",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()),
    )
    monkeypatch.setattr("extensions.formula1.artwork.ImageFont.load_default", lambda: fallback)
    assert _font(None, 10) is fallback
    monkeypatch.undo()
    loaded = object()
    monkeypatch.setattr(Path, "is_file", lambda _path: True)
    monkeypatch.setattr(
        "extensions.formula1.artwork.ImageFont.truetype", lambda *_args, **_kwargs: loaded
    )
    assert _font("font.ttf", 10) is loaded
    monkeypatch.undo()
    config = load_formula1_config(core, tmp_path / "config")
    config["artwork"].update(width=600, height=900)
    branding = config["paths"]["branding"]
    branding.mkdir(parents=True)
    Image.new("RGBA", (80, 40), (255, 255, 255, 255)).save(branding / "logo.png")
    race = parse_schedule(schedule_payload, 2026)[0]
    race = type(race)(
        **{**race.__dict__, "sprint_date": None, "race_date": None, "country": "Unknown"}
    )
    destination = tmp_path / "fallback.png"
    render_round_poster(race, None, config, destination)
    assert destination.exists()


def test_country_flag_background_resolution_and_visual_structure(tmp_path, monkeypatch):
    assert RENDERER_VERSION == 5
    for code in set(COUNTRY_FLAG_CODES.values()):
        with Image.open(FLAG_ASSET_ROOT / f"{code}.png") as flag:
            flag.verify()
    assert _country_key("Türkiye") == "turkiye"
    assert country_flag_asset("Canada").name == "ca.png"
    assert country_flag_asset("USA").name == "us.png"
    assert country_flag_asset("United Arab Emirates").name == "ae.png"
    assert country_flag_asset("Unknown") is None

    canadian = _render_background("Canada", 600, 900)
    fallback = _render_background("Unknown", 600, 900)
    canadian.save(tmp_path / "canadian-background.png")
    assert canadian.getpixel((300, 342)) != fallback.getpixel((300, 342))
    assert len({fallback.getpixel((0, y)) for y in range(0, 900, 100)}) > 1
    with Image.open(FLAG_ASSET_ROOT / "jp.png") as japan:
        flag_layer, flag_mask = _flag_overlay(japan, 600, 900)
    red_pixels = Image.new("L", flag_layer.size)
    red_pixels.putdata(
        [
            255 if red > 150 and green < 100 and blue < 100 else 0
            for red, green, blue in flag_layer.get_flattened_data()
        ]
    )
    red_bounds = red_pixels.getbbox()
    assert red_bounds is not None
    red_width = red_bounds[2] - red_bounds[0]
    red_height = red_bounds[3] - red_bounds[1]
    assert abs(red_width - red_height) <= 2
    assert red_width < 600 * 0.5
    assert flag_mask.getpixel((300, 342)) == FLAG_ALPHA
    assert flag_mask.getpixel((300, 0)) == 0
    japanese = _render_background("Japan", 600, 900)
    white_field = japanese.getpixel((115, 342))
    red_disc = japanese.getpixel((300, 342))
    neutral_top = japanese.getpixel((300, 40))
    assert min(white_field) > 150
    assert max(white_field) - min(white_field) < 18
    assert red_disc[0] > 110 and red_disc[0] > red_disc[1] * 4
    assert max(neutral_top) - min(neutral_top) < 15
    monkeypatch.setattr("extensions.formula1.artwork.FLAG_ASSET_ROOT", tmp_path)
    assert country_flag_asset("Canada") is None


def test_artwork_temporary_is_removed_after_failed_install(
    tmp_path, core, schedule_payload, monkeypatch
):
    config = load_formula1_config(core, tmp_path / "config")
    config["artwork"].update(width=600, height=900)
    race = parse_schedule(schedule_payload, 2026)[0]
    monkeypatch.setattr(
        "extensions.formula1.artwork.atomic_replace_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("install")),
    )
    with pytest.raises(OSError, match="install"):
        render_round_poster(race, None, config, tmp_path / "poster.png")
    assert not list(tmp_path.glob("*.png"))


def test_managed_artwork_and_run_logging(tmp_path, core):
    config = load_formula1_config(core, tmp_path / "config")
    state = Formula1State(config["paths"]["database"])
    poster = tmp_path / "poster.png"
    assert _managed_artwork_action(state, "key", poster, "fp") == "create"
    poster.write_bytes(b"first")
    assert _managed_artwork_action(state, "key", poster, "fp") == "adopt"
    state.save_artwork("key", poster, "fp", __import__("hashlib").sha256(b"first").hexdigest())
    assert _managed_artwork_action(state, "key", poster, "fp") == "unchanged"
    assert _managed_artwork_action(state, "key", poster, "new") == "update"
    poster.write_bytes(b"manual")
    assert _managed_artwork_action(state, "key", poster, "new") == "preserve-manual"
    state.close()
    run_id = run_identifier(datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=timezone.utc))
    logger, path = create_formula1_logger(config, run_id)
    logger.info("separate log")
    assert path.exists() and "separate log" in path.read_text()
    config["logging"].update(console="full", retention=1)
    create_formula1_logger(config, "second")
    create_formula1_logger(config, "third")
    assert len(list(config["paths"]["logs"].glob("formula1-*.log"))) == 1
    config["dry_run"] = True
    config["logging"]["console"] = "off"
    dry_logger, dry_path = create_formula1_logger(config, "dry")
    assert dry_path is None and dry_logger.handlers


def test_metadata_read_errors_missing_round_and_fact_values(tmp_path, core, schedule_payload):
    config = load_formula1_config(core, tmp_path / "config")
    inventory = __import__(
        "extensions.formula1.inventory", fromlist=["Formula1Episode", "Formula1Show"]
    )
    episode = inventory.Formula1Episode(
        2026, 2, 1, "Unknown", "Sprint Session", "sprint", tmp_path / "x", "1", "current"
    )
    show = inventory.Formula1Show(2026, "F1 2026", "show", [episode])
    generated, seasons, _episodes = build_show_entry(
        show, parse_schedule(schedule_payload, 2026), {}, config
    )
    assert seasons == set() and generated["seasons"] == {}
    destination = config["paths"]["metadata"] / "formula1_2026.yml"
    destination.parent.mkdir(parents=True)
    destination.write_text("[broken", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Unable to read"):
        write_show_metadata(show, [], {}, config)
    destination.write_text("- invalid\n", encoding="utf-8")
    with pytest.raises(TypeError, match="Invalid existing"):
        write_show_metadata(show, [], {}, config)


def test_issue_report_and_empty_runner(tmp_path, core):
    config = load_formula1_config(core, tmp_path / "config")
    path = _write_issues(config, "run", ["one"])
    assert '"one"' in path.read_text()
    assert (
        asyncio.run(
            run_formula1_extension(
                [],
                core,
                Session({}, ""),
                logging.getLogger("empty"),
                base_config_dir=tmp_path / "config",
            )
        )
        is None
    )
    regular = [Section("Movies", [])]
    assert partition_formula1_sections(regular, core, {}) == (regular, [])
    custom_root = tmp_path / "custom-config"
    private = custom_root / "formula1"
    private.mkdir(parents=True)
    (private / "formula1.yml").write_text("library:\n  name: Racing\n", encoding="utf-8")
    racing = Section("Racing", [])
    assert partition_formula1_sections(
        [racing], core, {"FORMULA1_ENABLED": "true"}, base_config_dir=custom_root
    )[1] == [racing]


def test_runner_end_to_end_isolated_outputs(tmp_path, core, schedule_payload):
    media = tmp_path / "S01E01 - Australia Grand Prix - Race.mkv"
    section = Section("Formula 1", [Show("F1 2026", [Season(1, [Episode(1, media)])])])
    session = Session(schedule_payload, '<svg><path d="M0 0 L100 0 L100 100 Z"/></svg>')
    logger = logging.getLogger("formula1-end-to-end")
    summary = asyncio.run(
        run_formula1_extension(
            [section], core, session, logger, base_config_dir=tmp_path / "config"
        )
    )
    assert summary["episodes"] == 1
    assert summary["artwork_created"] == 1
    metadata_path = tmp_path / "kometa/metadata/formula1_2026.yml"
    assert metadata_path.exists()
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    season = metadata["metadata"]["F1 2026"]["seasons"][1]
    assert "race runs for 58 laps and covers 306.124 km" in season["summary"]
    assert "race runs for 58 laps and covers 306.124 km" in season["episodes"][1]["summary"]
    assert (tmp_path / "config/formula1/cache/formula1.sqlite3").exists()
    assert not list((tmp_path / "kometa/metadata").glob("tv_metadata.yml"))

    unchanged = asyncio.run(
        run_formula1_extension(
            [section], core, session, logger, base_config_dir=tmp_path / "config"
        )
    )
    assert unchanged["artwork_unchanged"] == 1
    assert unchanged["metadata_unchanged"] == 1

    changed_payload = yaml.safe_load(yaml.safe_dump(schedule_payload))
    changed_payload["MRData"]["RaceTable"]["Races"][0]["raceName"] = "Melbourne Grand Prix"
    state = Formula1State(tmp_path / "config/formula1/cache/formula1.sqlite3")
    state.connection.execute("DELETE FROM provider_cache WHERE provider='jolpica'")
    state.connection.commit()
    state.close()
    updated = asyncio.run(
        run_formula1_extension(
            [section],
            core,
            Session(changed_payload, session.svg),
            logger,
            base_config_dir=tmp_path / "config",
        )
    )
    assert updated["artwork_updated"] == 1

    poster = tmp_path / "kometa/assets/formula1/rounds/2026/round-01/poster.png"
    poster.write_bytes(b"manual artwork")
    preserved = asyncio.run(
        run_formula1_extension(
            [section],
            core,
            Session(changed_payload, session.svg),
            logger,
            base_config_dir=tmp_path / "config",
        )
    )
    assert preserved["artwork_preserved"] == 1

    missing_media = tmp_path / "S02E01 - Bahrain Grand Prix - Race.mkv"
    missing_section = Section(
        "Formula 1", [Show("F1 2026", [Season(2, [Episode(1, missing_media)])])]
    )
    missing = asyncio.run(
        run_formula1_extension(
            [missing_section],
            core,
            Session(changed_payload, session.svg),
            logger,
            base_config_dir=tmp_path / "config",
        )
    )
    assert missing["issues"] == 1
    assert Path(missing["issue_report"]).exists()


def test_runner_adoption_dry_run_disabled_outputs_and_failure(tmp_path, core, schedule_payload):
    media = tmp_path / "S01E01 - Australia Grand Prix - Race.mkv"
    section = Section("Formula 1", [Show("F1 2026", [Season(1, [Episode(1, media)])])])
    base = tmp_path / "config"
    config = load_formula1_config(core, base)
    poster = config["paths"]["assets"] / "2026/round-01/poster.png"
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"existing")
    adopted = asyncio.run(
        run_formula1_extension(
            [section],
            core,
            Session(schedule_payload, '<svg><path d="M0 0 L1 1"/></svg>'),
            logging.getLogger("adopt"),
            base_config_dir=base,
        )
    )
    assert adopted["artwork_adopted"] == 1

    active = base / "formula1/formula1.yml"
    active.write_text(
        "artwork:\n  enabled: false\nmetadata:\n  enabled: false\nlogging:\n  console: off\n",
        encoding="utf-8",
    )
    poster.unlink()
    state = Formula1State(config["paths"]["database"])
    state.remove_artwork("2026:r01")
    state.close()
    disabled = asyncio.run(
        run_formula1_extension(
            [section],
            core,
            Session(schedule_payload, '<svg><path d="M0 0 L1 1"/></svg>'),
            logging.getLogger("disabled"),
            base_config_dir=base,
        )
    )
    assert disabled["artwork_unchanged"] == 1 and disabled["metadata_updated"] == 0

    dry_core = {**core, "settings": {**core["settings"], "dry_run": True}}
    dry = asyncio.run(
        run_formula1_extension(
            [section],
            dry_core,
            Session(schedule_payload, '<svg><path d="M0 0 L1 1"/></svg>'),
            logging.getLogger("dry"),
            base_config_dir=tmp_path / "dry-config",
        )
    )
    assert dry["log"] is None
    assert not (tmp_path / "dry-config").exists()

    bad = Section("Formula 1", [])
    bad.type = "movie"
    with pytest.raises(RuntimeError, match="TV library"):
        asyncio.run(
            run_formula1_extension(
                [bad],
                core,
                Session(schedule_payload, ""),
                logging.getLogger("bad"),
                base_config_dir=tmp_path / "bad-config",
            )
        )


def test_runner_cleanup_removes_only_owned_unchanged_round(tmp_path, core, schedule_payload):
    media = tmp_path / "S01E01 - Australia Grand Prix - Race.mkv"
    populated = Section("Formula 1", [Show("F1 2026", [Season(1, [Episode(1, media)])])])
    empty = Section("Formula 1", [])
    base = tmp_path / "config"
    private = base / "formula1"
    private.mkdir(parents=True)
    (private / "formula1.yml").write_text(
        "cleanup:\n  enabled: true\n  confirmation_scans: 1\n  grace_hours: 0\n",
        encoding="utf-8",
    )
    session = Session(schedule_payload, '<svg><path d="M0 0 L1 1"/></svg>')
    asyncio.run(
        run_formula1_extension(
            [populated], core, session, logging.getLogger("cleanup-populate"), base_config_dir=base
        )
    )
    poster = tmp_path / "kometa/assets/formula1/rounds/2026/round-01/poster.png"
    assert poster.exists()
    episode_poster = (
        tmp_path
        / "kometa/assets/formula1/rounds/2026/round-01/episodes/episode-01.png"
    )
    episode_poster.parent.mkdir(parents=True)
    episode_poster.write_bytes(b"managed episode")
    state = Formula1State(private / "cache/formula1.sqlite3")
    state.save_artwork(
        "2026:r01:e01",
        episode_poster,
        "episode-fingerprint",
        __import__("hashlib").sha256(episode_poster.read_bytes()).hexdigest(),
    )
    state.close()
    cleaned = asyncio.run(
        run_formula1_extension(
            [empty], core, session, logging.getLogger("cleanup-empty"), base_config_dir=base
        )
    )
    assert cleaned["cleanup_removed"] == 2
    assert not poster.exists()
    assert not episode_poster.exists()


def test_runner_rejects_duplicate_year_before_outputs(tmp_path, core, schedule_payload):
    first = tmp_path / "S01E01 - Australia Grand Prix - Race.mkv"
    second = tmp_path / "S01E02 - Australia Grand Prix - Highlights.mkv"
    section = Section(
        "Formula 1",
        [
            Show("F1 2026", [Season(1, [Episode(1, first)])], key="show-a"),
            Show("Formula 2026", [Season(1, [Episode(2, second)])], key="show-b"),
        ],
    )
    with pytest.raises(RuntimeError, match="One Plex show per Formula 1 championship year"):
        asyncio.run(
            run_formula1_extension(
                [section],
                core,
                Session(schedule_payload, ""),
                logging.getLogger("duplicate-year"),
                base_config_dir=tmp_path / "config",
            )
        )
    assert not (tmp_path / "kometa/metadata/formula1_2026.yml").exists()


def test_runner_quarantines_filename_event_mismatch(tmp_path, core, schedule_payload):
    media = tmp_path / "S01E01 - Bahrain Grand Prix - Race.mkv"
    section = Section("Formula 1", [Show("F1 2026", [Season(1, [Episode(1, media)])])])
    summary = asyncio.run(
        run_formula1_extension(
            [section],
            core,
            Session(schedule_payload, ""),
            logging.getLogger("event-mismatch"),
            base_config_dir=tmp_path / "config",
        )
    )
    assert summary["event_mismatches"] == 1
    assert summary["episodes"] == 0
    assert not (tmp_path / "kometa/metadata/formula1_2026.yml").exists()


def test_cleanup_grace_also_controls_yaml_reconciliation(
    tmp_path, core, schedule_payload
):
    first = tmp_path / "S01E01 - Australia Grand Prix - Race.mkv"
    second = tmp_path / "S01E02 - Australia Grand Prix - Highlights.mkv"
    populated = Section(
        "Formula 1", [Show("F1 2026", [Season(1, [Episode(1, first), Episode(2, second)])])]
    )
    reduced = Section(
        "Formula 1", [Show("F1 2026", [Season(1, [Episode(1, first)])])]
    )
    base = tmp_path / "config"
    private = base / "formula1"
    private.mkdir(parents=True)
    (private / "formula1.yml").write_text(
        "cleanup:\n  enabled: true\n  confirmation_scans: 2\n  grace_hours: 0\n"
        "show_artwork:\n  enabled: false\n",
        encoding="utf-8",
    )
    session = Session(schedule_payload, '<svg><path d="M0 0 L1 1"/></svg>')
    for section in (populated, reduced):
        asyncio.run(
            run_formula1_extension(
                [section], core, session, logging.getLogger("yaml-grace"), base_config_dir=base
            )
        )
    path = tmp_path / "kometa/metadata/formula1_2026.yml"
    document = yaml.safe_load(path.read_text())
    assert 2 in document["metadata"]["F1 2026"]["seasons"][1]["episodes"]
    asyncio.run(
        run_formula1_extension(
            [reduced], core, session, logging.getLogger("yaml-prune"), base_config_dir=base
        )
    )
    document = yaml.safe_load(path.read_text())
    assert 2 not in document["metadata"]["F1 2026"]["seasons"][1]["episodes"]


def test_show_rename_keeps_stable_mapping_and_match_aliases(tmp_path, core, schedule_payload):
    config = load_formula1_config(core, tmp_path / "config")
    inventory = __import__(
        "extensions.formula1.inventory", fromlist=["Formula1Episode", "Formula1Show"]
    )
    episode = inventory.Formula1Episode(
        2026, 1, 1, "Australian Grand Prix", "Race Session", "race",
        tmp_path / "race.mkv", "episode", "current"
    )
    old = inventory.Formula1Show(2026, "F1 2026", "same-show", [episode])
    race = parse_schedule(schedule_payload, 2026)[0]
    path = write_show_metadata(old, [race], {1: "/config/poster.png"}, config)[0]
    document = yaml.safe_load(path.read_text())
    document["metadata"]["F1 2026"]["user_note"] = "keep"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    renamed = inventory.Formula1Show(2026, "Formula One 2026", "same-show", [episode])
    write_show_metadata(
        renamed,
        [race],
        {1: "/config/poster.png"},
        config,
        previous_title="F1 2026",
    )
    migrated = yaml.safe_load(path.read_text())["metadata"]
    assert list(migrated) == ["F1 2026"]
    assert migrated["F1 2026"]["user_note"] == "keep"
    assert migrated["F1 2026"]["match"]["title"] == [
        "F1 2026",
        "Formula 1 (2026)",
        "Formula One 2026",
    ]
    assert migrated["F1 2026"]["title"] == "Formula 1 (2026)"


def test_formula1_alias_merge_preserves_stable_nested_values():
    primary = {"manual": {"owner": "user"}, "summary": "stable"}
    secondary = {"manual": {"note": "keep"}, "summary": "legacy"}

    assert _merge_preserved_fields(primary, secondary) == {
        "manual": {"owner": "user", "note": "keep"},
        "summary": "stable",
    }
    assert _merge_preserved_fields(primary, "not-a-mapping") == primary


def test_formula1_order_normalizer_handles_partial_manual_entries():
    assert _ordered_fields("manual", ()) == "manual"
    assert _normalize_show_order({"title": "Formula 1 (2026)"}) == {
        "title": "Formula 1 (2026)"
    }
    assert _normalize_show_order({"seasons": {1: {"title": "Round 1"}}}) == {
        "seasons": {1: {"title": "Round 1"}}
    }


def test_legacy_title_key_is_consolidated_without_losing_manual_fields(
    tmp_path, core, schedule_payload
):
    config = load_formula1_config(core, tmp_path / "config")
    inventory = __import__(
        "extensions.formula1.inventory", fromlist=["Formula1Episode", "Formula1Show"]
    )
    episode = inventory.Formula1Episode(
        2026,
        1,
        1,
        "Australian Grand Prix",
        "Race Session",
        "race",
        tmp_path / "race.mkv",
        "episode",
        "current",
    )
    show = inventory.Formula1Show(2026, "Formula 1 (2026)", "same-show", [episode])
    destination = config["paths"]["metadata"] / "formula1_2026.yml"
    destination.parent.mkdir(parents=True)
    destination.write_text(
        yaml.safe_dump(
            {
                "metadata": {
                    "Formula 1 (2026)": {
                        "title": "Formula 1 (2026)",
                        "f1_season": "2026",
                        "user_note": "preserve",
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    path, changed, _diagnostics = write_show_metadata(
        show,
        [parse_schedule(schedule_payload, 2026)[0]],
        {1: "/config/poster.png"},
        config,
        previous_title="F1 2026",
    )

    metadata = yaml.safe_load(path.read_text(encoding="utf-8"))["metadata"]
    assert changed and list(metadata) == ["F1 2026"]
    assert metadata["F1 2026"]["user_note"] == "preserve"
    assert metadata["F1 2026"]["match"]["title"] == [
        "F1 2026",
        "Formula 1 (2026)",
    ]


def test_delayed_application_verification_queue_and_report(tmp_path, core, monkeypatch):
    inventory = __import__(
        "extensions.formula1.inventory", fromlist=["Formula1Show"]
    )
    config = load_formula1_config(core, tmp_path / "config")
    config["verification"]["delay_hours"] = 0
    plex_show = Show("F1 2026", [])
    plex_show.summary = "Expected championship summary"
    show = inventory.Formula1Show(
        2026, "F1 2026", plex_show.ratingKey, [], plex_item=plex_show
    )
    state = Formula1State(config["paths"]["database"])
    queue_application_verification(
        state,
        show,
        {"summary": "Expected championship summary"},
        [],
        config,
    )
    records, report = asyncio.run(
        verify_due_applications(
            state,
            [show],
            config,
            core,
            None,
            "verification-run",
            logging.getLogger("verification"),
        )
    )
    assert records[0]["status"] == "applied"
    assert report.exists()
    assert state.due_application_verifications() == []
    for suffix in ("old-a", "old-b"):
        (config["paths"]["reports"] / f"formula1-application-verification-{suffix}.json").write_text(
            "{}\n", encoding="utf-8"
        )
    config["verification"]["retention"] = 1
    monkeypatch.setattr(
        formula1_verification_module,
        "compare_kometa_entry",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("readback")),
    )
    state.queue_application_verification(
        2026,
        plex_show.ratingKey,
        {
            "metadata": {},
            "artwork": [
                {
                    "child_key": "season:99",
                    "asset_type": "poster",
                    "destination": str(tmp_path / "missing-poster.png"),
                }
            ],
        },
        0,
    )
    partial, partial_report = asyncio.run(
        verify_due_applications(
            state,
            [show],
            config,
            core,
            None,
            "zzzz-verification-partial",
            logging.getLogger("verification"),
        )
    )
    assert partial[0]["status"] == "partial"
    assert partial[0]["artwork"][0]["status"] == "local_missing"
    assert partial_report.exists()
    assert len(list(config["paths"]["reports"].glob("formula1-application-verification-*.json"))) == 1
    state.queue_application_verification(2027, "missing", {"metadata": {}, "artwork": []}, 0)
    missing, _report = asyncio.run(
        verify_due_applications(
            state,
            [show],
            config,
            core,
            None,
            "verification-missing",
            logging.getLogger("verification"),
        )
    )
    assert missing[0]["status"] == "unverifiable"
    config["verification"]["enabled"] = False
    assert asyncio.run(
        verify_due_applications(
            state, [show], config, core, None, "disabled", logging.getLogger("verification")
        )
    ) == ([], None)
    queue_application_verification(state, show, {}, [], config)
    config["verification"]["enabled"] = True
    config["dry_run"] = True
    queue_application_verification(state, show, {}, [], config)
    state.close()


def test_application_artwork_verification_outcomes(tmp_path, core, monkeypatch):
    config = load_formula1_config(core, tmp_path / "config")
    child = SimpleNamespace(index=1, thumb="/season-thumb")
    show = SimpleNamespace(
        thumb="/poster-thumb",
        art="/background-thumb",
        seasons=lambda: [child],
    )
    assert _selected_path(show, "", "poster") == "/poster-thumb"
    assert _selected_path(show, "", "background") == "/background-thumb"
    assert _selected_path(show, "season:1", "poster") == "/season-thumb"
    assert _selected_path(show, "season:99", "poster") is None
    missing = asyncio.run(
        _verify_artwork(
            show,
            {"destination": str(tmp_path / "absent.png"), "asset_type": "poster"},
            core,
            config,
            None,
        )
    )
    assert missing["status"] == "local_missing"
    local = tmp_path / "poster.png"
    local.write_bytes(b"local")

    async def unavailable(*_args):
        return None, "not selected"

    monkeypatch.setattr(formula1_verification_module, "_download_plex_image", unavailable)
    unavailable_result = asyncio.run(
        _verify_artwork(
            show,
            {"destination": str(local), "asset_type": "poster"},
            core,
            config,
            None,
        )
    )
    assert unavailable_result["status"] == "plex_unavailable"

    async def downloaded(*_args):
        return b"plex", None

    monkeypatch.setattr(formula1_verification_module, "_download_plex_image", downloaded)
    monkeypatch.setattr(
        formula1_verification_module,
        "analyze_image_content",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad image")),
    )
    invalid = asyncio.run(
        _verify_artwork(
            show,
            {"destination": str(local), "asset_type": "poster"},
            core,
            config,
            None,
        )
    )
    assert invalid["status"] == "unverifiable"

    def analysis(content, **_kwargs):
        return {
            "content_sha256": content.decode(),
            "perceptual_hash": "0000" if content == b"local" else "ffff",
        }

    monkeypatch.setattr(formula1_verification_module, "analyze_image_content", analysis)
    different = asyncio.run(
        _verify_artwork(
            show,
            {"destination": str(local), "asset_type": "poster"},
            core,
            config,
            None,
        )
    )
    assert different["status"] == "not_selected"
    monkeypatch.setattr(
        formula1_verification_module,
        "analyze_image_content",
        lambda *_args, **_kwargs: {"content_sha256": "same", "perceptual_hash": "00"},
    )
    selected = asyncio.run(
        _verify_artwork(
            show,
            {"destination": str(local), "asset_type": "poster"},
            core,
            config,
            None,
        )
    )
    assert selected["status"] == "selected" and selected["exact_match"]


def test_authoritative_child_parser_ignores_unrelated_and_malformed_keys():
    seasons, episodes = _authoritative_children(
        {"2026:r01:e02", "2025:r01:e01", "2026:bad", "2026:rxx:e01"}, 2026
    )
    assert seasons == {1} and episodes == {1: {2}}


@pytest.mark.parametrize(
    ("action", "counter", "references"),
    [
        ("rotated", "show_artwork_rotated", True),
        ("restored", "show_artwork_restored", True),
        ("rerendered", "show_artwork_rerendered", True),
        ("unchanged", "show_artwork_unchanged", True),
        ("preserved", "show_artwork_preserved", True),
        ("missing", "show_artwork_missing", False),
    ],
)
def test_runner_maps_each_show_artwork_outcome(
    tmp_path, core, schedule_payload, monkeypatch, action, counter, references
):
    media = tmp_path / "S01E01 - Australia Grand Prix - Race.mkv"
    second_media = tmp_path / "S02E01 - Chinese Grand Prix - Race.mkv"
    second_race = {
        **schedule_payload["MRData"]["RaceTable"]["Races"][0],
        "round": "2",
        "raceName": "Chinese Grand Prix",
        "Circuit": {
            "circuitId": "shanghai",
            "circuitName": "Shanghai International Circuit",
            "Location": {
                "locality": "Shanghai",
                "country": "China",
                "lat": "31.3389",
                "long": "121.2197",
            },
        },
    }
    schedule_payload["MRData"]["RaceTable"]["Races"].append(second_race)
    section = Section(
        "Formula 1",
        [
            Show(
                "F1 2026",
                [Season(1, [Episode(1, media)]), Season(2, [Episode(1, second_media)])],
            )
        ],
    )

    async def result(*_args, **_kwargs):
        extension_config = _args[2]
        episode_path = (
            extension_config["paths"]["assets"]
            / "2026/round-01/episodes/episode-01.png"
        )
        episode_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (32, 18), "black").save(episode_path)
        return ShowArtworkResult(
            action,
            1,
            "/config/assets/formula1/shows/2026/poster.png" if references else None,
            "/config/assets/formula1/shows/2026/background.png" if references else None,
            "Test Team" if references else None,
            "no source" if action == "missing" else None,
            episode_references={
                1: "/config/assets/formula1/rounds/2026/round-01/episodes/episode-01.png"
            },
            episode_actions={
                1: "create",
                2: "update",
                3: "preserve-manual",
                4: "unchanged",
            },
            photo_path=str(episode_path),
            source_identity="source",
        )

    monkeypatch.setattr("extensions.formula1.runner.run_show_artwork_rotation", result)
    if action == "rerendered":
        monkeypatch.setattr(
            Formula1State,
            "show_rotation",
            lambda _state, _key: {
                "poster_destination": str(tmp_path / "show-poster.png"),
                "background_destination": str(tmp_path / "show-background.png"),
            },
        )
    summary = asyncio.run(
        run_formula1_extension(
            [section],
            core,
            Session(schedule_payload, '<svg><path d="M0 0 L1 1"/></svg>'),
            logging.getLogger(f"show-artwork-{action}"),
            base_config_dir=tmp_path / "config",
        )
    )
    assert summary[counter] == 1
    assert summary["episode_artwork_created"] == 1
    assert summary["episode_artwork_updated"] == 1
    assert summary["episode_artwork_preserved"] == 2
    assert summary["episode_artwork_unchanged"] == 1
    metadata = yaml.safe_load((tmp_path / "kometa/metadata/formula1_2026.yml").read_text())
    if references:
        assert metadata["metadata"]["F1 2026"]["file_poster"].endswith("poster.png")
        assert metadata["metadata"]["F1 2026"]["file_background"].endswith("background.png")
    else:
        assert "file_poster" not in metadata["metadata"]["F1 2026"]


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
def test_main_orchestrator_hands_formula_library_to_extension(tmp_path, monkeypatch, outcome):
    from test_phase26_orchestration_matrix import (
        _config,
        _patch_runtime,
        _Section,
    )

    import metafusion

    section = _Section("Formula 1", "show", [])
    _patch_runtime(
        monkeypatch,
        [section],
        ["Formula 1"],
        [{"title": "Formula 1", "type": "show"}],
    )
    monkeypatch.setattr(
        metafusion,
        "partition_formula1_sections",
        lambda *_args, **_kwargs: ([], [section]),
    )

    async def extension(*_args, **_kwargs):
        if outcome == "failure":
            raise RuntimeError("extension failed")
        if outcome == "cancel":
            raise asyncio.CancelledError
        return {"episodes": 1}

    monkeypatch.setattr(metafusion, "run_formula1_extension", extension)
    monkeypatch.setattr(
        metafusion,
        "get_feature_flags",
        lambda _config: {"dry_run": False, "cleanup": False},
    )
    monkeypatch.setattr(metafusion, "library_full_scan_decisions", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        metafusion,
        "prepare_tmdb_change_plan",
        lambda *_args, **_kwargs: {"status": "disabled"},
    )
    config = _config(tmp_path)
    config["settings"]["mode"] = "kometa"
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(metafusion.metafusion_main(config, logging.getLogger("f1-main")))
    elif outcome == "failure":
        with pytest.raises(RuntimeError, match="Formula 1: extension failed"):
            asyncio.run(metafusion.metafusion_main(config, logging.getLogger("f1-main")))
    else:
        asyncio.run(metafusion.metafusion_main(config, logging.getLogger("f1-main")))
        assert config["_formula1_summary"] == {"episodes": 1}
