#!/usr/bin/env python3
"""Generate every public configuration surface from config_schema.yml."""

import argparse
import copy
import io
import sys
from pathlib import Path
from xml.etree import ElementTree

import yaml

ROOT = Path(__file__).parents[1]
SCHEMA_PATH = ROOT / "config_schema.yml"


def load_schema():
    document = yaml.safe_load(SCHEMA_PATH.read_text(encoding="utf-8"))
    if document.get("schema_version") != 1:
        raise ValueError("unsupported configuration schema version")
    settings = document.get("settings") or []
    names = [setting.get("name") for setting in settings]
    if not names or None in names or len(names) != len(set(names)):
        raise ValueError("configuration setting names must be present and unique")
    unraid_variables = document.get("unraid_variables") or []
    if (
        len(unraid_variables) != len(set(unraid_variables))
        or set(unraid_variables) - set(names)
        or any(
            "unraid" not in setting
            for setting in settings
            if setting.get("name") in unraid_variables
        )
    ):
        raise ValueError("Unraid variables must be unique settings with UI metadata")
    return document


def _ordered(settings, key):
    return sorted(
        (setting for setting in settings if key in setting),
        key=lambda setting: int(setting[key]),
    )


def render_env_example(schema):
    lines = [
        "# Generated from config_schema.yml; run tools/generate_config_surfaces.py to update.",
    ]
    previous_section = None
    for setting in _ordered(schema["settings"], "env_order"):
        section = setting["section"]
        if section != previous_section:
            lines.extend(("", f"# {section}"))
            previous_section = section
        lines.append(f"{setting['name']}={setting.get('env_example', '')}")
    return "\n".join(lines) + "\n"


def _render_yaml_configuration(schema, *, mode=None):
    defaults = copy.deepcopy(schema["defaults"])
    if mode is not None:
        defaults["settings"]["mode"] = mode
    defaults["plex"]["token"] = "YOUR_PLEX_TOKEN"
    defaults["tmdb"]["api_key"] = "YOUR_TMDB_API_KEY"
    return yaml.safe_dump(
        defaults,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )


def render_config_template(schema):
    return (
        "# Generated from config_schema.yml. Copy this file to config.yml before editing.\n"
        "# Environment variables override non-empty values in config.yml.\n\n"
        + _render_yaml_configuration(schema)
    )


def render_mode_template(schema, mode):
    return (
        f"# Complete {mode.title()}-mode configuration generated from config_schema.yml.\n"
        f"# Copy this file to /config/{mode}.yml and edit the copy when needed.\n"
        "# Non-empty environment variables override matching YAML values.\n\n"
        + _render_yaml_configuration(schema, mode=mode)
    )


def render_compose(schema, existing):
    start = existing.index("    environment:\n")
    end = existing.index("    volumes:\n", start)
    lines = ["    environment:"]
    for setting in _ordered(schema["settings"], "compose_order"):
        value = setting["compose"]
        lines.append(f"      {setting['name']}: {value}")
    generated = "\n".join(lines) + "\n"
    return existing[:start] + generated + existing[end:]


def render_unraid(schema, existing):
    parser = ElementTree.XMLParser(target=ElementTree.TreeBuilder(insert_comments=True))
    root = ElementTree.fromstring(existing, parser=parser)
    for element in list(root):
        if element.tag == "Config" and element.attrib.get("Type") == "Variable":
            root.remove(element)
        elif element.tag is ElementTree.Comment:
            comment = (element.text or "").strip()
            if comment != "Required paths and output paths":
                root.remove(element)
    root.append(ElementTree.Comment(" Generated variables from config_schema.yml "))
    exposed = set(schema["unraid_variables"])
    unraid_settings = sorted(
        (
            setting
            for setting in schema["settings"]
            if setting.get("name") in exposed
        ),
        key=lambda setting: int(setting["unraid"]["order"]),
    )
    for setting in unraid_settings:
        ui = setting["unraid"]
        element = ElementTree.SubElement(
            root,
            "Config",
            {
                "Name": str(ui["name"]),
                "Target": setting["name"],
                "Default": str(ui.get("default", "")),
                "Mode": "",
                "Description": str(setting["description"]),
                "Type": "Variable",
                "Display": str(ui.get("display", "advanced")),
                "Required": str(bool(ui.get("required"))).lower(),
                "Mask": str(bool(ui.get("mask"))).lower(),
            },
        )
        value = str(ui.get("value", ""))
        if value:
            element.text = value
    ElementTree.indent(root, space="  ")
    buffer = io.BytesIO()
    ElementTree.ElementTree(root).write(
        buffer,
        encoding="UTF-8",
        xml_declaration=True,
        short_empty_elements=True,
    )
    return buffer.getvalue().decode("UTF-8") + "\n"


def render_reference(schema):
    lines = [
        "# Generated configuration surface",
        "",
        "This exhaustive table is generated from `config_schema.yml`. Edit the schema and",
        "run `python tools/generate_config_surfaces.py`; do not edit this file directly.",
    ]
    sections = []
    unraid_variables = set(schema["unraid_variables"])
    for setting in schema["settings"]:
        if setting["section"] not in sections:
            sections.append(setting["section"])
    for section in sections:
        lines.extend(
            (
                "",
                f"## {section}",
                "",
                "| Variable | Default/example | Purpose | Surfaces |",
                "| --- | --- | --- | --- |",
            )
        )
        settings = [item for item in schema["settings"] if item["section"] == section]
        settings.sort(key=lambda item: min(item.get("env_order", 10000), item.get("compose_order", 10000)))
        for setting in settings:
            default = setting.get("env_example")
            if default is None and "unraid" in setting:
                default = setting["unraid"].get("value", "")
            default = "unset" if default in (None, "") else str(default)
            surfaces = []
            if "path" in setting or "secret_file" in setting:
                surfaces.append("application")
            if "env_order" in setting:
                surfaces.append("`.env` example")
            if "compose_order" in setting:
                surfaces.append("Compose")
            if setting["name"] in unraid_variables:
                surfaces.append("Unraid")
            description = str(setting["description"]).replace("|", "\\|")
            lines.append(
                f"| `{setting['name']}` | `{default}` | {description} | {', '.join(surfaces)} |"
            )
    return "\n".join(lines) + "\n"


def generated_files(schema):
    compose_path = ROOT / "docker-compose.yml"
    unraid_path = ROOT / "unraid" / "metafusion.xml"
    return {
        ROOT / ".env.example": render_env_example(schema),
        ROOT / "config" / "config_template.yml": render_config_template(schema),
        ROOT / "config" / "examples" / "kometa.yml": render_mode_template(
            schema, "kometa"
        ),
        ROOT / "config" / "examples" / "plex.yml": render_mode_template(
            schema, "plex"
        ),
        compose_path: render_compose(schema, compose_path.read_text(encoding="utf-8")),
        unraid_path: render_unraid(schema, unraid_path.read_text(encoding="utf-8")),
        ROOT / "docs" / "configuration.generated.md": render_reference(schema),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when a generated configuration surface is stale",
    )
    args = parser.parse_args(argv)
    schema = load_schema()
    stale = []
    for path, content in generated_files(schema).items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == content:
            continue
        stale.append(path.relative_to(ROOT))
        if not args.check:
            path.write_text(content, encoding="utf-8")
    if stale and args.check:
        print("Stale generated configuration surfaces:", file=sys.stderr)
        for path in stale:
            print(f"- {path}", file=sys.stderr)
        return 1
    if stale:
        print("Updated: " + ", ".join(str(path) for path in stale))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
