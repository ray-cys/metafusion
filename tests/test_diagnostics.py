from helper.diagnostics import write_support_report


def test_support_report_omits_configuration_values(tmp_path):
    config = {
        "settings": {"mode": "plex", "dry_run": False},
        "plex": {
            "url": "http://private-plex:32400",
            "token": "secret-plex-token",
            "path_mappings": ["/private/source=>/media"],
        },
        "plex_libraries": ["Private Movies"],
        "tmdb": {"api_key": "secret-tmdb-key"},
        "plex_metadata": {"enabled": True, "policy": "fill_missing"},
    }

    report = write_support_report(
        config,
        base_dir=tmp_path,
        environ={"PLEX_TOKEN": "secret-plex-token", "TMDB_API_KEY": "secret-tmdb-key"},
    )
    contents = report.read_text(encoding="utf-8")

    assert "secret-plex-token" not in contents
    assert "secret-tmdb-key" not in contents
    assert "private-plex" not in contents
    assert "Private Movies" not in contents
    assert "/private/source" not in contents
    assert "PLEX_TOKEN" in contents
    assert "TMDB_API_KEY" in contents


def test_support_reports_do_not_overwrite_within_one_second(tmp_path):
    config = {
        "settings": {"mode": "kometa", "dry_run": True},
        "plex": {"path_mappings": []},
        "plex_libraries": [],
        "plex_metadata": {},
    }

    first = write_support_report(config, base_dir=tmp_path, environ={})
    second = write_support_report(config, base_dir=tmp_path, environ={})

    assert first != second
    assert first.exists()
    assert second.exists()
