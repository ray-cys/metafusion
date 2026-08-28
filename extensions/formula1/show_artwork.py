"""Race-triggered, paired Formula 1 show poster/background rotation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

from PIL import (
    Image,
    ImageChops,
    ImageDraw,
    ImageEnhance,
    ImageFilter,
    ImageOps,
    ImageStat,
    PngImagePlugin,
)

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
from extensions.formula1.race_background import (
    ACTION_BACKGROUND_TIERS,
    BACKGROUND_CANDIDATE_VERSION,
    ELIGIBLE_BACKGROUND_TIERS,
    TEAM_CAR_FALLBACK_TIER,
    RaceBackgroundCandidate,
    classify_image_environment,
    derive_race_environment,
    environment_compatible,
    image_has_meaningful_colour,
    search_race_backgrounds,
)
from extensions.formula1.sessions import session_date
from helper.io import atomic_replace_file, atomic_write_json, atomic_write_text

FILE_MODE = 0o664
SHOW_RENDERER_VERSION = 17
SHOW_BACKGROUND_RENDERER_VERSION = 1
EPISODE_RENDERER_VERSION = 5

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
    poster_renderer_version: int | None = None
    poster_checksum: str | None = None


@dataclass(frozen=True)
class EpisodeRoundArtworkResult:
    constructor: str | None = None
    references: dict[int, str] = field(default_factory=dict)
    actions: dict[int, str] = field(default_factory=dict)
    issue: str | None = None


@dataclass(frozen=True)
class PosterPhotoProfile:
    median_luminance: float
    shadow_luminance: float
    highlight_luminance: float
    focal_x: float
    focal_y: float
    subject_box: tuple[float, float, float, float]
    composition_x: float


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


def _show_render_fingerprints(config):
    poster_payload = {
        "renderer": SHOW_RENDERER_VERSION,
        "branding": branding_fingerprint(config),
        "dimensions": [
            config["show_artwork"]["poster_width"],
            config["show_artwork"]["poster_height"],
        ],
        "design": "adaptive-concept-a-v5-speed-accent",
    }
    background_payload = {
        "renderer": SHOW_BACKGROUND_RENDERER_VERSION,
        "dimensions": [
            config["show_artwork"]["background_width"],
            config["show_artwork"]["background_height"],
        ],
        "design": "cinematic-race-aware-background-v1",
        "candidate_version": BACKGROUND_CANDIDATE_VERSION,
    }
    return tuple(
        hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        for payload in (poster_payload, background_payload)
    )


def _show_render_fingerprint(config):
    return hashlib.sha256(
        "|".join(_show_render_fingerprints(config)).encode()
    ).hexdigest()


def _place_logo(image, logo_path, box, fallback_font, *, align="left"):
    left, top, right, bottom = map(int, box)
    if logo_path.is_file():
        with Image.open(logo_path) as source:
            logo = source.convert("RGBA")
            logo.thumbnail((right - left, bottom - top), Image.Resampling.LANCZOS)
            logo_left = right - logo.width if align == "right" else left
            image.paste(logo, (logo_left, top), logo)
    else:
        ImageDraw.Draw(image).text(
            (right if align == "right" else left, top),
            "FORMULA 1",
            font=fallback_font,
            fill=(245, 245, 245, 255),
            anchor="ra" if align == "right" else "la",
        )


def _rounded_flag_badge(image, country, box):
    """Float an undistorted host flag with adaptive, television-safe separation."""
    flag_path = country_flag_asset(country)
    if flag_path is None:
        return False
    left, top, right, bottom = map(round, box)
    maximum_width = max(1, right - left)
    maximum_height = max(1, bottom - top)
    with Image.open(flag_path) as source:
        flag = ImageOps.contain(
            opaque_flag(source),
            (maximum_width, maximum_height),
            Image.Resampling.LANCZOS,
        )
    flag_left = right - flag.width
    flag_top = top + (maximum_height - flag.height) // 2
    flag_right = flag_left + flag.width
    flag_bottom = flag_top + flag.height
    radius = max(4, round(flag.height * 0.17))
    shadow_offset = max(2, round(flag.height * 0.05))
    shadow_blur = max(4, round(flag.height * 0.12))
    background_luminance = float(
        ImageStat.Stat(
            image.crop((flag_left, flag_top, flag_right, flag_bottom))
            .resize((24, 16), Image.Resampling.BILINEAR)
            .convert("L")
        ).median[0]
    )

    shadow_mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(shadow_mask).rounded_rectangle(
        (
            flag_left + shadow_offset,
            flag_top + shadow_offset,
            flag_right + shadow_offset,
            flag_bottom + shadow_offset,
        ),
        radius=radius,
        fill=82,
    )
    shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(shadow_blur))
    image.paste(Image.new("RGB", image.size, (2, 3, 5)), (0, 0), shadow_mask)

    mask = Image.new("L", flag.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, flag.width - 1, flag.height - 1), radius=radius, fill=255
    )
    edge = max(1, round(min(flag.size) * 0.04))
    flag_luminance = flag.resize((48, 32), Image.Resampling.BILINEAR).convert("L")
    edge_samples = [
        *flag_luminance.crop((0, 0, 48, edge)).get_flattened_data(),
        *flag_luminance.crop((0, 32 - edge, 48, 32)).get_flattened_data(),
        *flag_luminance.crop((0, 0, edge, 32)).get_flattened_data(),
        *flag_luminance.crop((48 - edge, 0, 48, 32)).get_flattened_data(),
    ]
    edge_luminance = float(sorted(edge_samples)[len(edge_samples) // 2])
    image.paste(flag, (flag_left, flag_top), mask)

    if abs(background_luminance - edge_luminance) < 64:
        outline = (
            (246, 247, 250, 52)
            if background_luminance < 128
            else (3, 5, 8, 72)
        )
        ImageDraw.Draw(image, "RGBA").rounded_rectangle(
            (flag_left, flag_top, flag_right - 1, flag_bottom - 1),
            radius=radius,
            outline=outline,
            width=max(1, round(flag.height * 0.015)),
        )
    return True


def _technical_frame(image):
    """Render the subtle diagonal texture used only by the show poster."""
    draw = ImageDraw.Draw(image, "RGBA")
    width, height = image.size
    spacing = max(50, width // 22)
    for offset in range(-height, width, spacing):
        draw.line(
            (offset, 0, offset + height, height),
            fill=(235, 20, 40, 14),
            width=max(1, width // 900),
        )


def _draw_circuit(draw, path_data, box, width):
    points = _fit(svg_path_points(path_data), box)
    if not points:
        return
    draw.line(points, fill=(0, 0, 0, 180), width=max(12, width // 65), joint="curve")
    draw.line(points, fill=(175, 175, 180, 150), width=max(4, width // 220), joint="curve")


def _draw_circuit_watermark(image, path_data, box, width, *, align="center"):
    """Composite a TV-legible circuit watermark using local image luminance."""
    points = _fit(svg_path_points(path_data), box)
    if not points:
        return image
    if align == "left":
        horizontal_shift = box[0] - min(point[0] for point in points)
    elif align == "right":
        horizontal_shift = box[2] - max(point[0] for point in points)
    else:
        horizontal_shift = 0
    if horizontal_shift:
        points = [(x + horizontal_shift, y) for x, y in points]
    left, top, right, bottom = (round(value) for value in box)
    sample = image.convert("L").crop((left, top, right, bottom))
    statistics = ImageStat.Stat(sample)
    luminance = statistics.mean[0]
    detail = statistics.stddev[0]
    detail_boost = min(12, max(0, round((detail - 24) * 0.3)))
    shadow_alpha = min(
        90,
        round(48 + max(0, luminance - 90) * 0.25) + detail_boost,
    )
    highlight_alpha = min(
        108,
        max(58, round(96 - max(0, luminance - 85) * 0.2)) + detail_boost,
    )
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    draw.line(
        points,
        fill=(4, 7, 11, shadow_alpha),
        width=max(6, width // 120),
        joint="curve",
    )
    draw.line(
        points,
        fill=(235, 236, 238, highlight_alpha),
        width=max(2, width // 420),
        joint="curve",
    )
    return Image.alpha_composite(image.convert("RGBA"), overlay)


def _episode_circuit_box(width, height, text_side):
    """Place the circuit at the adaptive outer edge and below the F1 logo."""
    if text_side == "left":
        return (width * 0.045, height * 0.20, width * 0.245, height * 0.40)
    return (width * 0.755, height * 0.20, width * 0.955, height * 0.40)


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


def _poster_photo_profile(photo):
    """Measure light and composition without requiring a model or source metadata."""
    sample = photo.convert("RGB").resize((160, 90), Image.Resampling.BILINEAR)
    luminance = list(sample.convert("L").get_flattened_data())
    ordered = sorted(luminance)
    edges = list(
        sample.convert("L").filter(ImageFilter.FIND_EDGES).get_flattened_data()
    )
    edge_threshold = sorted(edges)[round(len(edges) * 0.70)]
    pixels = list(sample.get_flattened_data())
    weighted = []
    for index, (pixel, edge) in enumerate(zip(pixels, edges, strict=True)):
        x, y = index % 160, index // 160
        if x < 3 or x > 156 or y < 3 or y > 86:
            continue
        chroma = max(pixel) - min(pixel)
        weight = max(0.0, edge - edge_threshold) + max(0.0, chroma - 24) * 0.35
        if weight:
            # Cars normally occupy the lower two-thirds; this reduces sky/signage bias.
            weight *= 0.82 + y / 180
            weighted.append((x / 159, y / 89, weight))

    if weighted:
        total = sum(point[2] for point in weighted)
        focal_x = sum(point[0] * point[2] for point in weighted) / total
        focal_y = sum(point[1] * point[2] for point in weighted) / total
        ranked = sorted(weighted, key=lambda point: point[2], reverse=True)
        core = ranked[: max(8, round(len(ranked) * 0.55))]
        xs = sorted(point[0] for point in core)
        ys = sorted(point[1] for point in core)
        low = round((len(core) - 1) * 0.04)
        high = round((len(core) - 1) * 0.96)
        subject_box = (xs[low], ys[low], xs[high], ys[high])
    else:
        focal_x, focal_y = 0.5, 0.55
        subject_box = (0.08, 0.12, 0.92, 0.92)

    box_center = (subject_box[0] + subject_box[2]) / 2
    # Retain the source photograph's visual direction and existing lead room.
    composition_x = max(
        0.38, min(0.62, focal_x + (focal_x - box_center) * 0.30)
    )
    return PosterPhotoProfile(
        median_luminance=float(ordered[len(ordered) // 2]),
        shadow_luminance=float(ordered[round((len(ordered) - 1) * 0.10)]),
        highlight_luminance=float(ordered[round((len(ordered) - 1) * 0.90)]),
        focal_x=focal_x,
        focal_y=focal_y,
        subject_box=subject_box,
        composition_x=composition_x,
    )


def _adaptive_showcase_crop(photo, size, profile):
    """Fill a poster band while protecting the detected car and its visual lead room."""
    source = photo.convert("RGB")
    source_width, source_height = source.size
    target_ratio = size[0] / size[1]
    source_ratio = source_width / source_height
    if source_ratio >= target_ratio:
        crop_height = source_height
        crop_width = round(source_height * target_ratio)
        ideal_left = profile.focal_x * source_width - profile.composition_x * crop_width
        box_left = profile.subject_box[0] * source_width
        box_right = profile.subject_box[2] * source_width
        padding = crop_width * 0.025
        minimum = max(0.0, box_right + padding - crop_width)
        maximum = min(source_width - crop_width, box_left - padding)
        if minimum <= maximum:
            left = max(minimum, min(maximum, ideal_left))
        else:
            left = max(0.0, min(source_width - crop_width, ideal_left))
        box = (round(left), 0, round(left + crop_width), source_height)
    else:
        crop_width = source_width
        crop_height = round(source_width / target_ratio)
        ideal_top = profile.focal_y * source_height - 0.52 * crop_height
        box_top = profile.subject_box[1] * source_height
        box_bottom = profile.subject_box[3] * source_height
        padding = crop_height * 0.025
        minimum = max(0.0, box_bottom + padding - crop_height)
        maximum = min(source_height - crop_height, box_top - padding)
        if minimum <= maximum:
            top = max(minimum, min(maximum, ideal_top))
        else:
            top = max(0.0, min(source_height - crop_height, ideal_top))
        box = (0, round(top), source_width, round(top + crop_height))
    return source.crop(box).resize(size, Image.Resampling.LANCZOS)


def _lift_poster_subject(image, source, profile, daylight):
    """Lift a dark car without brightening the surrounding daylight scene."""
    width, height = image.size
    left = max(0, round((profile.subject_box[0] - 0.055) * width))
    top = max(0, round((profile.subject_box[1] - 0.085) * height))
    right = min(width - 1, round((profile.subject_box[2] + 0.055) * width))
    bottom = min(height - 1, round((profile.subject_box[3] + 0.085) * height))

    region = Image.new("L", image.size, 0)
    radius = max(8, round(min(width, height) * 0.055))
    ImageDraw.Draw(region).rounded_rectangle(
        (left, top, right, bottom),
        radius=radius,
        fill=round(190 + daylight * 35),
    )
    region = region.filter(ImageFilter.GaussianBlur(radius))

    shadow_lookup = []
    for value in range(256):
        shadow_weight = max(0.0, min(1.0, (172 - value) / 112))
        shadow_lookup.append(round(255 * shadow_weight**1.2))
    shadows = source.convert("L").point(shadow_lookup)
    mask = ImageChops.multiply(region, shadows)

    lifted = ImageEnhance.Brightness(image).enhance(1.18 + daylight * 0.12)
    return Image.composite(lifted, image, mask)


def _poster_showcase_grade(photo, size):
    """Adapt daylight or night car photography to a consistent television showcase."""
    source = photo.convert("RGB")
    profile = _poster_photo_profile(source)
    exposure_gain = max(
        0.82, min(1.36, 112.0 / max(1.0, profile.median_luminance))
    )

    cropped = _adaptive_showcase_crop(source, size, profile)
    cropped_profile = _poster_photo_profile(cropped)
    image = cropped
    daylight = max(0.0, min(1.0, (profile.highlight_luminance - 150) / 85))
    image = ImageEnhance.Color(image).enhance(1.02 - daylight * 0.06)
    image = ImageEnhance.Contrast(image).enhance(1.04 - daylight * 0.03)
    shoulder = 0.68 - daylight * 0.06
    highlight_strength = 0.78 - daylight * 0.18
    lookup = []
    for value in range(256):
        lifted = (value / 255) ** (0.90 if exposure_gain > 1 else 0.96)
        exposed = min(1.0, lifted * exposure_gain)
        compressed = (
            exposed
            if exposed <= shoulder
            else shoulder + (exposed - shoulder) * highlight_strength
        )
        lookup.append(round(min(1.0, compressed) * 255))
    image = image.point(lookup * 3)
    image = _lift_poster_subject(image, cropped, cropped_profile, daylight)

    width, height = size
    shade = Image.new("L", (1, height))
    top_shade = round(14 + daylight * 16)
    bottom_shade = round(4 + daylight * 7)
    shade.putdata(
        [
            round(top_shade + (bottom_shade - top_shade) * y / max(1, height - 1))
            for y in range(height)
        ]
    )
    black = Image.new("RGB", size, (3, 5, 9))
    image = Image.composite(black, image, shade.resize(size))

    vignette = Image.new("L", size, 0)
    border = max(12, round(min(size) * 0.075))
    ImageDraw.Draw(vignette).rectangle(
        (0, 0, width - 1, height - 1),
        outline=round(14 + daylight * 12),
        width=border,
    )
    vignette = vignette.filter(ImageFilter.GaussianBlur(border * 1.5))
    return Image.composite(black, image, vignette)


def _paste_showcase_band(image, band, top):
    """Blend the photograph into Concept A without a hard rectangular frame."""
    fade = max(12, round(band.height * 0.055))
    mask = Image.new("L", (1, band.height), 255)
    values = []
    for y in range(band.height):
        edge_distance = min(y, band.height - 1 - y)
        values.append(round(255 * min(1.0, edge_distance / fade)))
    mask.putdata(values)
    image.paste(band, (0, top), mask.resize(band.size))


def _draw_speed_accent(draw, width, height):
    """Balance the lower poster with a restrained, wordless motion motif."""
    thickness = max(4, round(height * 0.0055))
    bars = (
        (0.060, 0.927, 0.125, (235, 30, 48, 235)),
        (0.073, 0.944, 0.078, (142, 18, 34, 205)),
        (0.086, 0.961, 0.040, (238, 239, 242, 175)),
    )
    for left_ratio, top_ratio, length_ratio, colour in bars:
        left = round(width * left_ratio)
        top = round(height * top_ratio)
        draw.rounded_rectangle(
            (left, top, left + round(width * length_ratio), top + thickness),
            radius=max(2, thickness // 2),
            fill=colour,
        )


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
            config["show_artwork"]["episode_width"],
            config["show_artwork"]["episode_height"],
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


def _episode_text_side(profile):
    """Place copy opposite the detected subject while keeping centred shots stable."""
    subject_centre = (profile.subject_box[0] + profile.subject_box[2]) / 2
    weighted_centre = profile.focal_x * 0.6 + subject_centre * 0.4
    return "left" if weighted_centre >= 0.5 else "right"


def _episode_text_gradient(image, side):
    """Add only enough local shading to keep episode copy television-readable."""
    width, height = image.size
    region = (
        (0, 0, round(width * 0.48), height)
        if side == "left"
        else (round(width * 0.52), 0, width, height)
    )
    luminance = float(
        ImageStat.Stat(
            image.crop(region)
            .resize((32, 18), Image.Resampling.BILINEAR)
            .convert("L")
        ).median[0]
    )
    maximum_alpha = round(max(158, min(212, 142 + luminance * 0.34)))
    falloff = max(1, round(width * 0.56))
    mask = Image.new("L", (width, 1))
    values = []
    for x in range(width):
        distance = x if side == "left" else width - 1 - x
        strength = max(0.0, 1.0 - distance / falloff) ** 1.7
        values.append(round(maximum_alpha * strength))
    mask.putdata(values)
    shade = Image.new("RGBA", image.size, (3, 5, 9, 255))
    shade.putalpha(mask.resize(image.size))
    return Image.alpha_composite(image.convert("RGBA"), shade)


def _episode_display_title(episode):
    concise = {
        "qualifying": "QUALIFYING",
        "race": "RACE",
        "sprint": "SPRINT",
    }
    return concise.get(episode.program_kind, episode.program_title.upper())


def _episode_title_layout(font_path, title, maximum, minimum, maximum_width):
    """Keep common sessions large and split only genuinely long programme names."""
    single_font = fitted_font(font_path, title, maximum, minimum, maximum_width)
    single_width = single_font.getbbox(title)[2] - single_font.getbbox(title)[0]
    if single_width <= maximum_width and single_font.size >= maximum * 0.72:
        return title, single_font

    words = title.split()
    best = None
    for index in range(1, len(words)):
        candidate = " ".join(words[:index]) + "\n" + " ".join(words[index:])
        font = fitted_font(font_path, candidate, maximum, minimum, maximum_width)
        widths = [font.getbbox(line)[2] - font.getbbox(line)[0] for line in candidate.splitlines()]
        score = (font.size, -max(widths), -abs(widths[0] - widths[1]))
        if best is None or score > best[0]:
            best = (score, candidate, font)
    if best is not None:
        return best[1], best[2]
    return title, single_font


def _draw_episode_lines(draw, position, text, font, fill, *, align, spacing):
    """Draw predictable left- or right-aligned multiline copy and return its bounds."""
    x, y = position
    anchor = "rt" if align == "right" else "lt"
    line_height = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]
    widths = []
    for line_index, line in enumerate(text.splitlines()):
        draw.text(
            (x, y + line_index * (line_height + spacing)),
            line,
            font=font,
            fill=fill,
            anchor=anchor,
        )
        bounds = font.getbbox(line or " ")
        widths.append(bounds[2] - bounds[0])
    bottom = y + len(text.splitlines()) * line_height + max(
        0, len(text.splitlines()) - 1
    ) * spacing
    return max(widths, default=0), bottom


def _episode_date_label(value):
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%d %b %Y").upper()
    except ValueError:
        return str(value)


def render_episode_poster(episode, race, path_data, photo_path, config, destination):
    """Render an adaptive Concept A race/session card for Kometa metadata."""
    width = config["show_artwork"]["episode_width"]
    height = config["show_artwork"]["episode_height"]
    with Image.open(photo_path) as source:
        image = _poster_showcase_grade(source, (width, height)).convert("RGBA")
    profile = _poster_photo_profile(image.convert("RGB"))
    text_side = _episode_text_side(profile)
    image = _episode_text_gradient(image, text_side)

    logo_path, regular_path, bold_path = _branding_paths(config)
    round_font = _font(bold_path, max(24, width // 45))
    session_title = _episode_display_title(episode)
    session_title, session_font = _episode_title_layout(
        bold_path,
        session_title,
        max(46, width // 15),
        max(26, width // 54),
        width * 0.45,
    )
    grand_prix = race.name.upper()
    race_font = fitted_font(
        regular_path, grand_prix, max(28, width // 32), 20, width * 0.45
    )
    date_font = _font(regular_path, max(22, width // 48))
    text_x = width * (0.045 if text_side == "left" else 0.955)
    align = "left" if text_side == "left" else "right"
    logo_box = (
        (width * 0.045, height * 0.055, width * 0.22, height * 0.15)
        if text_side == "left"
        else (width * 0.78, height * 0.055, width * 0.955, height * 0.15)
    )
    _place_logo(
        image,
        logo_path,
        logo_box,
        round_font,
        align=align,
    )
    image = _draw_circuit_watermark(
        image,
        path_data,
        _episode_circuit_box(width, height, text_side),
        width,
        align=text_side,
    )
    draw = ImageDraw.Draw(image, "RGBA")
    draw.text(
        (text_x, height * 0.47),
        f"ROUND {race.round_number:02d}  •  {race.year}",
        font=round_font,
        fill=(235, 30, 48, 245),
        anchor="rt" if text_side == "right" else "lt",
    )
    draw.text(
        (text_x, height * 0.56),
        grand_prix,
        font=race_font,
        fill=(245, 245, 247, 230),
        anchor="rt" if text_side == "right" else "lt",
    )
    title_width, title_bottom = _draw_episode_lines(
        draw,
        (text_x, height * 0.65),
        session_title,
        font=session_font,
        fill=(255, 255, 255, 255),
        align=align,
        spacing=max(4, round(height * 0.008)),
    )
    accent_width = max(width * 0.055, min(title_width * 0.28, width * 0.12))
    accent_top = min(height * 0.855, title_bottom + height * 0.025)
    accent_left = text_x if text_side == "left" else text_x - accent_width
    draw.rounded_rectangle(
        (
            accent_left,
            accent_top,
            accent_left + accent_width,
            accent_top + max(3, height * 0.006),
        ),
        radius=max(2, round(height * 0.003)),
        fill=(235, 30, 48, 245),
    )
    episode_date, _session_field = session_date(episode, race, config)
    if episode_date:
        draw.text(
            (text_x, min(height * 0.91, accent_top + height * 0.045)),
            _episode_date_label(episode_date),
            font=date_font,
            fill=(225, 226, 230, 178),
            anchor="rt" if text_side == "right" else "lt",
        )
    provenance = PngImagePlugin.PngInfo()
    provenance.add_text("MetaFusion asset", "Formula 1 episode artwork")
    provenance.add_text(
        "MetaFusion renderer", f"episode-poster-v{EPISODE_RENDERER_VERSION}"
    )
    provenance.add_text("MetaFusion design", "cinematic-broadcast-minimal-v4")
    provenance.add_text("MetaFusion text side", text_side)
    return _atomic_save(image.convert("RGB"), destination, pnginfo=provenance)


def _atomic_save(image, destination, *, pnginfo=None):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(dir=destination.parent, suffix=".png")
        os.close(descriptor)
        temporary = Path(name)
        image.save(temporary, format="PNG", optimize=True, pnginfo=pnginfo)
        checksum = _checksum(temporary)
        atomic_replace_file(temporary, destination, new_file_mode=FILE_MODE)
        temporary = None
        return checksum
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def render_show_poster(show, race, path_data, photo_path, config, destination):
    """Render adaptive Concept A around a protected team-car composition."""
    width = config["show_artwork"]["poster_width"]
    height = config["show_artwork"]["poster_height"]
    image = Image.new("RGB", (width, height), (6, 7, 10))
    _technical_frame(image)
    draw = ImageDraw.Draw(image, "RGBA")
    draw.polygon(
        [(0, height), (width, height * 0.81), (width, height), (0, height)],
        fill=(142, 0, 20, 175),
    )
    draw.rectangle(
        (width * 0.055, height * 0.315, width * 0.16, height * 0.319),
        fill=(235, 20, 40, 220),
    )
    logo_path, regular_path, bold_path = _branding_paths(config)
    flag_available = country_flag_asset(race.country) is not None
    detail = race.circuit if flag_available else f"{race.circuit}  •  {race.country}"
    regular = fitted_font(regular_path, detail, max(26, width // 28), 18, width * 0.66)
    bold = fitted_font(
        bold_path, f"{show.year} SEASON", max(42, width // 15), 28, width * 0.88
    )
    small_bold = _font(bold_path, max(24, width // 30))
    race_font = fitted_font(
        bold_path, race.name.upper(), max(46, width // 11), 25, width * 0.88
    )
    _place_logo(
        image,
        logo_path,
        (width * 0.06, height * 0.055, width * 0.38, height * 0.15),
        small_bold,
    )
    draw.text(
        (width * 0.06, height * 0.19),
        f"{show.year} SEASON",
        font=bold,
        fill=(245, 245, 245, 255),
    )
    draw.text(
        (width * 0.06, height * 0.27),
        f"CURRENT RACE  •  ROUND {race.round_number:02d}",
        font=small_bold,
        fill=(235, 30, 48, 255),
    )
    _draw_circuit(
        draw,
        path_data,
        (width * 0.63, height * 0.065, width * 0.93, height * 0.285),
        width,
    )
    band_height = round(height * 0.39)
    band_top = round(height * 0.345)
    with Image.open(photo_path) as source:
        band = _poster_showcase_grade(source, (width, band_height))
    _paste_showcase_band(image, band, band_top)
    draw = ImageDraw.Draw(image, "RGBA")
    draw.text(
        (width * 0.06, height * 0.765),
        race.name.upper(),
        font=race_font,
        fill=(255, 255, 255, 245),
    )
    draw.text(
        (width * 0.06, height * 0.855),
        detail,
        font=regular,
        fill=(241, 45, 61, 240),
    )
    _rounded_flag_badge(
        image,
        race.country,
        (width * 0.77, height * 0.835, width * 0.94, height * 0.905),
    )
    _draw_speed_accent(draw, width, height)
    provenance = PngImagePlugin.PngInfo()
    provenance.add_text("MetaFusion asset", "Formula 1 rotating show poster")
    provenance.add_text("MetaFusion renderer", f"show-poster-v{SHOW_RENDERER_VERSION}")
    provenance.add_text(
        "MetaFusion design", "adaptive-concept-a-v5-speed-accent"
    )
    provenance.add_text("MetaFusion championship year", str(show.year))
    provenance.add_text("MetaFusion trigger round", str(race.round_number))
    return _atomic_save(image, destination, pnginfo=provenance)


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


def _asset_integrity(path, expected_checksum):
    destination = Path(path)
    if not destination.exists():
        return "missing"
    if _checksum(destination) != expected_checksum:
        return "manual"
    return "managed"


def _pair_integrity(current):
    statuses = {
        _asset_integrity(current["poster_destination"], current["poster_checksum"]),
        _asset_integrity(current["background_destination"], current["background_checksum"]),
    }
    if "manual" in statuses:
        return "manual"
    if "missing" in statuses:
        return "missing"
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
                "subject_type": background.get("subject_type", "race_car"),
                "match_tier": background.get(
                    "match_tier", "exact_event_action_race_car"
                ),
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


def _background_candidate_order(candidates, current, trigger_round):
    if not candidates:
        return []
    background = (current or {}).get("source", {}).get("background_candidate") or {}
    previous = int(background.get("page_id") or 0)
    ordered: list[RaceBackgroundCandidate] = []
    for tier in ACTION_BACKGROUND_TIERS:
        group = [candidate for candidate in candidates if candidate.match_tier == tier]
        if not group:
            continue
        for quality_group in (
            [candidate for candidate in group if "4k-source" in candidate.evidence],
            [candidate for candidate in group if "4k-source" not in candidate.evidence],
        ):
            if not quality_group:
                continue
            quality_group.sort(key=lambda candidate: (-candidate.score, candidate.title.casefold()))
            page_ids = [candidate.page_id for candidate in quality_group]
            if previous in page_ids:
                start = (page_ids.index(previous) + 1) % len(quality_group)
            else:
                start = (int(trigger_round) - 1) % len(quality_group)
            ordered.extend(
                quality_group[(start + offset) % len(quality_group)]
                for offset in range(len(quality_group))
            )
    return ordered


def _background_acquisition_config(config):
    """Validate backgrounds against the safe lower-resolution fallback floor."""
    return {
        **config,
        "show_artwork": {
            **config["show_artwork"],
            "minimum_source_width": config["show_artwork"][
                "fallback_background_source_width"
            ],
            "minimum_source_height": config["show_artwork"][
                "fallback_background_source_height"
            ],
            "preferred_source_width": config["show_artwork"][
                "minimum_background_source_width"
            ],
            "preferred_source_height": config["show_artwork"][
                "minimum_background_source_height"
            ],
        },
    }


def _requested_commons_width(url, width):
    """Upgrade an older persisted Commons thumbnail URL without guessing another file."""
    return re.sub(r"/\d+px-", f"/{int(width)}px-", str(url), count=1)


async def _acquire_background_image(session, config, candidate):
    width = config["show_artwork"]["minimum_background_source_width"]
    candidate = replace(
        candidate,
        image_url=_requested_commons_width(candidate.image_url, width),
    )
    return await acquire_candidate_image(
        session,
        _background_acquisition_config(config),
        candidate,
    )


def _background_source_quality(config, candidate, photo_path):
    """Describe whether the decoded source reached the preferred 4K floor."""
    width, height = candidate.width, candidate.height
    if Path(photo_path).is_file():
        with Image.open(photo_path) as source:
            width, height = source.size
    preferred_width = config["show_artwork"]["minimum_background_source_width"]
    preferred_height = config["show_artwork"]["minimum_background_source_height"]
    return (
        "4k-source"
        if width >= preferred_width and height >= preferred_height
        else "fallback-resolution-source"
    )


def _with_background_source_quality(config, candidate, photo_path):
    quality = _background_source_quality(config, candidate, photo_path)
    evidence = tuple(
        value
        for value in candidate.evidence
        if value not in {"4k-source", "fallback-resolution-source"}
    )
    return replace(candidate, evidence=(*evidence, quality)), quality


async def _team_car_background_fallback(session, config, race, candidate, diagnostics):
    """Use the selected current-season team car only after race-aware sources fail."""
    minimum_width = config["show_artwork"]["fallback_background_source_width"]
    minimum_height = config["show_artwork"]["fallback_background_source_height"]
    if candidate is None:
        raise RuntimeError("no selected current-season team-car fallback was available")
    if candidate.width < minimum_width or candidate.height < minimum_height:
        raise RuntimeError(
            f"selected team-car fallback is {candidate.width}x{candidate.height}; "
            f"minimum fallback source floor is {minimum_width}x{minimum_height}"
        )
    photo_path, image_source = await _acquire_background_image(
        session, config, candidate
    )
    photo_available = Path(photo_path).is_file()
    if photo_available and not image_has_meaningful_colour(photo_path):
        raise RuntimeError("selected team-car fallback was monochrome")
    if not photo_available and not config["dry_run"]:
        raise RuntimeError("selected team-car fallback was not cached after validation")
    source_quality = _background_source_quality(config, candidate, photo_path)
    environment = derive_race_environment(race)
    observed_environment = (
        classify_image_environment(photo_path) if photo_available else "unknown"
    )
    background_candidate = RaceBackgroundCandidate(
        page_id=candidate.page_id,
        title=candidate.title,
        page_url=candidate.page_url,
        image_url=_requested_commons_width(candidate.image_url, minimum_width),
        width=candidate.width,
        height=candidate.height,
        mime=candidate.mime,
        source_sha1=candidate.source_sha1,
        author=candidate.author,
        licence=candidate.licence,
        licence_url=candidate.licence_url,
        vehicle_name=candidate.constructor_name,
        score=candidate.score,
        subject_type="race_car",
        match_tier=TEAM_CAR_FALLBACK_TIER,
        environment=environment.mode,
        race_key=environment.race_key,
        evidence=("current-season-team-car", source_quality, "last-resort-fallback"),
        eligibility_version=BACKGROUND_CANDIDATE_VERSION,
    )
    perceptual_hash = (
        _perceptual_hash(photo_path)
        if photo_available
        else hashlib.sha256(candidate.image_url.encode()).hexdigest()[:16]
    )
    diagnostics.append(
        f"using {source_quality.replace('-', ' ')} current-season team-car fallback: "
        f"{candidate.title}"
    )
    return (
        background_candidate,
        photo_path,
        {
            "search": "show-poster-source",
            "image": image_source,
            "expected_environment": environment.mode,
            "observed_environment": observed_environment,
            "match_tier": TEAM_CAR_FALLBACK_TIER,
            "fallback_reason": "; ".join(diagnostics[-4:]),
        },
        perceptual_hash,
    )


async def _select_background_source(
    session,
    state,
    config,
    race,
    current,
    trigger_round,
    logger,
    team_candidate=None,
):
    environment = derive_race_environment(race)
    diagnostics = []
    try:
        candidates, search_source = await search_race_backgrounds(
            session, state, config, race, logger
        )
    except RuntimeError as error:
        candidates, search_source = [], "unavailable"
        diagnostics.append(f"race-aware search failed: {error}")
    if not candidates:
        diagnostics.append(
            "no safely licensed colour Formula 1 race-action photograph "
            "matched the exact event or circuit"
        )
    current_source = (current or {}).get("source", {})
    previous_hash = current_source.get("background_perceptual_hash")
    fallback = None
    for candidate in _background_candidate_order(candidates, current, trigger_round):
        try:
            photo_path, image_source = await _acquire_background_image(
                session, config, candidate
            )
            if Path(photo_path).is_file() and not image_has_meaningful_colour(photo_path):
                diagnostics.append(f"{candidate.title}: monochrome image rejected")
                continue
            candidate, source_quality = _with_background_source_quality(
                config, candidate, photo_path
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
                    "source_quality": source_quality,
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
    try:
        return await _team_car_background_fallback(
            session, config, race, team_candidate, diagnostics
        )
    except RuntimeError as error:
        diagnostics.append(str(error))
        reason = "; ".join(diagnostics[-4:])
        raise RuntimeError(reason) from error


def _rerender_current_poster(
    state,
    config,
    show,
    race,
    path_data,
    current,
    candidate,
    photo_path,
    image_source,
    poster_render_fingerprint,
    background_render_fingerprint,
    render_fingerprint,
    *,
    background_integrity,
    action,
    planned_action,
):
    """Restore or update a managed poster without touching its background."""
    trigger_round = int(race.round_number)
    source_identity = candidate.source_sha1 or hashlib.sha256(
        candidate.image_url.encode()
    ).hexdigest()
    provider_sources = {"roster": "state", "search": "state", "image": image_source}
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
    poster_reference, background_reference = _current_references(current)
    issue = (
        "Background artwork is manually modified; it was preserved while the managed "
        "poster renderer was updated"
        if background_integrity == "manual"
        else None
    )
    if config["dry_run"]:
        return ShowArtworkResult(
            planned_action,
            trigger_round,
            poster_reference,
            background_reference,
            candidate.constructor_name,
            issue,
            episode_references=episode_references,
            episode_actions=episode_actions,
            photo_path=str(photo_path),
            source_identity=source_identity,
            background_vehicle=_current_background_vehicle(current),
            poster_renderer_version=SHOW_RENDERER_VERSION,
        )

    poster_destination = Path(current["poster_destination"])
    poster_checksum = render_show_poster(
        show, race, path_data, photo_path, config, poster_destination
    )
    source = dict(current["source"])
    source.update(
        {
            "candidate": candidate.as_dict(),
            "provider_sources": provider_sources,
            "renderer_version": SHOW_RENDERER_VERSION,
            "poster_render_fingerprint": poster_render_fingerprint,
            # A missing background fingerprint identifies a legacy paired record.
            # Its managed background is accepted in place without being rewritten.
            "background_render_fingerprint": source.get(
                "background_render_fingerprint", background_render_fingerprint
            ),
            "render_fingerprint": render_fingerprint,
            "photo_cache": str(photo_path),
        }
    )
    checksums = dict(source.get("generated_checksums") or {})
    checksums["poster"] = poster_checksum
    checksums.setdefault("background", current["background_checksum"])
    source["generated_checksums"] = checksums
    state.save_show_rotation(
        current["logical_key"],
        show.year,
        trigger_round,
        candidate.constructor_id,
        source,
        poster_destination,
        poster_checksum,
        current["background_destination"],
        current["background_checksum"],
    )
    write_attribution_reports(state, config)
    return ShowArtworkResult(
        action,
        trigger_round,
        poster_reference,
        background_reference,
        candidate.constructor_name,
        issue,
        episode_references=episode_references,
        episode_actions=episode_actions,
        photo_path=str(photo_path),
        source_identity=source_identity,
        background_vehicle=_current_background_vehicle(current),
        poster_renderer_version=SHOW_RENDERER_VERSION,
        poster_checksum=poster_checksum,
    )


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
    poster_render_fingerprint, background_render_fingerprint = (
        _show_render_fingerprints(config)
    )
    render_fingerprint = _show_render_fingerprint(config)
    rerendering = False
    poster_only_maintenance = False
    poster_repairing = False
    background_integrity = "managed"
    if current is not None:
        integrity = _pair_integrity(current)
        poster_integrity = _asset_integrity(
            current["poster_destination"], current["poster_checksum"]
        )
        background_integrity = _asset_integrity(
            current["background_destination"], current["background_checksum"]
        )
        poster_reference, background_reference = _current_references(current)
        same_round = trigger_round == int(current["trigger_round"])
        current_source = current.get("source", {})
        poster_rerendering = same_round and (
            current_source.get("poster_render_fingerprint")
            != poster_render_fingerprint
        )
        # Legacy paired records have no independent background fingerprint. Their
        # already-managed background is adopted in place during poster migration.
        background_rerendering = same_round and (
            current_source.get("background_render_fingerprint") is not None
            and current_source.get("background_render_fingerprint")
            != background_render_fingerprint
        )
        if same_round and background_integrity == "managed":
            background_value = current_source.get("background_candidate") or {}
            background_rerendering = background_rerendering or (
                background_value.get("race_key")
                != derive_race_environment(race).race_key
            )
        rerendering = poster_rerendering or background_rerendering
        poster_repairing = (
            same_round
            and poster_integrity == "missing"
            and background_integrity != "missing"
        )
        poster_only_maintenance = (
            (poster_rerendering or poster_repairing)
            and not background_rerendering
            and poster_integrity in {"managed", "missing"}
            and background_integrity != "missing"
        )
        if integrity == "manual" and not poster_only_maintenance:
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
        if same_round and integrity == "managed":
            rerendering = poster_rerendering or background_rerendering
            poster_only_maintenance = (
                poster_rerendering and not background_rerendering
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
            if poster_only_maintenance:
                return _rerender_current_poster(
                    state,
                    config,
                    show,
                    race,
                    path_data,
                    current,
                    candidate,
                    photo_path,
                    image_source,
                    poster_render_fingerprint,
                    background_render_fingerprint,
                    render_fingerprint,
                    background_integrity=background_integrity,
                    action="restored" if poster_repairing else "rerendered",
                    planned_action=(
                        "restore-planned" if poster_repairing else "rerender-planned"
                    ),
                )
            provider_sources = {"roster": "state", "search": "state", "image": image_source}
            background_value = current["source"].get("background_candidate")
            saved_background = (
                RaceBackgroundCandidate.from_dict(background_value)
                if background_value
                else None
            )
            if (
                saved_background
                and saved_background.eligibility_version == BACKGROUND_CANDIDATE_VERSION
                and saved_background.race_key == derive_race_environment(race).race_key
                and saved_background.subject_type == "race_car"
                and saved_background.match_tier in ELIGIBLE_BACKGROUND_TIERS
            ):
                background_candidate = saved_background
                background_photo_path, background_image_source = await _acquire_background_image(
                    session, config, background_candidate
                )
                if not image_has_meaningful_colour(background_photo_path):
                    raise RuntimeError("saved background source was monochrome")
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
                    candidate,
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
                candidate,
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
            "repair-failed" if restoring else "preserved" if current else "missing",
            trigger_round,
            poster_reference,
            background_reference,
            current.get("constructor_id") if current else None,
            (
                f"No safe cached/current team-car source for poster maintenance: {error}"
                if poster_only_maintenance
                else f"No safe Wikimedia Commons race-car/background pair: {error}"
            ),
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
        "poster_render_fingerprint": poster_render_fingerprint,
        "background_render_fingerprint": background_render_fingerprint,
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
        poster_renderer_version=SHOW_RENDERER_VERSION,
        poster_checksum=poster_checksum,
    )
