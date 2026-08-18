import os

import pytest

import docker_entrypoint


class ExecCalled(Exception):
    pass


def stop_at_exec(monkeypatch, calls):
    def fake_exec(command, arguments):
        calls.append(("exec", command, arguments))
        raise ExecCalled

    monkeypatch.setattr(os, "execvp", fake_exec)


def test_root_entrypoint_prepares_managed_paths_and_drops_privileges(
    monkeypatch, tmp_path
):
    calls = []
    config_dir = tmp_path / "config"
    log_file = config_dir / "logs" / "metafusion.log"
    log_file.parent.mkdir(parents=True)
    log_file.write_text("existing", encoding="utf-8")
    report_file = config_dir / "reports" / "artwork-gaps-existing.txt"
    report_file.parent.mkdir(parents=True)
    report_file.write_text("existing", encoding="utf-8")

    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "old-home"))
    monkeypatch.setenv("PUID", "99")
    monkeypatch.setenv("PGID", "100")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        os,
        "chown",
        lambda path, uid, gid, follow_symlinks: calls.append(
            ("chown", str(path), uid, gid, follow_symlinks)
        ),
    )
    monkeypatch.setattr(os, "setgroups", lambda groups: calls.append(("groups", groups)))
    monkeypatch.setattr(os, "setgid", lambda gid: calls.append(("gid", gid)))
    monkeypatch.setattr(os, "setuid", lambda uid: calls.append(("uid", uid)))
    stop_at_exec(monkeypatch, calls)

    with pytest.raises(ExecCalled):
        docker_entrypoint.main(["python", "metafusion.py"])

    assert (config_dir / "cache").is_dir()
    assert (config_dir / "config_template.yml").read_bytes() == (
        docker_entrypoint.CONFIG_TEMPLATE_SOURCE.read_bytes()
    )
    assert (config_dir / "config_template.yml").stat().st_mode & 0o777 == 0o664
    assert any(call[:4] == ("chown", str(log_file), 99, 100) for call in calls)
    assert any(call[:4] == ("chown", str(report_file), 99, 100) for call in calls)
    assert calls[-4:] == [
        ("groups", []),
        ("gid", 100),
        ("uid", 99),
        ("exec", "python", ["python", "metafusion.py"]),
    ]
    assert os.environ["HOME"] == str(config_dir)


def test_runtime_path_repair_is_limited_to_config_managed_directories(
    monkeypatch, tmp_path
):
    calls = []
    config_dir = tmp_path / "config"
    unmanaged_asset = tmp_path / "kometa" / "assets" / "movie" / "poster.jpg"
    unmanaged_asset.parent.mkdir(parents=True)
    unmanaged_asset.write_text("existing", encoding="utf-8")

    monkeypatch.setattr(
        os,
        "chown",
        lambda path, uid, gid, follow_symlinks: calls.append(str(path)),
    )

    docker_entrypoint.prepare_runtime_paths(config_dir, 99, 100)

    assert (config_dir / "logs").is_dir()
    assert (config_dir / "cache").is_dir()
    assert (config_dir / "reports").is_dir()
    assert str(unmanaged_asset) not in calls
    assert all(str(tmp_path / "kometa") not in path for path in calls)


def test_explicit_docker_user_is_honored_and_receives_template(monkeypatch, tmp_path):
    calls = []
    config_dir = tmp_path / "not-created"
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "old-home"))
    monkeypatch.setenv("PUID", "10001")
    monkeypatch.setenv("PGID", "10001")
    monkeypatch.setattr(os, "geteuid", lambda: 99)
    monkeypatch.setattr(docker_entrypoint, "_set_owner", lambda *args: None)
    monkeypatch.setattr(os, "setuid", lambda uid: pytest.fail("must not remap UID"))
    monkeypatch.setattr(os, "setgid", lambda gid: pytest.fail("must not remap GID"))
    stop_at_exec(monkeypatch, calls)

    with pytest.raises(ExecCalled):
        docker_entrypoint.main(["python", "metafusion.py"])

    assert (config_dir / "config_template.yml").exists()
    assert calls == [("exec", "python", ["python", "metafusion.py"])]


def test_healthcheck_drops_identity_without_rewalking_paths(monkeypatch, tmp_path):
    calls = []
    config_dir = tmp_path / "not-created"
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "old-home"))
    monkeypatch.setenv("PUID", "99")
    monkeypatch.setenv("PGID", "100")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "setgroups", lambda groups: calls.append(("groups", groups)))
    monkeypatch.setattr(os, "setgid", lambda gid: calls.append(("gid", gid)))
    monkeypatch.setattr(os, "setuid", lambda uid: calls.append(("uid", uid)))
    stop_at_exec(monkeypatch, calls)

    with pytest.raises(ExecCalled):
        docker_entrypoint.main(["--healthcheck", "python", "healthcheck.py"])

    assert not config_dir.exists()
    assert calls == [
        ("groups", []),
        ("gid", 100),
        ("uid", 99),
        ("exec", "python", ["python", "healthcheck.py"]),
    ]


@pytest.mark.parametrize("name,value", [("PUID", "abc"), ("PGID", "0"), ("PUID", "-1")])
def test_invalid_identity_is_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        docker_entrypoint.parse_id(name, 10001)


def test_config_template_is_refreshed_without_touching_config(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yml"
    config_file.write_text("plex:\n  url: custom\n", encoding="utf-8")
    destination = config_dir / "config_template.yml"
    destination.write_text("outdated\n", encoding="utf-8")
    source = tmp_path / "packaged-template.yml"
    source.write_text("settings:\n  mode: kometa\n", encoding="utf-8")

    monkeypatch.setattr(docker_entrypoint, "_set_owner", lambda *args: None)

    assert docker_entrypoint.sync_config_template(config_dir, 99, 100, source)
    assert destination.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert config_file.read_text(encoding="utf-8") == "plex:\n  url: custom\n"
    assert destination.stat().st_mode & 0o777 == 0o664

    monkeypatch.setattr(
        os,
        "replace",
        lambda *args: pytest.fail("unchanged template must not be rewritten"),
    )
    assert not docker_entrypoint.sync_config_template(config_dir, 99, 100, source)


def test_config_template_destination_must_not_be_symlink(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    source = tmp_path / "packaged-template.yml"
    source.write_text("settings: {}\n", encoding="utf-8")
    destination = config_dir / "config_template.yml"
    destination.symlink_to(tmp_path / "outside.yml")

    with pytest.raises(RuntimeError, match="cannot be a symbolic link"):
        docker_entrypoint.sync_config_template(config_dir, 99, 100, source)
