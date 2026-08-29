import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from extensions.formula1 import upgrades
from extensions.formula1.commons import CommonsCandidate, ConstructorData
from extensions.formula1.race_background import RaceBackgroundCandidate
from extensions.formula1.state import Formula1State


def _candidate(*, provider="commons", constructor="ferrari", suffix="old"):
    return CommonsCandidate(
        1,
        f"Ferrari {suffix}",
        f"https://example.com/{suffix}",
        f"https://example.com/{suffix}.jpg",
        1600,
        900,
        "image/jpeg",
        f"sha-{suffix}",
        "Photographer",
        "CC BY 4.0",
        "https://creativecommons.org/licenses/by/4.0/",
        constructor,
        constructor.title(),
        10.0,
        provider,
    )


def _background(*, provider="commons", tier="historical_circuit_action_race_car"):
    return RaceBackgroundCandidate(
        2,
        "Australian Grand Prix action",
        "https://example.com/background",
        "https://example.com/background.jpg",
        1920,
        1080,
        "image/jpeg",
        "sha-background",
        "Photographer",
        "CC BY 4.0",
        "https://creativecommons.org/licenses/by/4.0/",
        "Ferrari",
        20.0,
        match_tier=tier,
        provider=provider,
    )


def _show(rounds=(1,)):
    episodes = [
        SimpleNamespace(round_number=number, episode_number=1) for number in rounds
    ]
    return SimpleNamespace(year=2026, title="F1 2026", episodes=episodes)


def _race(number=1):
    return SimpleNamespace(round_number=number)


def _config(tmp_path, *, dry_run=False, flickr=True):
    return {
        "dry_run": dry_run,
        "providers": {"flickr_enabled": flickr},
        "paths": {"reports": tmp_path / "reports"},
    }


def _detailed_image(path, size, *, blank=False):
    image = Image.new("RGB", size, (120, 120, 120))
    if not blank:
        draw = ImageDraw.Draw(image)
        for x in range(0, size[0], 8):
            draw.line((x, 0, size[0] - x, size[1]), fill=(255, 0, 0), width=3)
    image.save(path)


def test_quality_decision_decodes_images_and_handles_missing(tmp_path):
    old = tmp_path / "old.png"
    new = tmp_path / "new.png"
    _detailed_image(old, (100, 100), blank=True)
    _detailed_image(new, (200, 200))
    accepted, reason, evidence = upgrades._quality_decision(None, new)
    assert accepted is False
    assert reason == "existing source unavailable; quality gain cannot be verified"
    assert evidence["new"]["pixels"] == 40_000
    accepted, reason, evidence = upgrades._quality_decision(old, new)
    assert accepted is True
    assert reason in {"higher-resolution source", "sharper source"}
    assert evidence["perceptual_duplicate"] is False


@pytest.mark.parametrize(
    ("old", "new", "specific", "accepted", "reason"),
    [
        ({"pixels": 100, "sharpness": 10}, {"pixels": 120, "sharpness": 8}, False, True, "higher-resolution source"),
        ({"pixels": 100, "sharpness": 10}, {"pixels": 90, "sharpness": 12}, False, True, "sharper source"),
        ({"pixels": 100, "sharpness": 10}, {"pixels": 105, "sharpness": 10.5}, False, True, "improved resolution and sharpness"),
        ({"pixels": 100, "sharpness": 10}, {"pixels": 90, "sharpness": 8.5}, True, True, "stronger event/circuit specificity"),
        ({"pixels": 100, "sharpness": 10}, {"pixels": 90, "sharpness": 8}, False, False, "candidate did not materially improve decoded quality"),
    ],
)
def test_quality_decision_policy_branches(tmp_path, monkeypatch, old, new, specific, accepted, reason):
    old_path = tmp_path / "old"
    new_path = tmp_path / "new"
    old_path.touch()
    monkeypatch.setattr(upgrades, "_image_quality", lambda path: old if Path(path).name == "old" else new)
    monkeypatch.setattr(
        upgrades,
        "_perceptual_hash",
        lambda path: "old" if Path(path).name == "old" else "new",
    )
    result = upgrades._quality_decision(old_path, new_path, specificity_gain=specific)
    assert result[0] is accepted
    assert result[1] == reason
    assert result[2]["perceptual_duplicate"] is False


def test_quality_decision_never_upgrades_a_perceptual_duplicate(
    tmp_path, monkeypatch
):
    old_path = tmp_path / "old"
    new_path = tmp_path / "new"
    old_path.touch()
    monkeypatch.setattr(
        upgrades,
        "_image_quality",
        lambda path: {
            "pixels": 100 if Path(path).name == "old" else 200,
            "sharpness": 10 if Path(path).name == "old" else 20,
        },
    )
    monkeypatch.setattr(upgrades, "_perceptual_hash", lambda _path: "same")

    accepted, reason, evidence = upgrades._quality_decision(old_path, new_path)

    assert accepted is False
    assert reason == "perceptual duplicate"
    assert evidence["perceptual_duplicate"] is True


def test_state_records_upgrade_and_refreshes_rotation_history(tmp_path):
    state = Formula1State(tmp_path / "formula1.sqlite3")
    source = {"candidate": _candidate().as_dict()}
    state.save_show_rotation("show:2026", 2026, 1, "ferrari", source, "p", "pc", "b", "bc")
    source["candidate"] = _candidate(provider="flickr", suffix="new").as_dict()
    state.save_show_rotation("show:2026", 2026, 1, "ferrari", source, "p2", "pc2", "b2", "bc2")
    history = state.show_rotation_history()
    assert len(history) == 1
    assert history[0]["source"]["candidate"]["provider"] == "flickr"
    assert history[0]["poster_destination"] == "p2"
    state.record_artwork_upgrade(
        "run", 1, "current", 2026, 1, "episode", "upgraded",
        old_provider="commons", new_provider="flickr", details={"reason": "better"}
    )
    assert state.artwork_upgrade_history(run_id="missing") == []
    records = state.artwork_upgrade_history(run_id="run")
    assert records[0]["details"] == {"reason": "better"}
    assert state.artwork_upgrade_history()[0]["new_provider"] == "flickr"
    state.close()


def test_record_and_report_outputs(tmp_path):
    state = Formula1State(":memory:")
    old = _candidate()
    new = _candidate(provider="flickr", suffix="new")
    record = upgrades._record(
        state, "run", "all", 2026, 2, "episode", "upgraded", old, new,
        {"reason": "sharper source"},
    )
    assert record["old_source"] == "sha-old"
    no_sha = CommonsCandidate(**{**new.as_dict(), "source_sha1": ""})
    assert upgrades._source_name(no_sha) != ""
    path = upgrades.write_upgrade_report(_config(tmp_path), "run", "all", [record])
    assert path and path.is_file()
    assert "sharper source" in path.with_suffix(".txt").read_text()
    assert json.loads(path.read_text())["records"][0]["status"] == "upgraded"
    assert upgrades.write_upgrade_report(_config(tmp_path, dry_run=True), "dry", "all", []) is None
    state.close()


def test_background_tiers_are_ordered():
    assert upgrades._background_tier(_background(tier="exact_event_action_race_car")) == 3
    assert upgrades._background_tier(_background(tier="recent_circuit_action_race_car")) == 2
    assert upgrades._background_tier(_background(tier="historical_circuit_action_race_car")) == 1
    assert upgrades._background_tier(_background(tier="unknown")) == 0


def test_team_selection_keeps_constructor_and_accepts_better_flickr(monkeypatch):
    async def roster(*_args):
        return [ConstructorData("ferrari", "Ferrari")], "roster-live"

    async def search(*_args):
        return [_candidate(provider="flickr", suffix="new")], "search-live"

    calls = []

    async def acquire(_session, _config, candidate):
        calls.append(candidate.provider)
        return Path(f"/{candidate.provider}.jpg"), "image-live"

    monkeypatch.setattr(upgrades, "load_constructors", roster)
    monkeypatch.setattr(upgrades, "search_flickr_team_photos", search)
    monkeypatch.setattr(upgrades, "acquire_candidate_image", acquire)
    monkeypatch.setattr(upgrades, "_quality_decision", lambda *_args, **_kwargs: (True, "better", {}))
    result, reason, evidence = asyncio.run(
        upgrades._select_team_upgrade(None, None, {}, 2026, _candidate(), logging.getLogger())
    )
    assert result[0].constructor_id == "ferrari"
    assert calls == ["commons", "flickr"]
    assert reason == "better"
    assert evidence["roster_source"] == "roster-live"


def test_team_selection_skip_and_failure_paths(monkeypatch):
    assert asyncio.run(
        upgrades._select_team_upgrade(None, None, {}, 2026, _candidate(provider="flickr"), None)
    )[1] == "already uses Flickr"

    async def roster(*_args):
        return [ConstructorData("mclaren", "McLaren")], "roster"

    monkeypatch.setattr(upgrades, "load_constructors", roster)
    result = asyncio.run(
        upgrades._select_team_upgrade(None, None, {}, 2026, _candidate(), None)
    )
    assert result[1] == "stored constructor is absent from the current roster"

    async def same_roster(*_args):
        return [ConstructorData("ferrari", "Ferrari")], "roster"

    async def search(*_args):
        return [_candidate(provider="flickr")], "search"

    async def fail(*_args):
        raise RuntimeError("download failed")

    monkeypatch.setattr(upgrades, "load_constructors", same_roster)
    monkeypatch.setattr(upgrades, "search_flickr_team_photos", search)
    monkeypatch.setattr(upgrades, "acquire_candidate_image", fail)
    result = asyncio.run(
        upgrades._select_team_upgrade(None, None, {}, 2026, _candidate(), None)
    )
    assert result[1] == "download failed"

    async def acquire(_session, _config, candidate):
        return Path(f"/{candidate.provider}.jpg"), "image"

    monkeypatch.setattr(upgrades, "acquire_candidate_image", acquire)
    monkeypatch.setattr(upgrades, "_quality_decision", lambda *_args: (False, "not better", {}))
    result = asyncio.run(
        upgrades._select_team_upgrade(None, None, {}, 2026, _candidate(), None)
    )
    assert result[1] == "no materially better Flickr team-car source"


def test_background_selection_paths(monkeypatch):
    flickr = _background(provider="flickr", tier="exact_event_action_race_car")
    refresh_values = []

    async def search(*_args, **kwargs):
        refresh_values.append(kwargs.get("refresh"))
        return [flickr], "search"

    calls = 0

    async def old_fail_then_new(*_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("old unavailable")
        return Path("/flickr.jpg"), "image"

    monkeypatch.setattr(upgrades, "search_flickr_backgrounds", search)
    monkeypatch.setattr(upgrades, "_acquire_background_image", old_fail_then_new)
    monkeypatch.setattr(upgrades, "_quality_decision", lambda *_args, **kwargs: (True, "better", {"specific": kwargs["specificity_gain"]}))
    selected = asyncio.run(
        upgrades._select_background_upgrade(None, None, {}, None, _background(), None)
    )
    assert selected[0][0].provider == "flickr"
    assert refresh_values == [True]

    calls = 0

    async def old_then_fail(*_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            return Path("/old.jpg"), "old"
        raise RuntimeError("new failed")

    monkeypatch.setattr(upgrades, "_acquire_background_image", old_then_fail)
    rejected = asyncio.run(
        upgrades._select_background_upgrade(None, None, {}, None, _background(), None)
    )
    assert rejected[1] == "new failed"


def test_background_candidate_without_gain(monkeypatch):
    async def search(*_args, **_kwargs):
        return [_background(provider="flickr")], "search"

    async def acquire(_session, _config, candidate):
        return Path(f"/{candidate.provider}.jpg"), "image"

    monkeypatch.setattr(upgrades, "search_flickr_backgrounds", search)
    monkeypatch.setattr(upgrades, "_acquire_background_image", acquire)
    monkeypatch.setattr(upgrades, "_quality_decision", lambda *_args, **_kwargs: (False, "not better", {}))
    selected = asyncio.run(
        upgrades._select_background_upgrade(None, None, {}, None, _background(), None)
    )
    assert selected == (None, "no materially better Flickr background source", {})


def test_upgrade_episode_round_paths(tmp_path, monkeypatch):
    state = Formula1State(":memory:")
    show = _show()
    race = _race()
    assert asyncio.run(
        upgrades._upgrade_episode_round(None, state, {}, show, race, None, "current", "run", None)
    ) == ([], {}, False)
    state.save_episode_round_source(2026, 1, "ferrari", {"candidate": _candidate(provider="flickr").as_dict()})
    records, actions, changed = asyncio.run(
        upgrades._upgrade_episode_round(None, state, {}, show, race, None, "current", "run", None)
    )
    assert records[0]["status"] == "already-flickr"
    assert actions == {} and changed is False

    state.save_episode_round_source(2026, 1, "ferrari", {"candidate": _candidate().as_dict()})

    async def selected(*_args):
        return (_candidate(provider="flickr", suffix="new"), Path("/new.jpg")), "better", {}

    monkeypatch.setattr(upgrades, "_select_team_upgrade", selected)
    monkeypatch.setattr(upgrades, "reconcile_episode_posters", lambda *_args: ({1: "ref"}, {1: "create", 2: "preserve-manual"}))
    monkeypatch.setattr(upgrades, "_save_round_source", lambda *args: None)
    records, actions, changed = asyncio.run(
        upgrades._upgrade_episode_round(None, state, {}, show, race, None, "all", "run2", None)
    )
    assert changed is True
    assert records[0]["status"] == "upgraded"
    assert records[0]["details"]["manual_preserved"] == 1
    assert actions[(1, 1)] == "create"

    monkeypatch.setattr(upgrades, "reconcile_episode_posters", lambda *_args: ({}, {1: "preserve-manual"}))
    records, _actions, changed = asyncio.run(
        upgrades._upgrade_episode_round(None, state, {}, show, race, None, "all", "run3", None)
    )
    assert records[0]["status"] == "preserved-manual" and changed is False
    state.close()


def test_upgrade_active_show_guards_and_manual_preservation(tmp_path, monkeypatch):
    state = Formula1State(":memory:")
    show = _show()
    race = _race()
    config = _config(tmp_path)
    assert asyncio.run(
        upgrades._upgrade_active_show(None, state, config, show, race, None, "current", "run", None)
    ) == ([], False)
    source = {
        "candidate": _candidate().as_dict(),
        "background_candidate": _background().as_dict(),
    }
    state.save_show_rotation(
        "show:2026", 2026, 2, "ferrari", source,
        tmp_path / "poster.png", "poster", tmp_path / "background.png", "background",
    )
    assert asyncio.run(
        upgrades._upgrade_active_show(None, state, config, show, race, None, "current", "run", None)
    ) == ([], False)
    state.save_show_rotation(
        "show:2026", 2026, 1, "ferrari", source,
        tmp_path / "poster.png", "poster", tmp_path / "background.png", "background",
    )
    monkeypatch.setattr(upgrades, "_asset_integrity", lambda *_args: "manual")
    records, changed = asyncio.run(
        upgrades._upgrade_active_show(None, state, config, show, race, None, "current", "run", None)
    )
    assert changed is False
    assert [record["status"] for record in records] == ["preserved-manual", "preserved-manual"]
    state.close()


def test_upgrade_active_show_no_better_and_without_background(tmp_path, monkeypatch):
    state = Formula1State(":memory:")
    show = _show()
    race = _race()
    config = _config(tmp_path)
    source = {"candidate": _candidate(provider="flickr").as_dict()}
    state.save_show_rotation(
        "show:2026", 2026, 1, "ferrari", source,
        tmp_path / "poster.png", "poster", tmp_path / "background.png", "background",
    )
    monkeypatch.setattr(upgrades, "_asset_integrity", lambda *_args: "managed")
    records, changed = asyncio.run(
        upgrades._upgrade_active_show(None, state, config, show, race, None, "current", "run", None)
    )
    assert records[0]["status"] == "already-flickr" and changed is False
    state.close()


@pytest.mark.parametrize(
    ("provider", "expected"),
    [("commons", "no-better-candidate"), ("flickr", "no-better-candidate")],
)
def test_upgrade_active_show_background_no_better(tmp_path, monkeypatch, provider, expected):
    state = Formula1State(":memory:")
    show = _show()
    race = _race()
    source = {
        "candidate": _candidate(provider="flickr").as_dict(),
        "background_candidate": _background(provider=provider).as_dict(),
    }
    state.save_show_rotation(
        "show:2026", 2026, 1, "ferrari", source,
        tmp_path / "poster.png", "poster", tmp_path / "background.png", "background",
    )
    monkeypatch.setattr(upgrades, "_asset_integrity", lambda *_args: "managed")
    async def no_background_upgrade(*_args):
        return None, "no gain", {}

    monkeypatch.setattr(upgrades, "_select_background_upgrade", no_background_upgrade)
    records, changed = asyncio.run(
        upgrades._upgrade_active_show(
            None, state, _config(tmp_path), show, race, None, "current", "run", None
        )
    )
    assert records[1]["status"] == expected and changed is False
    state.close()


def test_upgrade_active_show_replaces_poster_and_background(tmp_path, monkeypatch):
    state = Formula1State(":memory:")
    show = _show()
    race = _race()
    config = _config(tmp_path)
    source = {
        "candidate": _candidate().as_dict(),
        "background_candidate": _background().as_dict(),
        "generated_checksums": {"old": "retained"},
    }
    state.save_show_rotation(
        "show:2026", 2026, 1, "ferrari", source,
        tmp_path / "poster.png", "poster", tmp_path / "background.png", "background",
    )
    new_poster = _candidate(provider="flickr", suffix="new")
    new_background = _background(provider="flickr", tier="exact_event_action_race_car")

    async def team(*_args):
        return (new_poster, Path("/new-poster.jpg")), "better", {
            "roster_source": "roster", "search_source": "search", "image_source": "image"
        }

    async def background(*_args):
        return (new_background, Path("/new-background.jpg")), "better", {
            "search_source": "search-bg", "image_source": "image-bg"
        }

    monkeypatch.setattr(upgrades, "_asset_integrity", lambda *_args: "managed")
    monkeypatch.setattr(upgrades, "_select_team_upgrade", team)
    monkeypatch.setattr(upgrades, "_select_background_upgrade", background)
    monkeypatch.setattr(upgrades, "render_show_poster", lambda *_args: "poster-new")
    monkeypatch.setattr(upgrades, "render_show_background", lambda *_args: "background-new")
    monkeypatch.setattr(upgrades, "_perceptual_hash", lambda *_args: "hash")
    monkeypatch.setattr(upgrades, "_show_render_fingerprints", lambda _config: ("poster-fp", "background-fp"))
    monkeypatch.setattr(upgrades, "_show_render_fingerprint", lambda _config: "pair-fp")
    records, changed = asyncio.run(
        upgrades._upgrade_active_show(None, state, config, show, race, None, "all", "run", None)
    )
    assert changed is True
    assert [record["lane"] for record in records] == ["show_poster", "show_background"]
    updated = state.show_rotation("show:2026")
    assert updated["poster_checksum"] == "poster-new"
    assert updated["background_checksum"] == "background-new"
    assert updated["source"]["generated_checksums"]["old"] == "retained"
    assert updated["source"]["candidate"]["provider"] == "flickr"
    state.close()


def test_upgrade_orchestrator_scope_and_guards(tmp_path, monkeypatch):
    state = Formula1State(":memory:")
    with pytest.raises(ValueError, match="current or all"):
        asyncio.run(upgrades.upgrade_formula1_artwork(None, state, _config(tmp_path), _show(), {}, {}, "bad", "run", None))
    with pytest.raises(RuntimeError, match="flickr"):
        asyncio.run(upgrades.upgrade_formula1_artwork(None, state, _config(tmp_path, flickr=False), _show(), {}, {}, "all", "run", None))
    assert asyncio.run(
        upgrades.upgrade_formula1_artwork(None, state, _config(tmp_path), _show(()), {}, {}, "all", "run", None)
    ) == upgrades.Formula1UpgradeResult()

    seen = []

    async def episode(_session, _state, _config, _show_value, race, *_args):
        seen.append(race.round_number)
        return ([{"status": "upgraded"}], {(race.round_number, 1): "update"}, True)

    async def active(*_args):
        return ([{"status": "unchanged"}], False)

    monkeypatch.setattr(upgrades, "_upgrade_episode_round", episode)
    monkeypatch.setattr(upgrades, "_upgrade_active_show", active)
    wrote = []
    monkeypatch.setattr(upgrades, "write_attribution_reports", lambda *_args: wrote.append(True))
    races = {1: _race(1), 2: _race(2)}
    result = asyncio.run(
        upgrades.upgrade_formula1_artwork(None, state, _config(tmp_path), _show((1, 2)), races, {}, "current", "run", None)
    )
    assert seen == [2] and result.changed and wrote == [True]
    seen.clear()
    result = asyncio.run(
        upgrades.upgrade_formula1_artwork(None, state, _config(tmp_path), _show((1, 2)), {1: _race(1)}, {}, "all", "run", None)
    )
    assert seen == [1]
    seen.clear()
    result = asyncio.run(
        upgrades.upgrade_formula1_artwork(None, state, _config(tmp_path), _show((2,)), {}, {}, "current", "run", None)
    )
    assert result.changed is False and seen == []
    state.close()


def test_upgrade_report_handles_record_without_reason(tmp_path):
    record = {
        "season_year": 2026,
        "round_number": 1,
        "lane": "episode",
        "status": "unchanged",
        "old_provider": None,
        "new_provider": None,
        "details": {},
    }
    report = upgrades.write_upgrade_report(_config(tmp_path), "no-reason", "current", [record])
    assert report and "none -> none" in report.with_suffix(".txt").read_text()
