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
    assert any(call[:4] == ("chown", str(log_file), 99, 100) for call in calls)
    assert calls[-4:] == [
        ("groups", []),
        ("gid", 100),
        ("uid", 99),
        ("exec", "python", ["python", "metafusion.py"]),
    ]
    assert os.environ["HOME"] == str(config_dir)


def test_explicit_docker_user_is_honored_without_remapping(monkeypatch, tmp_path):
    calls = []
    config_dir = tmp_path / "not-created"
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "old-home"))
    monkeypatch.setenv("PUID", "10001")
    monkeypatch.setenv("PGID", "10001")
    monkeypatch.setattr(os, "geteuid", lambda: 99)
    monkeypatch.setattr(os, "setuid", lambda uid: pytest.fail("must not remap UID"))
    monkeypatch.setattr(os, "setgid", lambda gid: pytest.fail("must not remap GID"))
    stop_at_exec(monkeypatch, calls)

    with pytest.raises(ExecCalled):
        docker_entrypoint.main(["python", "metafusion.py"])

    assert not config_dir.exists()
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
