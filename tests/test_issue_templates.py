from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
FORMS_DIR = REPO_ROOT / ".github" / "ISSUE_TEMPLATE"
EXPECTED_FORMS = {
    "artwork-identity.yml",
    "bug.yml",
    "docker-unraid.yml",
    "feature-request.yml",
    "kometa-output.yml",
    "plex_metadata.yml",
    "runtime-cleanup.yml",
}
KNOWN_LABELS = {
    "artwork",
    "bug",
    "docker",
    "enhancement",
    "kometa",
    "plex-metadata",
}
COMPONENT_TYPES = {"markdown", "input", "textarea", "dropdown", "checkboxes"}


def _load_yaml(path: Path) -> dict[str, object]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), f"{path.name} must contain a YAML mapping"
    return payload


def test_public_issue_forms_are_complete_and_phase_free() -> None:
    form_paths = sorted(
        path for path in FORMS_DIR.glob("*.yml") if path.name != "config.yml"
    )
    assert {path.name for path in form_paths} == EXPECTED_FORMS

    for path in form_paths:
        payload = _load_yaml(path)
        assert payload.get("name")
        assert payload.get("description")
        assert payload.get("title")

        labels = payload.get("labels")
        assert isinstance(labels, list) and labels
        assert set(labels) <= KNOWN_LABELS

        body = payload.get("body")
        assert isinstance(body, list) and body
        component_ids: set[str] = set()
        has_required_confirmation = False

        for component in body:
            assert isinstance(component, dict)
            component_type = component.get("type")
            assert component_type in COMPONENT_TYPES
            if component_type == "markdown":
                continue

            component_id = component.get("id")
            assert isinstance(component_id, str)
            assert re.fullmatch(r"[A-Za-z0-9_-]+", component_id)
            assert component_id not in component_ids
            component_ids.add(component_id)

            attributes = component.get("attributes")
            assert isinstance(attributes, dict)
            assert attributes.get("label")

            if component_type == "checkboxes":
                options = attributes.get("options")
                assert isinstance(options, list) and options
                has_required_confirmation |= any(
                    isinstance(option, dict) and option.get("required") is True
                    for option in options
                )

        assert has_required_confirmation
        if path.name != "feature-request.yml":
            assert "version" in component_ids
        assert re.search(r"\bphase\b", path.read_text(encoding="utf-8"), re.I) is None


def test_issue_chooser_disables_blank_reports_and_links_to_help() -> None:
    chooser = _load_yaml(FORMS_DIR / "config.yml")
    assert chooser.get("blank_issues_enabled") is False

    contact_links = chooser.get("contact_links")
    assert isinstance(contact_links, list) and len(contact_links) >= 2
    for link in contact_links:
        assert isinstance(link, dict)
        assert link.get("name")
        assert link.get("about")
        assert str(link.get("url", "")).startswith("https://")
