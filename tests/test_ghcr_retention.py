import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "cleanup_ghcr.py"
SPEC = importlib.util.spec_from_file_location("cleanup_ghcr", SCRIPT)
cleanup_ghcr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup_ghcr)


def version(identifier, digest, tags, created_at):
    return {
        "id": identifier,
        "name": digest,
        "created_at": created_at.isoformat(),
        "metadata": {"container": {"tags": tags}},
    }


def test_retention_preserves_tags_cosign_and_platform_children():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    old = now - timedelta(days=60)
    versions = [
        version(1, "sha256:index", ["main", "latest", "1.0.0"], old),
        version(2, "sha256:amd64", [], old),
        version(3, "sha256:arm64", [], old),
        version(4, "sha256:signature", ["sha256-index.sig"], old),
        version(5, "sha256:orphan", [], old),
    ]

    def fetch_manifest(digest):
        if digest == "sha256:index":
            return {
                "manifests": [
                    {"digest": "sha256:amd64"},
                    {"digest": "sha256:arm64"},
                ]
            }
        return {"schemaVersion": 2}

    protected = cleanup_ghcr.protected_manifest_digests(versions, fetch_manifest)
    candidates = cleanup_ghcr.deletion_candidates(
        versions,
        protected,
        now=now,
        retention_days=30,
        keep_untagged=0,
    )

    assert protected == {
        "sha256:index",
        "sha256:amd64",
        "sha256:arm64",
        "sha256:signature",
    }
    assert [candidate["name"] for candidate in candidates] == ["sha256:orphan"]


def test_retention_keeps_recent_and_minimum_untagged_versions():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    versions = [
        version(1, "sha256:new", [], now - timedelta(days=1)),
        version(2, "sha256:kept", [], now - timedelta(days=40)),
        version(3, "sha256:old", [], now - timedelta(days=50)),
    ]

    candidates = cleanup_ghcr.deletion_candidates(
        versions,
        set(),
        now=now,
        retention_days=30,
        keep_untagged=2,
    )

    assert [candidate["name"] for candidate in candidates] == ["sha256:old"]


def test_retention_protects_children_of_kept_untagged_index():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    old = now - timedelta(days=60)
    versions = [
        version(1, "sha256:kept-index", [], old),
        version(2, "sha256:child", [], old),
        version(3, "sha256:orphan", [], old),
    ]
    roots = cleanup_ghcr.retention_root_digests(
        versions,
        now=now,
        retention_days=30,
        keep_untagged=1,
    )

    protected = cleanup_ghcr.protected_manifest_digests(
        versions,
        lambda digest: (
            {"manifests": [{"digest": "sha256:child"}]}
            if digest == "sha256:kept-index"
            else {"schemaVersion": 2}
        ),
        root_digests=roots,
    )
    candidates = cleanup_ghcr.deletion_candidates(
        versions,
        protected,
        now=now,
        retention_days=30,
        keep_untagged=1,
    )

    assert protected == {"sha256:kept-index", "sha256:child"}
    assert [candidate["name"] for candidate in candidates] == ["sha256:orphan"]


def test_cleanup_main_supports_dry_run_and_delete(monkeypatch, capsys):
    now = datetime.now(timezone.utc)
    versions = [
        version(1, "sha256:index", ["develop"], now - timedelta(days=60)),
        version(2, "sha256:orphan", [], now - timedelta(days=60)),
    ]
    deletions = []
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "ray-cys/metafusion")
    monkeypatch.setenv("GHCR_RETENTION_DAYS", "30")
    monkeypatch.setenv("GHCR_KEEP_UNTAGGED", "0")
    monkeypatch.setattr(
        cleanup_ghcr, "_package_api_prefix", lambda *_args: "https://api/versions"
    )
    monkeypatch.setattr(cleanup_ghcr, "_all_versions", lambda *_args: versions)
    monkeypatch.setattr(
        cleanup_ghcr,
        "_registry_token",
        lambda *_args, **_kwargs: "registry",
    )

    def request(url, **kwargs):
        if "/manifests/" in url:
            return {"schemaVersion": 2}
        if kwargs.get("method") == "DELETE":
            deletions.append(url)
            return None
        raise AssertionError(url)

    monkeypatch.setattr(cleanup_ghcr, "_request_json", request)

    assert cleanup_ghcr.main(["--dry-run"]) == 0
    assert deletions == []
    assert "Would delete sha256:orphan" in capsys.readouterr().out

    assert cleanup_ghcr.main([]) == 0
    assert deletions == ["https://api/versions/2"]


def test_cleanup_main_fails_closed_when_tag_graph_is_unavailable(
    monkeypatch, capsys
):
    now = datetime.now(timezone.utc)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "ray-cys/metafusion")
    monkeypatch.setattr(
        cleanup_ghcr, "_package_api_prefix", lambda *_args: "https://api/versions"
    )
    monkeypatch.setattr(
        cleanup_ghcr,
        "_all_versions",
        lambda *_args: [version(1, "sha256:index", ["main"], now)],
    )
    monkeypatch.setattr(
        cleanup_ghcr,
        "_registry_token",
        lambda *_args, **_kwargs: "registry",
    )
    monkeypatch.setattr(
        cleanup_ghcr,
        "_request_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )

    assert cleanup_ghcr.main([]) == 1
    assert "refusing cleanup" in capsys.readouterr().err
