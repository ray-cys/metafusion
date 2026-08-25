"""Race-triggered, paired Formula 1 show poster/background rotation."""

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from extensions.formula1.artwork import _fit, _font, svg_path_points
from extensions.formula1.commons import (
    CommonsCandidate,
    acquire_candidate_image,
    load_constructors,
    search_commons,
)
from helper.io import atomic_replace_file, atomic_write_json, atomic_write_text

FILE_MODE = 0o664
SHOW_RENDERER_VERSION = 1


@dataclass(frozen=True)
class ShowArtworkResult:
    action: str
    trigger_round: int
    poster_reference: str | None = None
    background_reference: str | None = None
    constructor: str | None = None
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
    branding = config["paths"]["branding"]
    artwork = config["artwork"]
    return (
        branding / str(artwork.get("logo", "logo.png")).split("/")[-1],
        branding / str(artwork.get("font_regular", "font-regular.ttf")).split("/")[-1],
        branding / str(artwork.get("font_bold", "font-bold.ttf")).split("/")[-1],
    )


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
    regular = _font(regular_path, max(26, width // 28))
    bold = _font(bold_path, max(42, width // 15))
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
        band = _photo_band(source, (width, band_height))
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
        f"{race.circuit}  •  {race.locality}, {race.country}",
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
    detail_font = _font(bold_path, max(22, width // 55))
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
        f"ROUND {race.round_number:02d}  •  {race.name.upper()}",
        font=detail_font,
        fill=(240, 240, 242, 220),
    )
    return _atomic_save(image.convert("RGB"), destination)


def _current_references(current):
    source = current.get("source") or {}
    return source.get("poster_reference"), source.get("background_reference")


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
    if current is not None:
        integrity = _pair_integrity(current)
        poster_reference, background_reference = _current_references(current)
        if integrity == "manual":
            return ShowArtworkResult(
                "preserve-manual",
                trigger_round,
                poster_reference,
                background_reference,
                current["constructor_id"],
                "Managed show artwork was modified; automatic rotation is paused",
            )
        if trigger_round <= int(current["trigger_round"]) and integrity == "managed":
            return ShowArtworkResult(
                "unchanged",
                trigger_round,
                poster_reference,
                background_reference,
                current["constructor_id"],
            )

    restoring = current is not None and trigger_round <= int(current["trigger_round"])
    try:
        if restoring:
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
    except RuntimeError as error:
        poster_reference, background_reference = _current_references(current or {})
        return ShowArtworkResult(
            "preserved" if current else "missing",
            trigger_round,
            poster_reference,
            background_reference,
            current.get("constructor_id") if current else None,
            f"No safe Wikimedia Commons car image: {error}",
        )

    relative = destination_root.relative_to(config["paths"]["show_assets"])
    poster_destination = destination_root / "poster.png"
    background_destination = destination_root / "background.png"
    poster_reference = _asset_reference(config, relative / "poster.png")
    background_reference = _asset_reference(config, relative / "background.png")
    if config["dry_run"]:
        return ShowArtworkResult(
            "restore-planned" if restoring else "rotate-planned",
            trigger_round,
            poster_reference,
            background_reference,
            candidate.constructor_name,
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
    _attribution_reports(config, state.show_rotation_history())
    return ShowArtworkResult(
        "restored" if restoring else "rotated",
        trigger_round,
        poster_reference,
        background_reference,
        candidate.constructor_name,
    )
