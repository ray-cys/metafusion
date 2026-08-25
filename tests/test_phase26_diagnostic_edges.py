import json

from helper import diagnostics
from helper.io import sha256_file


def _event(old_path, new_path, checksum="checksum"):
    return {
        "previous_destination": str(old_path),
        "new_destination": str(new_path),
        "previous_checksum": checksum,
    }


def _config(root, *, mode="kometa", policy="managed"):
    config = {
        "settings": {"mode": mode, "path": str(root)},
        "assets": {"update_policy": policy},
        "plex": {"path_mappings": [f"/plex=>{root}", "invalid"]},
    }
    return config


def test_metadata_helpers_handle_empty_and_non_mapping_paths(tmp_path):
    assert list(diagnostics._flatten_metadata_fields({"seasons": {1: {"title": "One"}}})) == [
        ("seasons.1.title", "One")
    ]
    assert diagnostics._metadata_path_value({"a": "not-a-map"}, "a.b") == (None, False)
    assert diagnostics._metadata_path_value({"seasons": {1: "one"}}, "seasons.1") == (
        "one",
        True,
    )
    report = diagnostics.write_metadata_audit_report(
        [], mode="kometa", base_dir=tmp_path
    )
    assert "no eligible metadata fields" in report.read_text(encoding="utf-8")


def test_artwork_selection_details_render_all_explanations():
    lines = []
    diagnostics._append_artwork_selection_details(
        lines,
        {
            "provider": "tmdb",
            "provider_image_id": "1",
            "tmdb_canonical": True,
            "selection_reason": "highest score",
            "provider_attempts": [
                {"provider": "tmdb", "status": "selected", "candidates": 2}
            ],
            "quality_components": {
                "resolution": 3,
                "vote": 2,
                "aspect": 1,
                "language": 4,
            },
            "rejected_candidates": [
                {
                    "width": 10,
                    "height": 20,
                    "language": "en",
                    "vote": 1,
                    "quality_score": 2,
                    "reasons": ["small"],
                }
            ],
        },
    )
    rendered = "\n".join(lines)
    assert "providers attempted" in rendered
    assert "selected components" in rendered
    assert "highest-scoring rejected" in rendered
    assert "TMDb canonical: yes" in rendered


def test_managed_asset_roots_and_current_destinations(tmp_path):
    kometa_root = diagnostics._managed_asset_roots(_config(tmp_path))
    assert kometa_root == [(tmp_path / "assets").resolve()]
    plex_roots = diagnostics._managed_asset_roots(_config(tmp_path, mode="plex"))
    assert plex_roots == [tmp_path.resolve()]
    assert diagnostics._managed_asset_roots(None) == []
    assert diagnostics._current_asset_destinations(
        {
            "bad": [],
            "good": {
                "poster_path": tmp_path / "poster.jpg",
                "background_path": None,
                "seasons": {
                    "0": {"season_path": tmp_path / "s0.jpg"},
                    "1": "bad",
                },
            },
        }
    ) == {(tmp_path / "poster.jpg").resolve(), (tmp_path / "s0.jpg").resolve()}


def test_destination_reconciliation_preserves_every_unproven_case(tmp_path, monkeypatch):
    root = tmp_path / "output"
    assets = root / "assets"
    assets.mkdir(parents=True)
    old = assets / "old.jpg"
    new = assets / "new.jpg"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    old_checksum = sha256_file(old)
    current_checksum = sha256_file(new)
    event = _event(old, new, old_checksum)

    cases = [
        (None, current_checksum, None, "reconciliation was not enabled"),
        (_config(root, policy="fill_missing"), current_checksum, None, "policy is not managed"),
        (_config(root), current_checksum, {old.resolve()}, "still claimed"),
        (_config(tmp_path / "elsewhere"), current_checksum, None, "outside configured"),
        (_config(root), None, None, "no current managed checksum"),
    ]
    for config, checksum, claimed, reason in cases:
        status, detail = diagnostics._reconcile_managed_destination(
            event, config, checksum, claimed
        )
        assert status == "preserved"
        assert reason in detail

    same = _event(old, old, old_checksum)
    assert diagnostics._reconcile_managed_destination(
        same, _config(root), current_checksum
    )[1].startswith("old and current")

    new.unlink()
    assert diagnostics._reconcile_managed_destination(
        event, _config(root), current_checksum
    )[1].startswith("current destination")
    new.write_bytes(b"new")
    old.unlink()
    assert diagnostics._reconcile_managed_destination(
        event, _config(root), current_checksum
    )[0] == "already_absent"

    old.mkdir()
    assert diagnostics._reconcile_managed_destination(
        event, _config(root), current_checksum
    )[1].startswith("old destination is not")
    old.rmdir()
    old.write_bytes(b"old")
    no_checksum = dict(event, previous_checksum="")
    assert "no prior" in diagnostics._reconcile_managed_destination(
        no_checksum, _config(root), current_checksum
    )[1]
    assert "current destination no longer" in diagnostics._reconcile_managed_destination(
        event, _config(root), "wrong"
    )[1]
    old.write_bytes(b"modified")
    assert "old destination was modified" in diagnostics._reconcile_managed_destination(
        event, _config(root), current_checksum
    )[1]

    monkeypatch.setattr(
        diagnostics,
        "sha256_file",
        lambda _path: (_ for _ in ()).throw(OSError("denied")),
    )
    assert "safe removal failed" in diagnostics._reconcile_managed_destination(
        event, _config(root), current_checksum
    )[1]


def test_destination_history_reconciles_season_and_json_companion(tmp_path):
    root = tmp_path / "kometa"
    assets = root / "assets"
    old = assets / "show-old" / "Season01.jpg"
    new = assets / "show-new" / "Season01.jpg"
    old.parent.mkdir(parents=True)
    new.parent.mkdir(parents=True)
    old.write_bytes(b"same")
    new.write_bytes(b"same")
    checksum = sha256_file(new)
    cache = {
        "tv:1": {
            "title": "Show",
            "year": 2020,
            "seasons": {"1": {"season_checksum": checksum, "season_path": str(new)}},
            "destination_history": [
                {
                    "asset_type": "season",
                    "season_number": 1,
                    "previous_destination": str(old),
                    "new_destination": str(new),
                    "previous_checksum": checksum,
                    "reported_at": None,
                }
            ],
        }
    }
    report = diagnostics.write_destination_history_report(
        cache, base_dir=tmp_path, config=_config(root)
    )
    assert not old.exists()
    assert "removed" in report.read_text(encoding="utf-8")
    companion = report.with_suffix(".json")
    assert json.loads(companion.read_text(encoding="utf-8"))["data"]["entries"][0][
        "season_number"
    ] == 1


def test_unresolved_and_adoption_reports_cover_empty_and_resolved_sections(tmp_path):
    assert diagnostics.write_unresolved_work_report([], base_dir=tmp_path) is None
    unresolved = diagnostics.write_unresolved_work_report(
        [
            {
                "status": "resolved",
                "category": "artwork_missing",
                "library_name": "Movies",
                "media_type": "movie",
                "title": "Example",
                "asset_type": "poster",
                "resolved_at": "now",
            }
        ],
        base_dir=tmp_path,
    )
    assert "Open work\n- none" in unresolved.read_text(encoding="utf-8")
    assert diagnostics.write_adoption_audit_report([], base_dir=tmp_path) is None
    adoption = diagnostics.write_adoption_audit_report(
        [
            {
                "status": "installed",
                "library": "Shows",
                "title": "Example",
                "asset_type": "season",
                "season_number": 1,
                "provider": "tmdb",
                "plex_visibility": "pending",
                "destination": "poster.jpg",
            }
        ],
        base_dir=tmp_path,
    )
    assert "season 1" in adoption.read_text(encoding="utf-8")
