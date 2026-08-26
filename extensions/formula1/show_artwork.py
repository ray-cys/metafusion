"""Race-triggered, paired Formula 1 show poster/background rotation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from extensions.formula1.artwork import (
    _fit,
    _font,
    branding_fingerprint,
    branding_paths,
    fitted_font,
    svg_path_points,
)
from extensions.formula1.commons import (
    CommonsCandidate,
    acquire_candidate_image,
    load_constructors,
    search_commons,
)
from helper.io import atomic_replace_file, atomic_write_json, atomic_write_text

FILE_MODE = 0o664
SHOW_RENDERER_VERSION = 3
EPISODE_RENDERER_VERSION = 1

SESSION_DATE_FIELDS = {
    "warmup": "FirstPractice",
    "practice1": "FirstPractice",
    "practice2": "SecondPractice",
    "practice3": "ThirdPractice",
    "sprint_qualifying": "SprintQualifying",
    "pre_sprint": "Sprint",
    "sprint": "Sprint",
    "post_sprint": "Sprint",
    "pre_qualifying": "Qualifying",
    "qualifying": "Qualifying",
    "post_qualifying": "Qualifying",
}


@dataclass(frozen=True)
class ShowArtworkResult:
    action: str
    trigger_round: int
    poster_reference: str | None = None
    background_reference: str | None = None
    constructor: str | None = None
    issue: str | None = None
    pairs_pruned: int = 0
    cache_pruned: int = 0
    episode_references: dict[int, str] = field(default_factory=dict)
    episode_actions: dict[int, str] = field(default_factory=dict)


def _checksum(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _slug(value):
    return re.sub(r"[^a-z0-9]+", "-", str(value).casefold()).strip("-") or "team"


def _asset_reference(config, relative):
    root = str(
        config["show_artwork"].get(
            "asset_reference_root", "config/assets/formula1/shows"
        )
    )
    return f"/{root.strip('/')}/{str(relative).lstrip('/')}"


def _branding_paths(config):
    paths = branding_paths(config)
    return paths["logo"], paths["font_regular"], paths["font_bold"]


def _show_render_fingerprint(config):
    payload = {
        "renderer": SHOW_RENDERER_VERSION,
        "branding": branding_fingerprint(config),
        "poster": [
            config["show_artwork"]["poster_width"],
            config["show_artwork"]["poster_height"],
        ],
        "background": [
            config["show_artwork"]["background_width"],
            config["show_artwork"]["background_height"],
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _place_logo(image, logo_path, box, fallback_font):
    left, top, right, bottom = map(int, box)
    if logo_path.is_file():
        with Image.open(logo_path) as source:
            logo = source.convert("RGBA")
            logo.thumbnail((right - left, bottom - top), Image.Resampling.LANCZOS)
            image.paste(logo, (left, top), logo)
    else:
        ImageDraw.Draw(image).text(
            (left, top), "FORMULA 1", font=fallback_font, fill=(245, 245, 245, 255)
        )


def _technical_frame(image, *, dense=False):
    draw = ImageDraw.Draw(image, "RGBA")
    width, height = image.size
    if dense:
        spacing = max(50, width // 22)
        for offset in range(-height, width, spacing):
            draw.line(
                (offset, 0, offset + height, height),
                fill=(235, 20, 40, 24),
                width=max(1, width // 900),
            )
    else:
        accent = (235, 20, 40, 120)
        line_width = max(2, width // 800)
        draw.line(
            (width * 0.72, height * 0.06, width * 0.95, height * 0.06),
            fill=accent,
            width=line_width,
        )
        draw.line(
            (width * 0.04, height * 0.72, width * 0.22, height * 0.72),
            fill=accent,
            width=line_width,
        )
    draw.line((0, 3, width, 3), fill=(235, 20, 40, 210), width=max(3, width // 450))
    draw.line(
        (0, height - 4, width, height - 4),
        fill=(235, 20, 40, 210),
        width=max(3, width // 450),
    )


def _draw_circuit(draw, path_data, box, width):
    points = _fit(svg_path_points(path_data), box)
    if not points:
        return
    draw.line(points, fill=(0, 0, 0, 180), width=max(12, width // 65), joint="curve")
    draw.line(points, fill=(175, 175, 180, 150), width=max(4, width // 220), joint="curve")


def _photo_band(photo, size):
    contained = ImageOps.contain(photo.convert("RGB"), size, Image.Resampling.LANCZOS)
    layer = Image.new("RGB", size, (8, 9, 12))
    layer.paste(contained, ((size[0] - contained.width) // 2, (size[1] - contained.height) // 2))
    return layer


def _grade_photo(photo, size, *, contain=False, centering=(0.5, 0.5), strong=False):
    """Contain bright trackside scenery without flattening the car or team colours."""
    if contain:
        image = _photo_band(photo, size)
    else:
        image = ImageOps.fit(
            photo.convert("RGB"), size, Image.Resampling.LANCZOS, centering=centering
        )
    image = ImageEnhance.Color(image).enhance(0.90)
    image = ImageEnhance.Contrast(image).enhance(1.03)
    shoulder = 0.54
    strength = 0.58 if strong else 0.66
    exposure = 0.84 if strong else 0.88
    lookup = []
    for value in range(256):
        normalized = value / 255
        compressed = (
            normalized
            if normalized <= shoulder
            else shoulder + (normalized - shoulder) * strength
        )
        lookup.append(round(min(1.0, compressed * exposure) * 255))
    image = image.point(lookup * 3)

    width, height = size
    top_alpha = 62 if strong else 50
    bottom_alpha = 20 if strong else 16
    shade = Image.new("L", (1, height))
    shade.putdata(
        [
            round(top_alpha + (bottom_alpha - top_alpha) * y / max(1, height - 1))
            for y in range(height)
        ]
    )
    black = Image.new("RGB", size, (3, 5, 9))
    image = Image.composite(black, image, shade.resize(size))

    vignette = Image.new("L", size, 0)
    vignette_draw = ImageDraw.Draw(vignette)
    border = max(12, round(min(size) * 0.08))
    vignette_draw.rectangle((0, 0, width - 1, height - 1), outline=38, width=border)
    vignette = vignette.filter(ImageFilter.GaussianBlur(border * 1.4))
    return Image.composite(black, image, vignette)


def _episode_destination(config, episode):
    return (
        config["paths"]["assets"]
        / str(episode.year)
        / f"round-{episode.round_number:02d}"
        / "episodes"
        / f"episode-{episode.episode_number:02d}.png"
    )


def _episode_reference(config, episode):
    root = str(
        config["artwork"].get(
            "asset_reference_root", "config/assets/formula1/rounds"
        )
    )
    return (
        f"/{root.strip('/')}/{episode.year}/round-{episode.round_number:02d}/"
        f"episodes/episode-{episode.episode_number:02d}.png"
    )


def _episode_fingerprint(episode, race, path_data, source_identity, config):
    payload = {
        "renderer": EPISODE_RENDERER_VERSION,
        "branding": branding_fingerprint(config),
        "source": source_identity,
        "episode": [
            episode.year,
            episode.round_number,
            episode.episode_number,
            episode.program_title,
            episode.program_kind,
        ],
        "race": [race.name, race.circuit, race.locality, race.country, race.race_date],
        "path": path_data or "",
        "size": [
            config["show_artwork"]["background_width"],
            config["show_artwork"]["background_height"],
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _managed_episode_action(state, episode, destination, fingerprint):
    previous = state.artwork(episode.logical_key)
    if not destination.exists():
        return "create"
    checksum = _checksum(destination)
    if previous is None:
        return "preserve-manual"
    if checksum != previous["checksum"]:
        return "preserve-manual"
    if previous["fingerprint"] == fingerprint:
        return "unchanged"
    return "update"


def render_episode_poster(episode, race, path_data, photo_path, config, destination):
    """Render an immutable 16:9 race/session card for Kometa episode metadata."""
    width = config["show_artwork"]["background_width"]
    height = config["show_artwork"]["background_height"]
    with Image.open(photo_path) as source:
        image = _grade_photo(
            source, (width, height), centering=(0.62, 0.5), strong=True
        ).convert("RGBA")

    left_mask = Image.new("L", (width, 1))
    left_mask.putdata(
        [
            round(238 * max(0.0, 1 - x / (width * 0.70)) ** 1.45)
            for x in range(width)
        ]
    )
    black = Image.new("RGBA", image.size, (3, 4, 7, 255))
    clear = Image.new("RGBA", image.size, (0, 0, 0, 0))
    overlay = Image.composite(black, clear, left_mask.resize(image.size))
    image = Image.alpha_composite(image, overlay)
    _technical_frame(image)

    logo_path, regular_path, bold_path = _branding_paths(config)
    round_font = _font(bold_path, max(24, width // 45))
    session_font = fitted_font(
        bold_path,
        episode.program_title.upper(),
        max(42, width // 18),
        28,
        width * 0.56,
    )
    grand_prix = race.name.upper()
    race_font = fitted_font(
        bold_path, grand_prix, max(28, width // 29), 22, width * 0.56
    )
    detail = f"{race.circuit}  •  {race.locality}, {race.country}"
    detail_font = fitted_font(
        regular_path, detail, max(22, width // 42), 18, width * 0.58
    )
    _place_logo(
        image,
        logo_path,
        (width * 0.045, height * 0.07, width * 0.23, height * 0.21),
        round_font,
    )
    draw = ImageDraw.Draw(image, "RGBA")
    _draw_circuit(
        draw,
        path_data,
        (width * 0.68, height * 0.08, width * 0.94, height * 0.42),
        width,
    )
    draw.text(
        (width * 0.045, height * 0.35),
        f"ROUND {race.round_number:02d}  •  {race.year}",
        font=round_font,
        fill=(235, 30, 48, 245),
    )
    draw.text(
        (width * 0.045, height * 0.45),
        grand_prix,
        font=race_font,
        fill=(245, 245, 247, 235),
    )
    draw.text(
        (width * 0.045, height * 0.57),
        episode.program_title.upper(),
        font=session_font,
        fill=(255, 255, 255, 255),
    )
    draw.text(
        (width * 0.045, height * 0.73),
        detail,
        font=detail_font,
        fill=(225, 226, 230, 225),
    )
    session_field = SESSION_DATE_FIELDS.get(episode.program_kind)
    date = race.session_dates.get(session_field) if session_field else None
    date = date or race.race_date
    if date:
        draw.text(
            (width * 0.045, height * 0.82),
            str(date),
            font=round_font,
            fill=(225, 226, 230, 190),
        )
    return _atomic_save(image.convert("RGB"), destination)


def _atomic_save(image, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(dir=destination.parent, suffix=".png")
        os.close(descriptor)
        temporary = Path(name)
        image.save(temporary, format="PNG", optimize=True)
        checksum = _checksum(temporary)
        atomic_replace_file(temporary, destination, new_file_mode=FILE_MODE)
        temporary = None
        return checksum
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def render_show_poster(show, race, path_data, photo_path, config, destination):
    """Render the stable portrait frame around an undistorted landscape car photograph."""
    width = config["show_artwork"]["poster_width"]
    height = config["show_artwork"]["poster_height"]
    image = Image.new("RGB", (width, height), (6, 7, 10))
    _technical_frame(image, dense=True)
    draw = ImageDraw.Draw(image, "RGBA")
    draw.polygon(
        [(0, height), (width, height * 0.67), (width, height), (0, height)],
        fill=(150, 0, 20, 185),
    )
    logo_path, regular_path, bold_path = _branding_paths(config)
    detail = f"{race.circuit}  •  {race.locality}, {race.country}"
    regular = fitted_font(regular_path, detail, max(26, width // 28), 18, width * 0.88)
    bold = fitted_font(
        bold_path, f"{show.year} SEASON", max(42, width // 15), 28, width * 0.88
    )
    small_bold = _font(bold_path, max(24, width // 30))
    _place_logo(
        image,
        logo_path,
        (width * 0.06, height * 0.055, width * 0.38, height * 0.15),
        small_bold,
    )
    draw.text(
        (width * 0.06, height * 0.22),
        f"{show.year} SEASON",
        font=bold,
        fill=(245, 245, 245, 255),
    )
    draw.text(
        (width * 0.06, height * 0.29),
        f"CURRENT RACE  •  ROUND {race.round_number:02d}",
        font=small_bold,
        fill=(235, 30, 48, 255),
    )
    _draw_circuit(
        draw,
        path_data,
        (width * 0.61, height * 0.07, width * 0.93, height * 0.34),
        width,
    )
    band_height = round(width * 9 / 16)
    band_top = round(height * 0.39)
    with Image.open(photo_path) as source:
        band = _grade_photo(source, (width, band_height), contain=True)
    image.paste(band, (0, band_top))
    draw = ImageDraw.Draw(image, "RGBA")
    draw.rectangle(
        (0, band_top, width, band_top + band_height), outline=(235, 20, 40, 210), width=4
    )
    draw.text(
        (width * 0.06, height * 0.82),
        race.name.upper(),
        font=small_bold,
        fill=(255, 255, 255, 245),
    )
    draw.text(
        (width * 0.06, height * 0.87),
        detail,
        font=regular,
        fill=(235, 235, 238, 225),
    )
    draw.text(
        (width * 0.06, height * 0.94),
        "RACE-WEEK ROTATION",
        font=small_bold,
        fill=(255, 255, 255, 185),
    )
    return _atomic_save(image, destination)


def render_show_background(show, race, photo_path, config, destination):
    """Render the paired 16:9 background with a Plex-safe title area."""
    width = config["show_artwork"]["background_width"]
    height = config["show_artwork"]["background_height"]
    with Image.open(photo_path) as source:
        source_rgb = source.convert("RGB")
        image = ImageOps.fit(source_rgb, (width, height), Image.Resampling.LANCZOS)
        image = image.filter(ImageFilter.GaussianBlur(max(8, width // 100)))
        image = ImageEnhance.Brightness(image).enhance(0.42)
        complete_photo = ImageOps.contain(
            source_rgb,
            (round(width * 0.82), round(height * 0.88)),
            Image.Resampling.LANCZOS,
        )
        complete_photo = _grade_photo(
            complete_photo,
            complete_photo.size,
            contain=True,
        )
        image.paste(
            complete_photo,
            (width - complete_photo.width, (height - complete_photo.height) // 2),
        )
    image = ImageEnhance.Contrast(image).enhance(1.05)
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gradient = Image.new("L", (width, 1))
    gradient.putdata(
        [
            round(245 * max(0.0, 1 - x / (width * 0.68)) ** 1.6)
            for x in range(width)
        ]
    )
    black = Image.new("RGBA", (width, height), (3, 4, 7, 255))
    overlay = Image.composite(black, overlay, gradient.resize((width, height)))
    image = Image.alpha_composite(image.convert("RGBA"), overlay)
    _technical_frame(image)
    logo_path, _regular_path, bold_path = _branding_paths(config)
    season_font = _font(bold_path, max(34, width // 32))
    background_title = f"ROUND {race.round_number:02d}  •  {race.name.upper()}"
    detail_font = fitted_font(
        bold_path, background_title, max(22, width // 55), 18, width * 0.58
    )
    _place_logo(
        image,
        logo_path,
        (width * 0.045, height * 0.075, width * 0.25, height * 0.21),
        season_font,
    )
    draw = ImageDraw.Draw(image, "RGBA")
    draw.text(
        (width * 0.045, height * 0.77),
        f"{show.year} SEASON",
        font=season_font,
        fill=(235, 30, 48, 245),
    )
    draw.text(
        (width * 0.045, height * 0.86),
        background_title,
        font=detail_font,
        fill=(240, 240, 242, 220),
    )
    return _atomic_save(image.convert("RGB"), destination)


def _current_references(current):
    source = current.get("source") or {}
    return source.get("poster_reference"), source.get("background_reference")


def _existing_episode_outputs(state, config, show, race):
    references = {}
    actions = {}
    for episode in show.episodes:
        if episode.round_number != race.round_number:
            continue
        destination = _episode_destination(config, episode)
        previous = state.artwork(episode.logical_key)
        if not destination.is_file():
            continue
        if previous and _checksum(destination) != previous["checksum"]:
            actions[episode.episode_number] = "preserve-manual"
        else:
            actions[episode.episode_number] = (
                "unchanged" if previous else "preserve-manual"
            )
        references[episode.episode_number] = _episode_reference(config, episode)
    return references, actions


def _reconcile_episode_posters(
    state, config, show, race, path_data, photo_path, source_identity
):
    references = {}
    actions = {}
    for episode in show.episodes:
        if episode.round_number != race.round_number:
            continue
        destination = _episode_destination(config, episode)
        fingerprint = _episode_fingerprint(
            episode, race, path_data, source_identity, config
        )
        previous = state.artwork(episode.logical_key)
        if path_data is None and previous is not None:
            fingerprint = previous["fingerprint"]
        action = _managed_episode_action(state, episode, destination, fingerprint)
        actions[episode.episode_number] = action
        if action in {"create", "update"} and not config["dry_run"]:
            checksum = render_episode_poster(
                episode, race, path_data, photo_path, config, destination
            )
            state.save_artwork(episode.logical_key, destination, fingerprint, checksum)
        if destination.exists() or action in {"create", "update"}:
            references[episode.episode_number] = _episode_reference(config, episode)
    return references, actions


def _pair_integrity(current):
    poster = Path(current["poster_destination"])
    background = Path(current["background_destination"])
    if not poster.exists() or not background.exists():
        return "missing"
    if (
        _checksum(poster) != current["poster_checksum"]
        or _checksum(background) != current["background_checksum"]
    ):
        return "manual"
    return "managed"


def _prune_retained_pairs(state, config, logical_key, current):
    limit = config["show_artwork"]["retention_pairs_per_season"]
    history = [
        item for item in state.show_rotation_history() if item["logical_key"] == logical_key
    ]
    removable = history[:-limit] if len(history) > limit else []
    current_paths = {
        str(current["poster_destination"]),
        str(current["background_destination"]),
    }
    removed = 0
    for item in removable:
        source = item.get("source") or {}
        checksums = source.get("generated_checksums") or {}
        paths = [Path(item["poster_destination"]), Path(item["background_destination"])]
        if any(str(path) in current_paths for path in paths):
            continue
        expected = [checksums.get("poster"), checksums.get("background")]
        if not all(expected):
            continue
        safe = all(
            (not path.exists())
            or (path.is_file() and _checksum(path) == expected[index])
            for index, path in enumerate(paths)
        )
        if not safe:
            continue
        for path in paths:
            path.unlink(missing_ok=True)
        try:
            paths[0].parent.rmdir()
        except OSError:
            pass
        state.remove_show_rotation_history(item["id"])
        removed += 1
    return removed


def _prune_source_cache(config, active_photo):
    cutoff = time.time() - config["show_artwork"]["source_cache_retention_days"] * 86400
    active = Path(active_photo).resolve()
    removed = 0
    root = config["paths"]["show_image_cache"]
    for path in root.glob("*") if root.exists() else ():
        if (
            path.is_file()
            and path.suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp"}
            and path.resolve() != active
            and path.stat().st_mtime < cutoff
        ):
            path.unlink()
            removed += 1
    return removed


def _candidate_order(roster, current, trigger_round):
    if not roster:
        return []
    ids = [constructor.constructor_id for constructor in roster]
    previous = current.get("constructor_id") if current else None
    if previous in ids:
        start = (ids.index(previous) + 1) % len(ids)
    else:
        start = (int(trigger_round) - 1) % len(ids)
    return [roster[(start + offset) % len(roster)] for offset in range(len(roster))]


def _attribution_reports(config, history):
    if config["dry_run"]:
        return
    records = []
    lines = ["MetaFusion Formula 1 show artwork attribution", ""]
    for item in history:
        source = item["source"]
        candidate = source["candidate"]
        record = {
            "season_year": item["season_year"],
            "trigger_round": item["trigger_round"],
            "constructor_id": item["constructor_id"],
            "constructor_name": candidate["constructor_name"],
            "source_title": candidate["title"],
            "source_page": candidate["page_url"],
            "author": candidate["author"],
            "licence": candidate["licence"],
            "licence_url": candidate["licence_url"],
            "poster": item["poster_destination"],
            "background": item["background_destination"],
        }
        records.append(record)
        lines.extend(
            [
                f"Season {record['season_year']} • Round {record['trigger_round']:02d}",
                f"Team: {record['constructor_name']}",
                f"Source: {record['source_title']}",
                f"Page: {record['source_page']}",
                f"Author: {record['author']}",
                f"Licence: {record['licence']}",
                f"Licence URL: {record['licence_url']}",
                f"Poster: {record['poster']}",
                f"Background: {record['background']}",
                "",
            ]
        )
    report_root = config["paths"]["reports"]
    atomic_write_json(report_root / "formula1-show-artwork-attribution.json", {"records": records})
    atomic_write_text(report_root / "formula1-show-artwork-attribution.txt", "\n".join(lines))


async def _select_source(session, state, config, year, current, trigger_round, logger):
    roster, roster_source = await load_constructors(session, state, config, year, logger)
    diagnostics = []
    for constructor in _candidate_order(roster, current, trigger_round):
        try:
            candidates, search_source = await search_commons(
                session, state, config, year, constructor, roster, logger
            )
        except RuntimeError as error:
            diagnostics.append(f"{constructor.name}: {error}")
            continue
        for candidate in candidates:
            try:
                photo_path, image_source = await acquire_candidate_image(
                    session, config, candidate
                )
                return candidate, photo_path, {
                    "roster": roster_source,
                    "search": search_source,
                    "image": image_source,
                }
            except RuntimeError as error:
                diagnostics.append(f"{constructor.name}/{candidate.title}: {error}")
    reason = "; ".join(diagnostics[-4:]) or "no licensed current-season candidate matched"
    raise RuntimeError(reason)


async def run_show_artwork_rotation(
    session,
    state,
    config,
    show,
    race,
    path_data,
    logger,
):
    """Rotate only when a newly parsed Plex race round becomes authoritative."""
    trigger_round = int(race.round_number)
    logical_key = f"show:{show.year}"
    current = state.show_rotation(logical_key)
    render_fingerprint = _show_render_fingerprint(config)
    rerendering = False
    if current is not None:
        integrity = _pair_integrity(current)
        poster_reference, background_reference = _current_references(current)
        if integrity == "manual":
            episode_references, episode_actions = _existing_episode_outputs(
                state, config, show, race
            )
            return ShowArtworkResult(
                "preserve-manual",
                trigger_round,
                poster_reference,
                background_reference,
                current["constructor_id"],
                "Managed show artwork was modified; automatic rotation is paused",
                episode_references=episode_references,
                episode_actions=episode_actions,
            )
        if trigger_round < int(current["trigger_round"]) and integrity == "managed":
            episode_references, episode_actions = _existing_episode_outputs(
                state, config, show, race
            )
            return ShowArtworkResult(
                "unchanged",
                trigger_round,
                poster_reference,
                background_reference,
                current["constructor_id"],
                episode_references=episode_references,
                episode_actions=episode_actions,
            )
        if trigger_round < int(current["trigger_round"]) and integrity == "missing":
            episode_references, episode_actions = _existing_episode_outputs(
                state, config, show, race
            )
            return ShowArtworkResult(
                "preserved",
                trigger_round,
                poster_reference,
                background_reference,
                current["constructor_id"],
                "The active artwork belongs to a newer round; repair waits for that round",
                episode_references=episode_references,
                episode_actions=episode_actions,
            )
        if trigger_round == int(current["trigger_round"]) and integrity == "managed":
            rerendering = (
                current.get("source", {}).get("render_fingerprint") != render_fingerprint
            )
            if not rerendering:
                candidate = CommonsCandidate.from_dict(current["source"]["candidate"])
                try:
                    photo_path, _image_source = await acquire_candidate_image(
                        session, config, candidate
                    )
                    source_identity = candidate.source_sha1 or hashlib.sha256(
                        candidate.image_url.encode()
                    ).hexdigest()
                    episode_references, episode_actions = _reconcile_episode_posters(
                        state,
                        config,
                        show,
                        race,
                        path_data,
                        photo_path,
                        source_identity,
                    )
                    issue = None
                except RuntimeError as error:
                    episode_references, episode_actions = _existing_episode_outputs(
                        state, config, show, race
                    )
                    issue = f"Episode artwork source unavailable: {error}"
                return ShowArtworkResult(
                    "unchanged",
                    trigger_round,
                    poster_reference,
                    background_reference,
                    current["constructor_id"],
                    issue,
                    episode_references=episode_references,
                    episode_actions=episode_actions,
                )

    restoring = (
        current is not None
        and trigger_round == int(current["trigger_round"])
        and _pair_integrity(current) == "missing"
    )
    try:
        if restoring or rerendering:
            candidate = CommonsCandidate.from_dict(current["source"]["candidate"])
            photo_path, image_source = await acquire_candidate_image(session, config, candidate)
            provider_sources = {"roster": "state", "search": "state", "image": image_source}
            destination_root = Path(current["poster_destination"]).parent
        else:
            candidate, photo_path, provider_sources = await _select_source(
                session, state, config, show.year, current, trigger_round, logger
            )
            source_identity = candidate.source_sha1 or hashlib.sha256(
                candidate.image_url.encode()
            ).hexdigest()
            directory_name = (
                f"round-{trigger_round:02d}-{_slug(candidate.constructor_id)}-"
                f"{source_identity[:10]}"
            )
            destination_root = config["paths"]["show_assets"] / str(show.year) / directory_name
        source_identity = candidate.source_sha1 or hashlib.sha256(
            candidate.image_url.encode()
        ).hexdigest()
    except RuntimeError as error:
        poster_reference, background_reference = _current_references(current or {})
        episode_references, episode_actions = _existing_episode_outputs(
            state, config, show, race
        )
        return ShowArtworkResult(
            "preserved" if current else "missing",
            trigger_round,
            poster_reference,
            background_reference,
            current.get("constructor_id") if current else None,
            f"No safe Wikimedia Commons car image: {error}",
            episode_references=episode_references,
            episode_actions=episode_actions,
        )

    relative = destination_root.relative_to(config["paths"]["show_assets"])
    poster_destination = destination_root / "poster.png"
    background_destination = destination_root / "background.png"
    poster_reference = _asset_reference(config, relative / "poster.png")
    background_reference = _asset_reference(config, relative / "background.png")
    episode_references, episode_actions = _reconcile_episode_posters(
        state,
        config,
        show,
        race,
        path_data,
        photo_path,
        source_identity,
    )
    if config["dry_run"]:
        return ShowArtworkResult(
            (
                "rerender-planned"
                if rerendering
                else "restore-planned"
                if restoring
                else "rotate-planned"
            ),
            trigger_round,
            poster_reference,
            background_reference,
            candidate.constructor_name,
            episode_references=episode_references,
            episode_actions=episode_actions,
        )

    poster_checksum = render_show_poster(
        show, race, path_data, photo_path, config, poster_destination
    )
    background_checksum = render_show_background(
        show, race, photo_path, config, background_destination
    )
    source = {
        "candidate": candidate.as_dict(),
        "provider_sources": provider_sources,
        "poster_reference": poster_reference,
        "background_reference": background_reference,
        "renderer_version": SHOW_RENDERER_VERSION,
        "render_fingerprint": render_fingerprint,
        "photo_cache": str(photo_path),
        "generated_checksums": {
            "poster": poster_checksum,
            "background": background_checksum,
        },
        "trigger": "plex_new_race",
    }
    state.save_show_rotation(
        logical_key,
        show.year,
        trigger_round,
        candidate.constructor_id,
        source,
        poster_destination,
        poster_checksum,
        background_destination,
        background_checksum,
    )
    current = state.show_rotation(logical_key)
    pairs_pruned = _prune_retained_pairs(state, config, logical_key, current)
    cache_pruned = _prune_source_cache(config, photo_path)
    _attribution_reports(config, state.show_rotation_history())
    return ShowArtworkResult(
        "rerendered" if rerendering else "restored" if restoring else "rotated",
        trigger_round,
        poster_reference,
        background_reference,
        candidate.constructor_name,
        pairs_pruned=pairs_pruned,
        cache_pruned=cache_pruned,
        episode_references=episode_references,
        episode_actions=episode_actions,
    )
