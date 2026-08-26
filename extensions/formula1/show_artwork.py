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
    country_flag_asset,
    fitted_font,
    opaque_flag,
    svg_path_points,
)
from extensions.formula1.commons import (
    CommonsCandidate,
    acquire_candidate_image,
    load_constructors,
    search_commons,
)
from extensions.formula1.safety_car import (
    SafetyCarCandidate,
    classify_image_environment,
    derive_race_environment,
    environment_compatible,
    search_safety_cars,
)
from extensions.formula1.sessions import session_date
from helper.io import atomic_replace_file, atomic_write_json, atomic_write_text

FILE_MODE = 0o664
SHOW_RENDERER_VERSION = 8
EPISODE_RENDERER_VERSION = 1

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
    photo_path: str | None = None
    source_identity: str | None = None
    background_vehicle: str | None = None


@dataclass(frozen=True)
class EpisodeRoundArtworkResult:
    constructor: str | None = None
    references: dict[int, str] = field(default_factory=dict)
    actions: dict[int, str] = field(default_factory=dict)
    issue: str | None = None


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
            "cinematic-race-aware-background-v1",
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


def _rounded_flag_badge(image, country, box):
    """Place an undistorted host flag inside a restrained rounded badge."""
    flag_path = country_flag_asset(country)
    if flag_path is None:
        return False
    left, top, right, bottom = map(round, box)
    width = max(1, right - left)
    height = max(1, bottom - top)
    radius = max(3, round(height * 0.22))
    padding = max(3, round(height * 0.10))
    shadow_offset = max(2, round(height * 0.07))

    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (
            left + shadow_offset,
            top + shadow_offset,
            right + shadow_offset,
            bottom + shadow_offset,
        ),
        radius=radius,
        fill=(0, 0, 0, 105),
    )
    image.paste(shadow, (0, 0), shadow)

    badge = Image.new("RGB", (width, height), (18, 19, 23))
    with Image.open(flag_path) as source:
        flag = ImageOps.contain(
            opaque_flag(source),
            (max(1, width - padding * 2), max(1, height - padding * 2)),
            Image.Resampling.LANCZOS,
        )
    badge.paste(flag, ((width - flag.width) // 2, (height - flag.height) // 2))
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, width - 1, height - 1), radius=radius, fill=255
    )
    image.paste(badge, (left, top), mask)
    ImageDraw.Draw(image).rounded_rectangle(
        (left, top, right - 1, bottom - 1),
        radius=radius,
        outline=(238, 238, 242),
        width=max(2, round(height * 0.035)),
    )
    return True


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
    date, _session_field = session_date(episode, race, config)
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
    flag_available = country_flag_asset(race.country) is not None
    detail = race.circuit if flag_available else f"{race.circuit}  •  {race.country}"
    regular = fitted_font(regular_path, detail, max(26, width // 28), 18, width * 0.66)
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
    _rounded_flag_badge(
        image,
        race.country,
        (width * 0.79, height * 0.855, width * 0.94, height * 0.91),
    )
    draw.text(
        (width * 0.06, height * 0.94),
        "RACE-WEEK ROTATION",
        font=small_bold,
        fill=(255, 255, 255, 185),
    )
    return _atomic_save(image, destination)


def _saliency_focal_point(photo):
    """Estimate a robust subject centre from edges and local contrast."""
    sample = ImageOps.fit(photo.convert("RGB"), (96, 54), Image.Resampling.BILINEAR)
    edges = sample.convert("L").filter(ImageFilter.FIND_EDGES)
    values = list(edges.get_flattened_data())
    threshold = sorted(values)[round(len(values) * 0.72)]
    weighted_x = weighted_y = total = 0.0
    for index, value in enumerate(values):
        weight = max(0, value - threshold)
        if not weight:
            continue
        x, y = index % 96, index // 96
        # Prefer the television-safe right half when two subjects compete.
        weight *= 0.85 + x / 192
        weighted_x += x * weight
        weighted_y += y * weight
        total += weight
    if not total:
        return 0.62, 0.5
    return weighted_x / total / 95, weighted_y / total / 53


def _cinematic_crop(photo, size):
    focal_x, focal_y = _saliency_focal_point(photo)
    # Pillow's centering controls crop alignment, not subject destination. Bias
    # the crop gently so the detected subject remains in the right visual field.
    centering_x = max(0.2, min(0.8, focal_x - 0.10))
    return ImageOps.fit(
        photo.convert("RGB"), size, Image.Resampling.LANCZOS,
        centering=(centering_x, max(0.25, min(0.75, focal_y))),
    )


def _cinematic_grade(image, environment):
    """Produce restrained TV-safe contrast without crushing a night circuit."""
    image = ImageEnhance.Color(image).enhance(0.96)
    image = ImageEnhance.Contrast(image).enhance(1.04)
    image = ImageEnhance.Brightness(image).enhance(0.96 if environment == "night" else 0.94)
    # Lift deep shadows slightly and compress highlights to preserve track lights.
    lookup = []
    for value in range(256):
        normalized = value / 255
        lifted = normalized ** 0.92
        compressed = lifted / (1 + 0.10 * lifted)
        lookup.append(round(min(1.0, compressed * 1.05) * 255))
    return image.point(lookup * 3)


def render_show_background(show, race, photo_path, config, destination):
    """Render a race-aware, photo-only cinematic Plex background."""
    del show  # The clean background intentionally contains no season typography.
    width = config["show_artwork"]["background_width"]
    height = config["show_artwork"]["background_height"]
    environment = derive_race_environment(race)
    with Image.open(photo_path) as source:
        image = _cinematic_grade(_cinematic_crop(source, (width, height)), environment.mode)

    # A restrained left gradient preserves Plex title legibility without turning
    # the composition back into a black information panel.
    left_mask = Image.new("L", (width, 1))
    left_mask.putdata([
        round(52 * max(0.0, 1 - x / max(1, width * 0.66)) ** 1.7)
        for x in range(width)
    ])
    image = Image.composite(
        Image.new("RGB", (width, height), (4, 6, 10)),
        image,
        left_mask.resize((width, height)),
    )

    vignette = Image.new("L", (width, height), 0)
    border = max(20, round(min(width, height) * 0.07))
    ImageDraw.Draw(vignette).rectangle(
        (0, 0, width - 1, height - 1), outline=24, width=border
    )
    vignette = vignette.filter(ImageFilter.GaussianBlur(border * 1.5))
    image = Image.composite(Image.new("RGB", image.size, (3, 5, 8)), image, vignette)
    return _atomic_save(image, destination)


def _current_references(current):
    source = current.get("source") or {}
    return source.get("poster_reference"), source.get("background_reference")


def _current_background_vehicle(current):
    candidate = (current or {}).get("source", {}).get("background_candidate") or {}
    return candidate.get("vehicle_name")


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


def reconcile_episode_posters(
    state,
    config,
    show,
    race,
    path_data,
    photo_path,
    source_identity,
    *,
    missing_only=False,
):
    references = {}
    actions = {}
    for episode in show.episodes:
        if episode.round_number != race.round_number:
            continue
        destination = _episode_destination(config, episode)
        if missing_only and destination.is_file():
            previous = state.artwork(episode.logical_key)
            actions[episode.episode_number] = (
                "unchanged"
                if previous and _checksum(destination) == previous["checksum"]
                else "preserve-manual"
            )
            references[episode.episode_number] = _episode_reference(config, episode)
            continue
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


def _round_source_payload(candidate, provider_sources, photo_path, source_identity):
    return {
        "candidate": candidate.as_dict(),
        "provider_sources": provider_sources,
        "photo_cache": str(photo_path),
        "source_identity": source_identity,
    }


def _save_round_source(
    state,
    config,
    season_year,
    round_number,
    candidate,
    provider_sources,
    photo_path,
    source_identity,
):
    if config["dry_run"]:
        return
    state.save_episode_round_source(
        season_year,
        round_number,
        candidate.constructor_id,
        _round_source_payload(
            candidate, provider_sources, photo_path, source_identity
        ),
    )


async def reconcile_episode_round_artwork(
    session,
    state,
    config,
    show,
    race,
    path_data,
    logger,
):
    """Use one persistent, licensed team-car source for every episode in a round."""
    binding = state.episode_round_source(show.year, race.round_number)
    try:
        if binding is not None:
            source = binding["source"]
            candidate = CommonsCandidate.from_dict(source["candidate"])
            photo_path, image_source = await acquire_candidate_image(
                session, config, candidate
            )
            provider_sources = {
                **(source.get("provider_sources") or {}),
                "image": image_source,
            }
        else:
            candidate, photo_path, provider_sources = await _select_source(
                session,
                state,
                config,
                show.year,
                None,
                race.round_number,
                logger,
            )
        source_identity = candidate.source_sha1 or hashlib.sha256(
            candidate.image_url.encode()
        ).hexdigest()
        _save_round_source(
            state,
            config,
            show.year,
            race.round_number,
            candidate,
            provider_sources,
            photo_path,
            source_identity,
        )
        references, actions = reconcile_episode_posters(
            state,
            config,
            show,
            race,
            path_data,
            photo_path,
            source_identity,
        )
        return EpisodeRoundArtworkResult(
            candidate.constructor_name,
            references,
            actions,
        )
    except (KeyError, RuntimeError, ValueError) as error:
        references, actions = _existing_episode_outputs(
            state, config, show, race
        )
        return EpisodeRoundArtworkResult(
            binding["constructor_id"] if binding else None,
            references,
            actions,
            f"No safe persistent round car image: {error}",
        )


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


def _prune_source_cache(config, active_photo, *additional_active_photos):
    cutoff = time.time() - config["show_artwork"]["source_cache_retention_days"] * 86400
    active = {
        Path(value).resolve()
        for value in (active_photo, *additional_active_photos)
        if value is not None
    }
    removed = 0
    root = config["paths"]["show_image_cache"]
    for path in root.glob("*") if root.exists() else ():
        if (
            path.is_file()
            and path.suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp"}
            and path.resolve() not in active
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


def _attribution_reports(config, history, round_sources=()):
    if config["dry_run"]:
        return
    records = []
    lines = ["MetaFusion Formula 1 artwork attribution", ""]
    for item in history:
        source = item["source"]
        candidate = source["candidate"]
        poster_record = {
            "scope": "show_poster",
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
            "background": None,
        }
        records.append(poster_record)
        lines.extend(
            [
                f"Season {poster_record['season_year']} • Round "
                f"{poster_record['trigger_round']:02d}",
                "Scope: show poster",
                f"Team: {poster_record['constructor_name']}",
                f"Source: {poster_record['source_title']}",
                f"Page: {poster_record['source_page']}",
                f"Author: {poster_record['author']}",
                f"Licence: {poster_record['licence']}",
                f"Licence URL: {poster_record['licence_url']}",
                f"Poster: {poster_record['poster']}",
                "",
            ]
        )
        background = source.get("background_candidate")
        if background:
            background_sources = source.get("background_provider_sources") or {}
            background_record = {
                "scope": "show_background",
                "season_year": item["season_year"],
                "trigger_round": item["trigger_round"],
                "vehicle_name": background["vehicle_name"],
                "subject_type": background.get("subject_type", "safety_car"),
                "match_tier": background.get("match_tier", "season_safety_car"),
                "environment": background.get("environment", "unknown"),
                "observed_environment": background_sources.get(
                    "observed_environment", "unknown"
                ),
                "race_key": background.get("race_key", ""),
                "evidence": background.get("evidence", []),
                "source_title": background["title"],
                "source_page": background["page_url"],
                "author": background["author"],
                "licence": background["licence"],
                "licence_url": background["licence_url"],
                "poster": None,
                "background": item["background_destination"],
            }
            records.append(background_record)
            lines.extend(
                [
                    f"Season {background_record['season_year']} • Round "
                    f"{background_record['trigger_round']:02d}",
                    "Scope: show background",
                    f"Vehicle: {background_record['vehicle_name']}",
                    f"Subject: {background_record['subject_type']}",
                    f"Match tier: {background_record['match_tier']}",
                    f"Environment: {background_record['environment']}",
                    f"Observed pixels: {background_record['observed_environment']}",
                    f"Race key: {background_record['race_key']}",
                    f"Evidence: {', '.join(background_record['evidence']) or 'none'}",
                    f"Source: {background_record['source_title']}",
                    f"Page: {background_record['source_page']}",
                    f"Author: {background_record['author']}",
                    f"Licence: {background_record['licence']}",
                    f"Licence URL: {background_record['licence_url']}",
                    f"Background: {background_record['background']}",
                    "",
                ]
            )
    for item in round_sources:
        source = item.get("source") or {}
        candidate = source.get("candidate")
        if not candidate:
            continue
        record = {
            "scope": "episode_round",
            "season_year": item["season_year"],
            "trigger_round": item["round_number"],
            "constructor_id": item["constructor_id"],
            "constructor_name": candidate["constructor_name"],
            "source_title": candidate["title"],
            "source_page": candidate["page_url"],
            "author": candidate["author"],
            "licence": candidate["licence"],
            "licence_url": candidate["licence_url"],
            "poster": None,
            "background": None,
        }
        records.append(record)
        lines.extend(
            [
                f"Season {record['season_year']} • Round {record['trigger_round']:02d}",
                "Scope: episode cards",
                f"Team: {record['constructor_name']}",
                f"Source: {record['source_title']}",
                f"Page: {record['source_page']}",
                f"Author: {record['author']}",
                f"Licence: {record['licence']}",
                f"Licence URL: {record['licence_url']}",
                "",
            ]
        )
    report_root = config["paths"]["reports"]
    atomic_write_json(report_root / "formula1-show-artwork-attribution.json", {"records": records})
    atomic_write_text(report_root / "formula1-show-artwork-attribution.txt", "\n".join(lines))


def write_attribution_reports(state, config):
    _attribution_reports(
        config,
        state.show_rotation_history(),
        state.episode_round_sources(),
    )


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


def _perceptual_hash(path):
    with Image.open(path) as source:
        pixels = list(
            source.convert("L")
            .resize((9, 8), Image.Resampling.LANCZOS)
            .get_flattened_data()
        )
    bits: list[bool] = []
    for row in range(8):
        start = row * 9
        bits.extend(
            pixels[start + column] > pixels[start + column + 1]
            for column in range(8)
        )
    hash_value = 0
    for index, bit in enumerate(bits):
        if bit:
            hash_value |= 1 << index
    return f"{hash_value:016x}"


def _hash_distance(left, right):
    if not left or not right:
        return 64
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _safety_candidate_order(candidates, current, trigger_round):
    if not candidates:
        return []
    background = (current or {}).get("source", {}).get("background_candidate") or {}
    previous = int(background.get("page_id") or 0)
    ordered: list[SafetyCarCandidate] = []
    for tier in (
        "exact_event_circuit_safety_car",
        "exact_event_safety_car",
        "recent_circuit_safety_car",
        "exact_event_atmosphere",
        "season_safety_car",
    ):
        group = [candidate for candidate in candidates if candidate.match_tier == tier]
        if not group:
            continue
        page_ids = [candidate.page_id for candidate in group]
        if previous in page_ids:
            start = (page_ids.index(previous) + 1) % len(group)
        else:
            start = (int(trigger_round) - 1) % len(group)
        ordered.extend(group[(start + offset) % len(group)] for offset in range(len(group)))
    return ordered


async def _select_background_source(
    session, state, config, race, current, trigger_round, logger
):
    environment = derive_race_environment(race)
    candidates, search_source = await search_safety_cars(
        session, state, config, race, logger
    )
    if not candidates:
        raise RuntimeError(
            "no licensed exact-event/circuit Formula 1 background candidate matched"
        )
    current_source = (current or {}).get("source", {})
    previous_hash = current_source.get("background_perceptual_hash")
    fallback = None
    diagnostics = []
    for candidate in _safety_candidate_order(candidates, current, trigger_round):
        try:
            photo_path, image_source = await acquire_candidate_image(
                session, config, candidate
            )
            actual_environment = (
                classify_image_environment(photo_path)
                if Path(photo_path).is_file()
                else "unknown"
            )
            if actual_environment != "unknown" and not environment_compatible(
                environment.mode, actual_environment
            ):
                diagnostics.append(
                    f"{candidate.title}: expected {environment.mode} scene, "
                    f"pixels classified as {actual_environment}"
                )
                continue
            perceptual_hash = (
                _perceptual_hash(photo_path)
                if Path(photo_path).is_file()
                else hashlib.sha256(candidate.image_url.encode()).hexdigest()[:16]
            )
            result = (
                candidate,
                photo_path,
                {
                    "search": search_source,
                    "image": image_source,
                    "expected_environment": environment.mode,
                    "observed_environment": actual_environment,
                    "match_tier": candidate.match_tier,
                },
                perceptual_hash,
            )
            if fallback is None:
                fallback = result
            if _hash_distance(previous_hash, perceptual_hash) > 4:
                return result
        except RuntimeError as error:
            diagnostics.append(f"{candidate.title}: {error}")
    if fallback is not None:
        return fallback
    reason = "; ".join(diagnostics[-4:]) or "no race background passed pixel validation"
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
            background_value = current.get("source", {}).get("background_candidate") or {}
            expected_race_key = derive_race_environment(race).race_key
            rerendering = (
                current.get("source", {}).get("render_fingerprint") != render_fingerprint
                or background_value.get("race_key") != expected_race_key
            )
            if not rerendering:
                candidate = CommonsCandidate.from_dict(current["source"]["candidate"])
                available_photo = None
                available_identity = None
                try:
                    photo_path, _image_source = await acquire_candidate_image(
                        session, config, candidate
                    )
                    source_identity = candidate.source_sha1 or hashlib.sha256(
                        candidate.image_url.encode()
                    ).hexdigest()
                    available_photo = str(photo_path)
                    available_identity = source_identity
                    _save_round_source(
                        state,
                        config,
                        show.year,
                        trigger_round,
                        candidate,
                        {"roster": "state", "search": "state", "image": _image_source},
                        photo_path,
                        source_identity,
                    )
                    episode_references, episode_actions = reconcile_episode_posters(
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
                    photo_path=available_photo,
                    source_identity=available_identity,
                    background_vehicle=_current_background_vehicle(current),
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
            background_value = current["source"].get("background_candidate")
            if background_value and SafetyCarCandidate.from_dict(
                background_value
            ).race_key == derive_race_environment(race).race_key:
                background_candidate = SafetyCarCandidate.from_dict(background_value)
                background_photo_path, background_image_source = await acquire_candidate_image(
                    session, config, background_candidate
                )
                background_provider_sources = {
                    "search": "state",
                    "image": background_image_source,
                }
                background_perceptual_hash = (
                    _perceptual_hash(background_photo_path)
                    if Path(background_photo_path).is_file()
                    else hashlib.sha256(background_candidate.image_url.encode()).hexdigest()[:16]
                )
            else:
                (
                    background_candidate,
                    background_photo_path,
                    background_provider_sources,
                    background_perceptual_hash,
                ) = await _select_background_source(
                    session,
                    state,
                    config,
                    race,
                    current,
                    trigger_round,
                    logger,
                )
            destination_root = Path(current["poster_destination"]).parent
        else:
            candidate, photo_path, provider_sources = await _select_source(
                session, state, config, show.year, current, trigger_round, logger
            )
            (
                background_candidate,
                background_photo_path,
                background_provider_sources,
                background_perceptual_hash,
            ) = await _select_background_source(
                session,
                state,
                config,
                race,
                current,
                trigger_round,
                logger,
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
            f"No safe Wikimedia Commons team-car/race-background pair: {error}",
            episode_references=episode_references,
            episode_actions=episode_actions,
            background_vehicle=_current_background_vehicle(current),
        )

    relative = destination_root.relative_to(config["paths"]["show_assets"])
    poster_destination = destination_root / "poster.png"
    background_destination = destination_root / "background.png"
    poster_reference = _asset_reference(config, relative / "poster.png")
    background_reference = _asset_reference(config, relative / "background.png")
    _save_round_source(
        state,
        config,
        show.year,
        trigger_round,
        candidate,
        provider_sources,
        photo_path,
        source_identity,
    )
    episode_references, episode_actions = reconcile_episode_posters(
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
            photo_path=str(photo_path),
            source_identity=source_identity,
            background_vehicle=background_candidate.vehicle_name,
        )

    poster_checksum = render_show_poster(
        show, race, path_data, photo_path, config, poster_destination
    )
    background_checksum = render_show_background(
        show, race, background_photo_path, config, background_destination
    )
    source = {
        "candidate": candidate.as_dict(),
        "provider_sources": provider_sources,
        "background_candidate": background_candidate.as_dict(),
        "background_provider_sources": background_provider_sources,
        "background_photo_cache": str(background_photo_path),
        "background_source_identity": (
            background_candidate.source_sha1
            or hashlib.sha256(background_candidate.image_url.encode()).hexdigest()
        ),
        "background_perceptual_hash": background_perceptual_hash,
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
    cache_pruned = _prune_source_cache(config, photo_path, background_photo_path)
    write_attribution_reports(state, config)
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
        photo_path=str(photo_path),
        source_identity=source_identity,
        background_vehicle=background_candidate.vehicle_name,
    )
