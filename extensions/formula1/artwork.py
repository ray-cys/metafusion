"""Deterministic, managed Formula 1 round-poster rendering."""

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from helper.io import atomic_replace_file

FILE_MODE = 0o664
RENDERER_VERSION = 6
TOKENS = re.compile(r"[A-Za-z]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
FLAG_ASSET_ROOT = Path(__file__).with_name("assets") / "flags"
FLAG_ALPHA = round(255 * 0.78)
def _country_key(country):
    normalized = unicodedata.normalize("NFKD", str(country or ""))
    ascii_country = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z]+", " ", ascii_country.casefold()).strip()


def _country_flag_codes():
    source = FLAG_ASSET_ROOT / "countries.json"
    try:
        countries = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        countries = {}
    codes = {
        _country_key(name): str(code).casefold()
        for code, name in countries.items()
        if _country_key(name)
    }
    codes.update(
        {
            "great britain": "gb",
            "korea": "kr",
            "south korea": "kr",
            "turkey": "tr",
            "turkiye": "tr",
            "uae": "ae",
            "uk": "gb",
            "united states of america": "us",
            "us": "us",
            "usa": "us",
        }
    )
    return codes


COUNTRY_FLAG_CODES = _country_flag_codes()


def country_flag_asset(country):
    """Return the bundled flag asset for a provider country name, when known."""
    code = COUNTRY_FLAG_CODES.get(_country_key(country))
    if not code:
        return None
    candidate = FLAG_ASSET_ROOT / f"{code}.png"
    return candidate if candidate.is_file() else None


def opaque_flag(flag):
    """Flatten palette transparency onto white without Pillow palette warnings."""
    rgba = flag.convert("RGBA")
    canvas = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    canvas.alpha_composite(rgba)
    return canvas.convert("RGB")


def _flag_overlay(flag, width, height):
    """Fit an authentic, undistorted flag into the upper poster field."""
    fitted = ImageOps.contain(
        opaque_flag(flag),
        (max(1, round(width * 0.88)), max(1, round(height * 0.47))),
        method=Image.Resampling.LANCZOS,
    )
    left = (width - fitted.width) // 2
    centre_y = round(height * 0.38)
    top = max(0, min(height - fitted.height, centre_y - fitted.height // 2))
    layer = Image.new("RGB", (width, height))
    layer.paste(fitted, (left, top))
    feather = max(2, min(fitted.width, fitted.height) // 10)
    flag_mask = Image.new("L", fitted.size)
    mask_draw = ImageDraw.Draw(flag_mask)
    mask_draw.rectangle(
        (feather, feather, fitted.width - feather - 1, fitted.height - feather - 1),
        fill=FLAG_ALPHA,
    )
    flag_mask = flag_mask.filter(ImageFilter.GaussianBlur(feather))
    mask = Image.new("L", (width, height))
    mask.paste(flag_mask, (left, top))
    return layer, mask


def _render_background(country, width, height):
    """Render a neutral canvas that never recolours a host-country flag."""
    gradient = Image.new("RGB", (1, height))
    pixels = gradient.load()
    for y in range(height):
        progress = y / max(height - 1, 1)
        upper_glow = max(0.0, 1.0 - abs(progress - 0.30) / 0.42)
        pixels[0, y] = (
            round(12 + 16 * (1 - progress) + 5 * upper_glow),
            round(13 + 17 * (1 - progress) + 4 * upper_glow),
            round(17 + 20 * (1 - progress) + 4 * upper_glow),
        )
    image = gradient.resize((width, height))
    flag_path = country_flag_asset(country)
    if flag_path:
        with Image.open(flag_path) as flag:
            flag_layer, flag_mask = _flag_overlay(flag, width, height)
            image = Image.composite(flag_layer, image, flag_mask)
    return image


def _font(path, size):
    try:
        if path and Path(path).is_file():
            return ImageFont.truetype(str(path), size)
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default()


def branding_paths(config):
    """Resolve branding filenames inside the extension-owned branding directory."""
    branding = config["paths"]["branding"]
    artwork = config["artwork"]
    return {
        "logo": branding / Path(str(artwork.get("logo", "logo.png"))).name,
        "font_regular": branding / Path(
            str(artwork.get("font_regular", "font-regular.ttf"))
        ).name,
        "font_bold": branding / Path(
            str(artwork.get("font_bold", "font-bold.ttf"))
        ).name,
    }


def branding_fingerprint(config):
    values = {}
    for name, path in branding_paths(config).items():
        values[name] = {
            "name": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file()
            else "missing",
        }
    return values


def validate_branding(config):
    """Validate supplied files early; missing files intentionally use safe fallbacks."""
    paths = branding_paths(config)
    warnings = []
    logo = paths["logo"]
    if not logo.is_file():
        warnings.append(f"Logo not supplied; using text fallback ({logo})")
    else:
        try:
            with Image.open(logo) as image:
                image.verify()
            with Image.open(logo) as image:
                width, height = image.size
                if width < 120 or height < 40 or width * height > 20_000_000:
                    raise ValueError("dimensions are unsuitable")
        except (OSError, ValueError) as error:
            raise ValueError(f"Formula 1 logo is unreadable or unsuitable: {logo}") from error
    for name in ("font_regular", "font_bold"):
        path = paths[name]
        if not path.is_file():
            warnings.append(f"{name.replace('_', ' ').title()} not supplied; using fallback ({path})")
            continue
        try:
            ImageFont.truetype(str(path), 32)
        except OSError as error:
            raise ValueError(f"Formula 1 font is unreadable: {path}") from error
    return warnings


def fitted_font(path, text, maximum, minimum, maximum_width):
    """Return the largest configured font that keeps every supplied line in bounds."""
    for size in range(int(maximum), int(minimum) - 1, -2):
        font = _font(path, size)
        widths = []
        for line in str(text).splitlines() or [""]:
            left, _top, right, _bottom = font.getbbox(line or " ")
            widths.append(right - left)
        if max(widths, default=0) <= maximum_width:
            return font
    return _font(path, minimum)


def _curve(p0, p1, p2, p3, count=12):
    points = []
    for step in range(1, count + 1):
        t = step / count
        inverse = 1 - t
        points.append(
            (
                inverse**3 * p0[0]
                + 3 * inverse**2 * t * p1[0]
                + 3 * inverse * t**2 * p2[0]
                + t**3 * p3[0],
                inverse**3 * p0[1]
                + 3 * inverse**2 * t * p1[1]
                + 3 * inverse * t**2 * p2[1]
                + t**3 * p3[1],
            )
        )
    return points


def svg_path_points(path_data):
    """Flatten common SVG path commands into points for Pillow rendering."""
    tokens = TOKENS.findall(path_data or "")
    points = []
    index = 0
    command = None
    current = (0.0, 0.0)
    start = current
    last_control = current

    def numbers(count):
        nonlocal index
        if index + count > len(tokens) or any(tokens[index + i].isalpha() for i in range(count)):
            return None
        values = [float(tokens[index + i]) for i in range(count)]
        index += count
        return values

    while index < len(tokens):
        if tokens[index].isalpha():
            command = tokens[index]
            index += 1
        if command is None:
            break
        upper = command.upper()
        relative = command.islower()
        if upper == "Z":
            points.append(start)
            current = start
            command = None
            continue
        counts = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7}
        values = numbers(counts.get(upper, 0))
        if values is None or upper not in counts:
            break
        x, y = current
        if upper in {"M", "L", "T"}:
            target = (values[0] + x, values[1] + y) if relative else (values[0], values[1])
            current = target
            points.append(target)
            if upper == "M":
                start = target
                command = "l" if relative else "L"
        elif upper == "H":
            current = (values[0] + x if relative else values[0], y)
            points.append(current)
        elif upper == "V":
            current = (x, values[0] + y if relative else values[0])
            points.append(current)
        elif upper == "C":
            c1 = (values[0] + x, values[1] + y) if relative else (values[0], values[1])
            c2 = (values[2] + x, values[3] + y) if relative else (values[2], values[3])
            target = (values[4] + x, values[5] + y) if relative else (values[4], values[5])
            points.extend(_curve(current, c1, c2, target))
            current, last_control = target, c2
        elif upper == "S":
            c1 = (2 * x - last_control[0], 2 * y - last_control[1])
            c2 = (values[0] + x, values[1] + y) if relative else (values[0], values[1])
            target = (values[2] + x, values[3] + y) if relative else (values[2], values[3])
            points.extend(_curve(current, c1, c2, target))
            current, last_control = target, c2
        elif upper == "Q":
            control = (values[0] + x, values[1] + y) if relative else (values[0], values[1])
            target = (values[2] + x, values[3] + y) if relative else (values[2], values[3])
            cubic1 = (x + 2 * (control[0] - x) / 3, y + 2 * (control[1] - y) / 3)
            cubic2 = (
                target[0] + 2 * (control[0] - target[0]) / 3,
                target[1] + 2 * (control[1] - target[1]) / 3,
            )
            points.extend(_curve(current, cubic1, cubic2, target))
            current, last_control = target, control
        elif upper == "A":
            target = (values[5] + x, values[6] + y) if relative else (values[5], values[6])
            points.append(target)
            current = target
    return points


def _fit(points, box):
    if len(points) < 2:
        return []
    left, top, right, bottom = box
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    width = max(max(xs) - min(xs), 1)
    height = max(max(ys) - min(ys), 1)
    scale = min((right - left) / width, (bottom - top) / height)
    x_offset = left + ((right - left) - width * scale) / 2
    y_offset = top + ((bottom - top) - height * scale) / 2
    return [(x_offset + (x - min(xs)) * scale, y_offset + (y - min(ys)) * scale) for x, y in points]


def artwork_fingerprint(race, path_data, config):
    payload = {
        "race": {
            "year": race.year,
            "round_number": race.round_number,
            "name": race.name,
            "circuit": race.circuit,
            "locality": race.locality,
            "country": race.country,
            "race_date": race.race_date,
            "sprint_date": race.sprint_date,
            "session_dates": race.session_dates,
        },
        "path": path_data,
        "size": [config["artwork"]["width"], config["artwork"]["height"]],
        "branding": branding_fingerprint(config),
        "country_flag": COUNTRY_FLAG_CODES.get(_country_key(race.country)),
        "renderer": RENDERER_VERSION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def render_round_poster(race, path_data, config, destination):
    """Render a poster atomically and return its checksum."""
    width, height = config["artwork"]["width"], config["artwork"]["height"]
    image = _render_background(race.country, width, height)
    draw = ImageDraw.Draw(image, "RGBA")
    for x in range(-height, width, max(120, width // 6)):
        draw.line(
            (x, 0, x + height, height),
            fill=(205, 210, 220, 14),
            width=max(1, width // 1000),
        )
    panel_top_left = round(height * 0.52)
    panel_top_right = round(height * 0.46)
    draw.polygon(
        [(0, panel_top_left), (width, panel_top_right), (width, height), (0, height)],
        fill=(4, 5, 8, 218),
    )
    draw.line(
        (0, panel_top_left, width, panel_top_right),
        fill=(235, 20, 40, 125),
        width=max(2, width // 450),
    )

    paths = branding_paths(config)
    regular = paths["font_regular"]
    bold = paths["font_bold"]
    title = race.name.upper().replace(" GRAND PRIX", "\nGRAND PRIX")
    title_font = fitted_font(bold, title, max(56, width // 11), 34, width - 140)
    detail_font = fitted_font(
        regular,
        f"{race.circuit}\n{race.locality}, {race.country}",
        max(30, width // 25),
        22,
        width - 140,
    )
    round_font = _font(bold, max(26, width // 30))
    logo_path = paths["logo"]
    if logo_path.is_file():
        with Image.open(logo_path) as logo:
            logo.thumbnail((width // 3, height // 10))
            image.paste(logo.convert("RGBA"), (width - logo.width - 70, 55), logo.convert("RGBA"))
    else:
        draw.text((70, 60), "FORMULA 1", font=round_font, fill=(255, 255, 255, 235))

    draw.text(
        (70, height * 0.53),
        f"ROUND {race.round_number:02d}",
        font=round_font,
        fill=(255, 255, 255, 210),
    )
    draw.multiline_text((70, height * 0.59), title, font=title_font, fill="white", spacing=8)
    detail_y = int(height * 0.79)
    draw.text((70, detail_y), race.circuit, font=detail_font, fill=(255, 255, 255, 225))
    draw.text(
        (70, detail_y + 58),
        f"{race.locality}, {race.country}",
        font=detail_font,
        fill=(255, 255, 255, 190),
    )
    scheduled_dates = sorted(set(race.session_dates.values()))
    if race.race_date:
        scheduled_dates.append(race.race_date)
    date_text = (
        f"{min(scheduled_dates)} — {max(scheduled_dates)}"
        if scheduled_dates
        else "DATES TO BE CONFIRMED"
    )
    draw.text((70, detail_y + 125), date_text, font=detail_font, fill=(255, 255, 255, 225))
    if race.sprint:
        draw.rounded_rectangle(
            (70, detail_y + 195, 280, detail_y + 255), radius=15, fill=(235, 20, 40, 230)
        )
        draw.text((100, detail_y + 203), "SPRINT", font=round_font, fill="white")

    circuit = _fit(
        svg_path_points(path_data), (width * 0.47, height * 0.14, width * 0.92, height * 0.50)
    )
    if circuit:
        draw.line(circuit, fill=(0, 0, 0, 130), width=max(16, width // 45), joint="curve")
        draw.line(circuit, fill=(255, 255, 255, 240), width=max(6, width // 125), joint="curve")
    else:
        draw.ellipse(
            (width * 0.59, height * 0.20, width * 0.85, height * 0.42),
            outline=(255, 255, 255, 210),
            width=8,
        )

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(dir=destination.parent, suffix=".png")
        os.close(descriptor)
        temporary = Path(name)
        image.save(temporary, format="PNG", optimize=True)
        checksum = hashlib.sha256(temporary.read_bytes()).hexdigest()
        atomic_replace_file(temporary, destination, new_file_mode=FILE_MODE)
        temporary = None
        return checksum
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
