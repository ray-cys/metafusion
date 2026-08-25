"""Deterministic, managed Formula 1 round-poster rendering."""

import hashlib
import json
import math
import os
import re
import tempfile
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from helper.io import atomic_replace_file

FILE_MODE = 0o664
RENDERER_VERSION = 3
TOKENS = re.compile(r"[A-Za-z]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
FLAG_ASSET_ROOT = Path(__file__).with_name("assets") / "flags"
COUNTRY_FLAG_CODES = {
    "argentina": "ar",
    "australia": "au",
    "austria": "at",
    "azerbaijan": "az",
    "bahrain": "bh",
    "belgium": "be",
    "brazil": "br",
    "canada": "ca",
    "china": "cn",
    "france": "fr",
    "germany": "de",
    "great britain": "gb",
    "hungary": "hu",
    "india": "in",
    "italy": "it",
    "japan": "jp",
    "korea": "kr",
    "malaysia": "my",
    "mexico": "mx",
    "monaco": "mc",
    "morocco": "ma",
    "netherlands": "nl",
    "portugal": "pt",
    "qatar": "qa",
    "russia": "ru",
    "saudi arabia": "sa",
    "singapore": "sg",
    "south africa": "za",
    "south korea": "kr",
    "spain": "es",
    "sweden": "se",
    "switzerland": "ch",
    "turkey": "tr",
    "turkiye": "tr",
    "uae": "ae",
    "uk": "gb",
    "united arab emirates": "ae",
    "united kingdom": "gb",
    "united states": "us",
    "united states of america": "us",
    "us": "us",
    "usa": "us",
}
COUNTRY_COLORS = {
    "ae": (0, 90, 60),
    "at": (215, 20, 35),
    "au": (0, 36, 80),
    "az": (0, 159, 219),
    "be": (250, 205, 20),
    "bh": (206, 17, 38),
    "br": (0, 145, 70),
    "ca": (215, 20, 35),
    "cn": (210, 25, 35),
    "es": (215, 20, 35),
    "gb": (20, 45, 100),
    "hu": (0, 125, 90),
    "it": (0, 145, 70),
    "jp": (215, 20, 35),
    "mc": (210, 25, 35),
    "mx": (0, 105, 80),
    "nl": (235, 95, 20),
    "qa": (120, 25, 65),
    "sa": (0, 120, 70),
    "sg": (215, 20, 35),
    "us": (25, 55, 115),
}


def _country_key(country):
    normalized = unicodedata.normalize("NFKD", str(country or ""))
    ascii_country = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z]+", " ", ascii_country.casefold()).strip()


def country_flag_asset(country):
    """Return the bundled flag asset for a provider country name, when known."""
    code = COUNTRY_FLAG_CODES.get(_country_key(country))
    if not code:
        return None
    candidate = FLAG_ASSET_ROOT / f"{code}.png"
    return candidate if candidate.is_file() else None


def _flag_overlay(flag, width, height):
    """Fit a complete flag without distortion and feather it into a portrait canvas."""
    fitted = ImageOps.contain(
        flag.convert("RGB"),
        (width, height),
        method=Image.Resampling.LANCZOS,
    )
    left = (width - fitted.width) // 2
    top = (height - fitted.height) // 2
    layer = Image.new("RGB", (width, height))
    layer.paste(fitted, (left, top))
    band = Image.new("L", (1, fitted.height))
    pixels = band.load()
    feather = max(1, min(fitted.height // 5, height // 10))
    maximum_alpha = round(255 * 0.48)
    for y in range(fitted.height):
        distance = min(y + 1, fitted.height - y)
        pixels[0, y] = round(maximum_alpha * min(1.0, distance / feather))
    mask = Image.new("L", (width, height))
    mask.paste(band.resize((fitted.width, fitted.height)), (left, top))
    return layer, mask


def _render_background(country, width, height):
    code = COUNTRY_FLAG_CODES.get(_country_key(country))
    base = COUNTRY_COLORS.get(code or "", (25, 30, 42))
    gradient = Image.new("RGB", (1, height), base)
    pixels = gradient.load()
    for y in range(height):
        factor = 0.55 + 0.45 * (1 - y / max(height - 1, 1))
        glow = 1 + 0.04 * math.sin(y / 170)
        pixels[0, y] = tuple(min(255, int(channel * factor * glow)) for channel in base)
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
        "logo": str(config["artwork"].get("logo", "")),
        "font": str(config["artwork"].get("font_bold", "")),
        "country_flag": COUNTRY_FLAG_CODES.get(_country_key(race.country)),
        "renderer": RENDERER_VERSION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def render_round_poster(race, path_data, config, destination):
    """Render a poster atomically and return its checksum."""
    width, height = config["artwork"]["width"], config["artwork"]["height"]
    image = _render_background(race.country, width, height)
    draw = ImageDraw.Draw(image, "RGBA")
    draw.polygon(
        [(0, 0), (width, 0), (width, height * 0.56), (0, height * 0.72)], fill=(0, 0, 0, 58)
    )
    for x in range(-height, width, 90):
        draw.line((x, 0, x + height, height), fill=(255, 255, 255, 12), width=2)

    branding = config["paths"]["branding"]
    regular = branding / config["artwork"].get("font_regular", "font-regular.ttf").split("/")[-1]
    bold = branding / config["artwork"].get("font_bold", "font-bold.ttf").split("/")[-1]
    title_font = _font(bold, max(56, width // 11))
    detail_font = _font(regular, max(30, width // 25))
    round_font = _font(bold, max(26, width // 30))
    logo_path = branding / config["artwork"].get("logo", "logo.png").split("/")[-1]
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
    title = race.name.upper().replace(" GRAND PRIX", "\nGRAND PRIX")
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
