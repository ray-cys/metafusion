import re
from pathlib import Path
from xml.etree import ElementTree

import yaml

from helper.config import ENV_BINDINGS, SECRET_FILE_BINDINGS

REPO_ROOT = Path(__file__).parents[1]


def test_compose_defaults_are_safe_for_scheduler():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["metafusion"]
    environment = service["environment"]

    assert "version" not in compose
    assert service["image"] == "${METAFUSION_IMAGE:-ghcr.io/ray-cys/metafusion:main}"
    assert service["init"] is True
    assert service["read_only"] is True
    assert "user" not in service
    assert "cap_drop" not in service
    assert "no-new-privileges:true" in service["security_opt"]
    assert "healthcheck" in service
    assert environment["METAFUSION_RUN"] == "${METAFUSION_RUN-}"
    assert environment["RUN_CLEANUP"] == "${RUN_CLEANUP-}"
    assert "RUN_PROCESS" not in environment
    assert environment["PLEX_TOKEN"] == "${PLEX_TOKEN-}"
    assert environment["PLEX_TOKEN_FILE"] == "${PLEX_TOKEN_FILE-}"
    assert environment["TMDB_API_KEY"] == "${TMDB_API_KEY-}"
    assert environment["TMDB_API_KEY_FILE"] == "${TMDB_API_KEY_FILE-}"
    assert environment["PUID"] == "${PUID:-10001}"
    assert environment["PGID"] == "${PGID:-10001}"
    assert environment["PLEX_TIMEOUT"] == "${PLEX_TIMEOUT-}"
    assert environment["SHUTDOWN_TIMEOUT"] == "${SHUTDOWN_TIMEOUT-}"
    assert environment["INCREMENTAL"] == "${INCREMENTAL-}"
    assert environment["TMDB_CACHE_ENABLED"] == "${TMDB_CACHE_ENABLED-}"
    assert environment["TMDB_CACHE_MAX_MB"] == "${TMDB_CACHE_MAX_MB-}"
    assert environment["VALIDATE_OUTPUT"] == "${VALIDATE_OUTPUT-}"
    assert environment["HEALTH_FAIL_ON_JOB_ERROR"] == "${HEALTH_FAIL_ON_JOB_ERROR:-False}"
    assert environment["HEALTH_MAX_HEARTBEAT_AGE"] == "${HEALTH_MAX_HEARTBEAT_AGE:-120}"
    assert environment["STATUS_FILE"] == "/tmp/metafusion-status.json"
    assert service["stop_grace_period"] == "${STOP_GRACE_PERIOD:-20s}"
    assert service["healthcheck"]["start_period"] == "20s"


def test_configuration_reference_documents_every_supported_environment_variable():
    documentation = "\n".join(
        (
            (REPO_ROOT / "README.md").read_text(encoding="utf-8"),
            (REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8"),
        )
    )
    documented = {env_name for env_name, _path, _converter in ENV_BINDINGS}
    documented.update(
        file_env for file_env, _path, _direct_env in SECRET_FILE_BINDINGS
    )
    documented.update(
        {
            "CONFIG_DIR",
            "STATUS_FILE",
            "HEALTH_MAX_HEARTBEAT_AGE",
            "HEALTH_FAIL_ON_JOB_ERROR",
            "PUID",
            "PGID",
            "TZ",
            "CONFIG_PATH",
            "KOMETA_HOST_PATH",
            "STOP_GRACE_PERIOD",
            "METAFUSION_IMAGE",
        }
    )

    missing = sorted(name for name in documented if f"`{name}`" not in documentation)
    assert missing == []


def test_env_example_contains_every_current_application_binding():
    names = {
        line.split("=", 1)[0]
        for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    expected = {env_name for env_name, _path, _converter in ENV_BINDINGS}
    expected.update(
        file_env for file_env, _path, _direct_env in SECRET_FILE_BINDINGS
    )

    assert sorted(expected - names) == []


def test_dockerfile_uses_stable_python_minor_without_os_upgrade():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.startswith("FROM python:3.13-slim-bookworm@sha256:")
    assert "apt-get upgrade" not in dockerfile
    assert "PIP_NO_CACHE_DIR=1" in dockerfile
    assert "--require-hashes -r requirements.txt" in dockerfile
    assert "USER 10001:10001" not in dockerfile
    assert 'ENTRYPOINT ["python", "/app/docker_entrypoint.py"]' in dockerfile
    assert '"--healthcheck"' in dockerfile
    assert "HEALTHCHECK" in dockerfile


def test_docker_build_context_excludes_development_only_content():
    exclusions = set(
        (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    )

    assert {
        "**/.coverage",
        "**/.DS_Store",
        "**/.pytest_cache",
        "**/.ruff_cache",
        "asset",
        "docs",
        "requirements-dev.in",
        "requirements-dev.txt",
        "requirements.in",
        "unraid",
    } <= exclusions
    assert "requirements.txt" not in exclusions


def test_packaged_configuration_references_are_versioned_and_in_image_context():
    references = {
        REPO_ROOT / "config" / "config_template.yml",
        REPO_ROOT / "config" / "examples" / "kometa.yml",
        REPO_ROOT / "config" / "examples" / "plex.yml",
    }
    assert all(path.is_file() for path in references)

    dockerignore = set(
        (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    )
    assert "config/" not in dockerignore
    assert {
        "!config/config_template.yml",
        "!config/examples/",
        "!config/examples/*.yml",
    } <= dockerignore

    gitignore = set(
        (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    )
    assert "/config/" not in gitignore
    assert {
        "!/config/config_template.yml",
        "!/config/examples/",
        "!/config/examples/kometa.yml",
        "!/config/examples/plex.yml",
    } <= gitignore

    workflow = (REPO_ROOT / ".github" / "workflows" / "docker-latest.yml").read_text(
        encoding="utf-8"
    )
    assert (
        'cmp "${config_dir}/config_template.yml" config/config_template.yml'
        in workflow
    )


def test_generated_yaml_quotes_schedule_times_unambiguously():
    references = (
        REPO_ROOT / "config" / "config_template.yml",
        REPO_ROOT / "config" / "examples" / "kometa.yml",
        REPO_ROOT / "config" / "examples" / "plex.yml",
    )

    for reference in references:
        document = reference.read_text(encoding="utf-8")
        assert '  - "06:00"' in document
        assert '  - "18:30"' in document
        assert yaml.safe_load(document)["settings"]["run_times"] == [
            "06:00",
            "18:30",
        ]


def test_generated_dependency_manifests_are_self_contained_and_in_sync():
    runtime_input = (REPO_ROOT / "requirements.in").read_text(encoding="utf-8")
    runtime_manifest = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    dev_input = (REPO_ROOT / "requirements-dev.in").read_text(encoding="utf-8")
    dev_manifest = (REPO_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")

    pin_pattern = re.compile(r"(?m)^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\\\s]+)")

    def pins(document):
        return {
            name.lower().replace("_", "-"): version
            for name, version in pin_pattern.findall(document)
        }

    def assert_hashed(document):
        blocks = re.split(
            r"(?m)(?=^[A-Za-z0-9][A-Za-z0-9_.-]*==)", document
        )
        package_blocks = [
            block
            for block in blocks
            if pin_pattern.match(block)
        ]
        assert package_blocks
        assert all("--hash=sha256:" in block for block in package_blocks)

    runtime_input_pins = pins(runtime_input)
    runtime_pins = pins(runtime_manifest)
    dev_input_pins = pins(dev_input)
    dev_pins = pins(dev_manifest)

    assert re.search(r"(?m)^\s*(?:-r|--requirement)\s+", runtime_manifest) is None
    assert runtime_input_pins.items() <= runtime_pins.items()
    assert dev_input_pins.items() <= dev_pins.items()
    assert runtime_pins.items() <= dev_pins.items()
    assert_hashed(runtime_manifest)
    assert_hashed(dev_manifest)


def test_release_docs_match_local_feature_branch_publication_policy():
    release_docs = (REPO_ROOT / "docs" / "release-testing.md").read_text(
        encoding="utf-8"
    )

    assert "`codex/**` phase branch" not in release_docs
    assert "Local feature or phase branch" in release_docs
    assert "local branches are not pushed" in release_docs


def test_ci_smoke_tests_unraid_runtime_identity():
    workflow = (REPO_ROOT / ".github/workflows/docker-latest.yml").read_text(
        encoding="utf-8"
    )

    assert "Smoke test Unraid UID/GID remapping" in workflow
    assert "--env PUID=99" in workflow
    assert "--env PGID=100" in workflow
    assert "--security-opt no-new-privileges" in workflow
    assert "--read-only" in workflow
    assert "(os.getuid(), os.getgid()) == (99, 100)" in workflow
    assert 'reports = Path("/config/reports")' in workflow
    assert "reports.is_dir()" in workflow
    assert "(status.st_uid, status.st_gid) == (99, 100)" in workflow


def test_ci_publishes_versioned_and_immutable_signed_images():
    workflow = (REPO_ROOT / ".github/workflows/docker-latest.yml").read_text(
        encoding="utf-8"
    )

    assert "tags: [ 'v*.*.*' ]" in workflow
    assert "type=semver,pattern={{version}}" in workflow
    assert "type=semver,pattern={{major}}.{{minor}}" in workflow
    assert "type=semver,pattern={{major}}" in workflow
    assert "type=sha,prefix=sha-,format=long" in workflow
    assert (
        "type=raw,value=latest,enable=${{ startsWith(github.ref, "
        "'refs/tags/v') && !contains(github.ref_name, '-rc.') }}"
    ) in workflow
    assert "enable={{is_default_branch}}" not in workflow
    assert "cosign sign --yes" in workflow
    assert "org.opencontainers.image.licenses=LicenseRef-All-Rights-Reserved" in workflow


def test_release_tags_require_the_exact_current_main_commit():
    workflow = (REPO_ROOT / ".github/workflows/docker-latest.yml").read_text(
        encoding="utf-8"
    )

    assert "main_commit=\"$(git rev-parse 'origin/main^{commit}')\"" in workflow
    assert '[[ "${tag_commit}" != "${main_commit}" ]]' in workflow
    assert "Release tags must reference the exact current main commit." in workflow
    assert "git merge-base --is-ancestor" not in workflow


def test_registry_publication_is_serialized_and_verified():
    workflow = (REPO_ROOT / ".github/workflows/docker-latest.yml").read_text(
        encoding="utf-8"
    )

    assert "'metafusion-registry-publish'" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "Verify published aliases, signature, SBOM, and provenance" in workflow
    assert "resolved_digest" in workflow
    assert "cosign verify" in workflow
    assert "--certificate-identity" in workflow
    assert "{{json .SBOM}}" in workflow
    assert "{{json .Provenance}}" in workflow


def test_pull_requests_build_both_architectures_without_publishing():
    workflow = (REPO_ROOT / ".github/workflows/docker-latest.yml").read_text(
        encoding="utf-8"
    )

    assert "github.event_name == 'pull_request' ||" in workflow
    assert "platforms: linux/amd64,linux/arm64" in workflow
    assert "push: ${{ github.event_name != 'pull_request' }}" in workflow
    assert workflow.count("github.event_name != 'pull_request'") >= 6


def test_ci_smoke_tests_each_published_platform_in_isolation():
    workflow = (REPO_ROOT / ".github/workflows/docker-latest.yml").read_text(
        encoding="utf-8"
    )

    amd64 = 'docker run --rm --platform linux/amd64 "${image}" --version'
    reset = 'docker image rm --force "${image}"'
    arm64 = 'docker run --rm --platform linux/arm64 "${image}" --version'
    assert workflow.index(amd64) < workflow.index(reset) < workflow.index(arm64)


def test_deployment_docs_document_exact_image_pinning_and_rollback():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    deployment_docs = "\n".join(
        (
            readme,
            (REPO_ROOT / "docs" / "docker-compose.md").read_text(encoding="utf-8"),
            (REPO_ROOT / "docs" / "unraid.md").read_text(encoding="utf-8"),
            (REPO_ROOT / "docs" / "release-testing.md").read_text(encoding="utf-8"),
            (REPO_ROOT / "SUPPORT.md").read_text(encoding="utf-8"),
        )
    )

    assert "docs/release-testing.md" in readme
    assert "## CI and image lanes" in deployment_docs
    assert "## Update or roll back" in deployment_docs
    assert "ghcr.io/ray-cys/metafusion:1.2.3" in deployment_docs
    assert "METAFUSION_IMAGE" in deployment_docs
    assert "sha-<full-commit>" in deployment_docs


def test_readme_remains_a_concise_documentation_landing_page():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    required_links = {
        "docs/index.md",
        "docs/modes.md",
        "docs/docker-compose.md",
        "docs/unraid.md",
        "docs/configuration.md",
        "docs/policies.md",
        "docs/operations.md",
        "docs/diagnostics.md",
        "docs/release-testing.md",
        "SUPPORT.md",
    }

    assert len(readme.splitlines()) <= 150
    assert len(readme.split()) <= 900
    assert sorted(link for link in required_links if link not in readme) == []
    assert "## Scheduling and incremental processing" not in readme
    assert "## Output and persistent state" not in readme
    assert "## Docker image tags and rollback" not in readme

    index = (REPO_ROOT / "docs" / "index.md").read_text(encoding="utf-8")
    for guide in (
        "modes.md",
        "configuration.md",
        "policies.md",
        "artwork-providers.md",
        "operations.md",
        "diagnostics.md",
        "release-testing.md",
    ):
        assert guide in index

    for document in (
        REPO_ROOT / "README.md",
        REPO_ROOT / "docs" / "index.md",
        REPO_ROOT / "docs" / "modes.md",
        REPO_ROOT / "docs" / "configuration.md",
        REPO_ROOT / "docs" / "docker-compose.md",
        REPO_ROOT / "docs" / "unraid.md",
    ):
        text = document.read_text(encoding="utf-8")
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
            path = target.split("#", 1)[0]
            if not path or "://" in path:
                continue
            assert (document.parent / path).resolve().exists(), (
                f"Broken local documentation link in {document}: {target}"
            )


def test_docker_build_embeds_version_and_commit():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/docker-latest.yml").read_text(
        encoding="utf-8"
    )

    assert "ARG METAFUSION_VERSION" in dockerfile
    assert "ARG METAFUSION_COMMIT" in dockerfile
    assert "org.opencontainers.image.revision" in dockerfile
    assert "METAFUSION_VERSION=${{ github.ref_name }}" in workflow
    assert "METAFUSION_COMMIT=${{ github.sha }}" in workflow


def test_nightly_reliability_qualifies_main_and_develop_without_publishing():
    workflow = (REPO_ROOT / ".github/workflows/nightly-reliability.yml").read_text(
        encoding="utf-8"
    )

    assert 'cron: "17 18 * * *"' in workflow
    assert "branch: [main, develop]" in workflow
    assert 'python-version: ["3.10", "3.13"]' in workflow
    assert "ref: ${{ matrix.branch }}" in workflow
    assert "--cov-fail-under=85" in workflow
    assert "--cov-fail-under=100" in workflow
    assert "provider_compatibility.py verify" in workflow
    assert "performance_regression.py" in workflow
    assert "pip-audit" in workflow
    assert "docker build" in workflow
    assert "--severity CRITICAL" in workflow
    assert "contents: read" in workflow
    assert "packages: write" not in workflow
    assert "docker push" not in workflow


def test_unraid_template_exposes_only_schema_declared_essential_variables():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    compose_variables = set(compose["services"]["metafusion"]["environment"])
    schema = yaml.safe_load((REPO_ROOT / "config_schema.yml").read_text(encoding="utf-8"))
    expected = set(schema["unraid_variables"])
    root = ElementTree.parse(REPO_ROOT / "unraid" / "metafusion.xml").getroot()
    variables = {
        config.attrib["Target"]
        for config in root.findall("Config")
        if config.attrib.get("Type") == "Variable"
    }

    assert variables == expected
    assert variables < compose_variables
    assert len(variables) == len(
        [config for config in root.findall("Config") if config.attrib.get("Type") == "Variable"]
    )


def test_unraid_template_requires_only_core_connection_library_and_mode_variables():
    root = ElementTree.parse(REPO_ROOT / "unraid" / "metafusion.xml").getroot()
    configs = {
        config.attrib.get("Target"): config
        for config in root.findall("Config")
        if config.attrib.get("Type") == "Variable"
    }
    required = {
        target
        for target, config in configs.items()
        if config.attrib.get("Required") == "true"
    }

    assert required == {
        "RUN_MODE",
        "PLEX_URL",
        "PLEX_TOKEN",
        "PLEX_LIBRARIES",
        "TMDB_API_KEY",
    }
    assert configs["PLEX_TOKEN"].attrib["Mask"] == "true"
    assert configs["TMDB_API_KEY"].attrib["Mask"] == "true"


def test_unraid_template_preserves_hardened_runtime_and_unraid_identity():
    root = ElementTree.parse(REPO_ROOT / "unraid" / "metafusion.xml").getroot()
    configs = {config.attrib.get("Target"): config for config in root.findall("Config")}
    extra_params = root.findtext("ExtraParams", default="")

    assert root.findtext("Repository") == "ghcr.io/ray-cys/metafusion:latest"
    assert configs["/config"].attrib["Required"] == "true"
    assert configs["/config"].attrib["Mode"] == "rw"
    assert configs["PUID"].text == "99"
    assert configs["PGID"].text == "100"
    assert "STATUS_FILE" not in configs
    assert "--read-only" in extra_params
    assert "--cap-drop" not in extra_params
    assert "--security-opt=no-new-privileges" in extra_params
    assert "--stop-timeout=20" in extra_params
