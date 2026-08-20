import asyncio
import inspect
import os
import re
import time
from pathlib import Path

from plexapi.server import PlexServer

from helper.concurrency import CircuitOpenError, runtime_slot
from helper.logging import log_plex_event, redact_secrets
from helper.plex_paths import translate_plex_path

PLEX_COUNTRY_OVERRIDES = {
    "US": "United States of America",
    "GB": "United Kingdom",
    "RU": "Russia",
    "KR": "South Korea",
    "IR": "Iran",
    "VN": "Vietnam",
    "TW": "Taiwan",
    "CZ": "Czech Republic",
    "CD": "Democratic Republic of the Congo",
    "CG": "Republic of the Congo",
    "VE": "Venezuela",
    "SY": "Syria",
    "LA": "Laos",
    "MD": "Moldova",
    "MK": "North Macedonia",
    "BO": "Bolivia",
    "TZ": "Tanzania",
    "PS": "Palestine",
    "CI": "Ivory Coast",
    "CV": "Cape Verde",
    "FM": "Micronesia",
    "KN": "Saint Kitts and Nevis",
    "LC": "Saint Lucia",
    "VC": "Saint Vincent and the Grenadines",
    "WS": "Samoa",
    "ST": "Sao Tome and Principe",
    "TL": "Timor-Leste",
    "VA": "Vatican City",
    "SX": "Sint Maarten",
    "MF": "Saint Martin",
    "BL": "Saint Barthelemy",
    "BQ": "Caribbean Netherlands",
    "SS": "South Sudan",
    "XK": "Kosovo",
}

ISO_COUNTRY_NAMES = {
    "AF": "Afghanistan",
    "AL": "Albania",
    "DZ": "Algeria",
    "AS": "American Samoa",
    "AD": "Andorra",
    "AO": "Angola",
    "AI": "Anguilla",
    "AQ": "Antarctica",
    "AG": "Antigua and Barbuda",
    "AR": "Argentina",
    "AM": "Armenia",
    "AW": "Aruba",
    "AU": "Australia",
    "AT": "Austria",
    "AZ": "Azerbaijan",
    "BS": "Bahamas",
    "BH": "Bahrain",
    "BD": "Bangladesh",
    "BB": "Barbados",
    "BY": "Belarus",
    "BE": "Belgium",
    "BZ": "Belize",
    "BJ": "Benin",
    "BM": "Bermuda",
    "BT": "Bhutan",
    "BO": "Bolivia",
    "BA": "Bosnia and Herzegovina",
    "BW": "Botswana",
    "BV": "Bouvet Island",
    "BR": "Brazil",
    "IO": "British Indian Ocean Territory",
    "BN": "Brunei",
    "BG": "Bulgaria",
    "BF": "Burkina Faso",
    "BI": "Burundi",
    "KH": "Cambodia",
    "CM": "Cameroon",
    "CA": "Canada",
    "CV": "Cape Verde",
    "KY": "Cayman Islands",
    "CF": "Central African Republic",
    "TD": "Chad",
    "CL": "Chile",
    "CN": "China",
    "CX": "Christmas Island",
    "CC": "Cocos Islands",
    "CO": "Colombia",
    "KM": "Comoros",
    "CG": "Republic of the Congo",
    "CD": "Democratic Republic of the Congo",
    "CK": "Cook Islands",
    "CR": "Costa Rica",
    "CI": "Ivory Coast",
    "HR": "Croatia",
    "CU": "Cuba",
    "CY": "Cyprus",
    "CZ": "Czech Republic",
    "DK": "Denmark",
    "DJ": "Djibouti",
    "DM": "Dominica",
    "DO": "Dominican Republic",
    "EC": "Ecuador",
    "EG": "Egypt",
    "SV": "El Salvador",
    "GQ": "Equatorial Guinea",
    "ER": "Eritrea",
    "EE": "Estonia",
    "ET": "Ethiopia",
    "FK": "Falkland Islands",
    "FO": "Faroe Islands",
    "FJ": "Fiji",
    "FI": "Finland",
    "FR": "France",
    "GF": "French Guiana",
    "PF": "French Polynesia",
    "TF": "French Southern Territories",
    "GA": "Gabon",
    "GM": "Gambia",
    "GE": "Georgia",
    "DE": "Germany",
    "GH": "Ghana",
    "GI": "Gibraltar",
    "GR": "Greece",
    "GL": "Greenland",
    "GD": "Grenada",
    "GP": "Guadeloupe",
    "GU": "Guam",
    "GT": "Guatemala",
    "GG": "Guernsey",
    "GN": "Guinea",
    "GW": "Guinea-Bissau",
    "GY": "Guyana",
    "HT": "Haiti",
    "HM": "Heard Island and McDonald Islands",
    "VA": "Vatican City",
    "HN": "Honduras",
    "HK": "Hong Kong",
    "HU": "Hungary",
    "IS": "Iceland",
    "IN": "India",
    "ID": "Indonesia",
    "IR": "Iran",
    "IQ": "Iraq",
    "IE": "Ireland",
    "IM": "Isle of Man",
    "IL": "Israel",
    "IT": "Italy",
    "JM": "Jamaica",
    "JP": "Japan",
    "JE": "Jersey",
    "JO": "Jordan",
    "KZ": "Kazakhstan",
    "KE": "Kenya",
    "KI": "Kiribati",
    "KP": "North Korea",
    "KR": "South Korea",
    "KW": "Kuwait",
    "KG": "Kyrgyzstan",
    "LA": "Laos",
    "LV": "Latvia",
    "LB": "Lebanon",
    "LS": "Lesotho",
    "LR": "Liberia",
    "LY": "Libya",
    "LI": "Liechtenstein",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "MO": "Macau",
    "MK": "North Macedonia",
    "MG": "Madagascar",
    "MW": "Malawi",
    "MY": "Malaysia",
    "MV": "Maldives",
    "ML": "Mali",
    "MT": "Malta",
    "MH": "Marshall Islands",
    "MQ": "Martinique",
    "MR": "Mauritania",
    "MU": "Mauritius",
    "YT": "Mayotte",
    "MX": "Mexico",
    "FM": "Micronesia",
    "MD": "Moldova",
    "MC": "Monaco",
    "MN": "Mongolia",
    "ME": "Montenegro",
    "MS": "Montserrat",
    "MA": "Morocco",
    "MZ": "Mozambique",
    "MM": "Myanmar",
    "NA": "Namibia",
    "NR": "Nauru",
    "NP": "Nepal",
    "NL": "Netherlands",
    "NC": "New Caledonia",
    "NZ": "New Zealand",
    "NI": "Nicaragua",
    "NE": "Niger",
    "NG": "Nigeria",
    "NU": "Niue",
    "NF": "Norfolk Island",
    "MP": "Northern Mariana Islands",
    "NO": "Norway",
    "OM": "Oman",
    "PK": "Pakistan",
    "PW": "Palau",
    "PS": "Palestine",
    "PA": "Panama",
    "PG": "Papua New Guinea",
    "PY": "Paraguay",
    "PE": "Peru",
    "PH": "Philippines",
    "PN": "Pitcairn Islands",
    "PL": "Poland",
    "PT": "Portugal",
    "PR": "Puerto Rico",
    "QA": "Qatar",
    "RE": "Reunion",
    "RO": "Romania",
    "RU": "Russia",
    "RW": "Rwanda",
    "SH": "Saint Helena",
    "KN": "Saint Kitts and Nevis",
    "LC": "Saint Lucia",
    "PM": "Saint Pierre and Miquelon",
    "VC": "Saint Vincent and the Grenadines",
    "WS": "Samoa",
    "SM": "San Marino",
    "ST": "Sao Tome and Principe",
    "SA": "Saudi Arabia",
    "SN": "Senegal",
    "RS": "Serbia",
    "SC": "Seychelles",
    "SL": "Sierra Leone",
    "SG": "Singapore",
    "SX": "Sint Maarten",
    "SK": "Slovakia",
    "SI": "Slovenia",
    "SB": "Solomon Islands",
    "SO": "Somalia",
    "ZA": "South Africa",
    "GS": "South Georgia and the South Sandwich Islands",
    "SS": "South Sudan",
    "ES": "Spain",
    "LK": "Sri Lanka",
    "SD": "Sudan",
    "SR": "Suriname",
    "SJ": "Svalbard and Jan Mayen",
    "SZ": "Eswatini",
    "SE": "Sweden",
    "CH": "Switzerland",
    "SY": "Syria",
    "TW": "Taiwan",
    "TJ": "Tajikistan",
    "TZ": "Tanzania",
    "TH": "Thailand",
    "TL": "Timor-Leste",
    "TG": "Togo",
    "TK": "Tokelau",
    "TO": "Tonga",
    "TT": "Trinidad and Tobago",
    "TN": "Tunisia",
    "TR": "Turkey",
    "TM": "Turkmenistan",
    "TC": "Turks and Caicos Islands",
    "TV": "Tuvalu",
    "UG": "Uganda",
    "UA": "Ukraine",
    "AE": "United Arab Emirates",
    "GB": "United Kingdom",
    "US": "United States of America",
    "UM": "United States Minor Outlying Islands",
    "UY": "Uruguay",
    "UZ": "Uzbekistan",
    "VU": "Vanuatu",
    "VE": "Venezuela",
    "VN": "Vietnam",
    "VG": "British Virgin Islands",
    "VI": "United States Virgin Islands",
    "WF": "Wallis and Futuna",
    "EH": "Western Sahara",
    "YE": "Yemen",
    "ZM": "Zambia",
    "ZW": "Zimbabwe",
}

def get_plex_country(code):
    return PLEX_COUNTRY_OVERRIDES.get(code) or ISO_COUNTRY_NAMES.get(code) or code


def _external_ids(item):
    """Read modern and legacy Plex agent GUID formats."""
    values = []
    for guid in getattr(item, "guids", []) or []:
        value = getattr(guid, "id", guid)
        if value:
            values.append(str(value))
    legacy = getattr(item, "guid", None)
    if legacy:
        values.append(str(legacy))

    result = {"tmdb": None, "imdb": None, "tvdb": None}
    patterns = (
        ("tmdb", r"(?:tmdb|themoviedb)(?:://|/)(\d+)"),
        ("imdb", r"imdb(?:://|/)(tt\d+)"),
        ("tvdb", r"(?:tvdb|thetvdb)(?:://|/)(\d+)"),
    )
    for value in values:
        lowered = value.casefold()
        for name, pattern in patterns:
            match = re.search(pattern, lowered)
            if match and not result[name]:
                result[name] = match.group(1)
    return result


_SEASON_DIRECTORY = re.compile(
    r"^(?:season[ ._-]*\d+|s\d+|specials?|season[ ._-]*specials?)$",
    re.IGNORECASE,
)


def _discover_show_directory(season_directories, locations=None):
    directories = {Path(value) for value in season_directories if value}
    location_paths = {Path(value) for value in (locations or []) if value}
    if len(location_paths) == 1:
        location = next(iter(location_paths))
        if not directories or all(
            directory == location or location in directory.parents
            for directory in directories
        ):
            return location
    if not directories:
        return None
    if len(directories) == 1:
        directory = next(iter(directories))
        return directory.parent if _SEASON_DIRECTORY.match(directory.name) else directory
    try:
        common = Path(os.path.commonpath([str(path) for path in directories]))
    except ValueError:
        return None
    if common in directories:
        return common
    if all(path.parent == common for path in directories):
        return common
    if all(common == path or common in path.parents for path in directories):
        return common

def connect_plex_server(config):
    runtime = config.get("runtime", {})
    plex_timeout = max(
        1.0,
        float(runtime.get("plex_timeout", 10.0)),
    )
    retries = max(1, int(runtime.get("plex_retries", 3)))
    retry_delay = max(0.0, float(runtime.get("plex_retry_delay", 1.0)))
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            plex = PlexServer(
                config["plex"]["url"],
                config["plex"]["token"],
                timeout=plex_timeout,
            )
            log_plex_event("plex_connected", version=plex.version)
            return plex
        except Exception as error:
            last_error = error
            log_plex_event(
                "plex_connect_failed",
                error=redact_secrets(error, config.get("plex", {}).get("token")),
                attempt=attempt,
                retries=retries,
            )
            if attempt < retries and retry_delay:
                time.sleep(retry_delay * attempt)
    raise RuntimeError("Unable to connect to Plex") from last_error


def connect_plex_library(config, selected_libraries=None, plex=None):
    if selected_libraries is None:
        selected_libraries = config.get("plex_libraries") or []
    selected_libraries = [
        str(value).strip() for value in selected_libraries if str(value).strip()
    ]
    automatic = not selected_libraries or any(
        value.casefold() == "auto" for value in selected_libraries
    )
    plex = connect_plex_server(config) if plex is None else plex
    runtime = config.get("runtime", {})
    retries = max(1, int(runtime.get("plex_retries", 3)))
    retry_delay = max(0.0, float(runtime.get("plex_retry_delay", 1.0)))
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            sections = list(plex.library.sections())
            break
        except Exception as error:
            last_error = error
            log_plex_event(
                "plex_libraries_retrieved_failed",
                error=redact_secrets(error, config.get("plex", {}).get("token")),
                attempt=attempt,
                retries=retries,
            )
            if attempt < retries and retry_delay:
                time.sleep(retry_delay * attempt)
    else:
        raise RuntimeError("Unable to retrieve Plex libraries") from last_error

    libraries = [
        {
            "title": section.title,
            "type": getattr(section, "TYPE", None)
            or getattr(section, "type", None)
            or "unknown",
            "uuid": getattr(section, "uuid", None)
            or getattr(section, "key", None)
            or section.title,
        }
        for section in sections
    ]
    all_libraries = libraries.copy()
    detected_names = [lib["title"] for lib in libraries]

    if automatic:
        selected_libraries = [
            library["title"]
            for library in libraries
            if str(library.get("type") or "").casefold()
            in {"movie", "movies", "show", "shows", "tv"}
        ]
    else:
        unsupported = sorted(
            library["title"]
            for library in libraries
            if library["title"] in selected_libraries
            and str(library.get("type") or "").casefold()
            not in {"movie", "movies", "show", "shows", "tv"}
        )
        if unsupported:
            raise RuntimeError(
                "Configured Plex libraries use unsupported types: "
                + ", ".join(unsupported)
            )
    config["_library_discovery_auto"] = automatic

    filtered_sections = []
    filtered_libraries = []
    skipped_libraries = []
    for section, lib in zip(sections, libraries, strict=False):
        if lib['title'] in selected_libraries and str(
            lib.get("type") or ""
        ).casefold() in {"movie", "movies", "show", "shows", "tv"}:
            filtered_sections.append(section)
            filtered_libraries.append(lib)
        else:
            skipped_libraries.append(lib['title'])
    sections = filtered_sections
    libraries = filtered_libraries

    log_plex_event(
        "plex_detected_and_skipped_libraries",
        detected=", ".join(detected_names) if detected_names else "None",
        selected=", ".join(selected_libraries) if selected_libraries else "None",
        skipped=", ".join(skipped_libraries) if skipped_libraries else "None",
        selection="automatic" if automatic else "explicit",
    )
    if not sections:
        log_plex_event("plex_no_libraries_found")
        if automatic:
            raise RuntimeError("No supported Plex movie or show libraries were found")

    return sections, selected_libraries, all_libraries


def collect_plex_path_samples(sections, max_items_per_library=2):
    """Collect a bounded Plex path sample for preflight mapping advice."""
    samples = []
    for section in sections or []:
        try:
            items = list(section.search(maxresults=max_items_per_library))
        except Exception as error:
            log_plex_event(
                "plex_path_sample_library_failed",
                library_name=getattr(section, "title", "unknown"),
                error=error,
            )
            continue
        for item in items[:max_items_per_library]:
            for location in getattr(item, "locations", None) or []:
                if location:
                    samples.append(str(location))
            if hasattr(item, "iterParts"):
                try:
                    for part in list(item.iterParts())[:2]:
                        if getattr(part, "file", None):
                            samples.append(str(part.file))
                except Exception as error:
                    log_plex_event(
                        "plex_path_sample_item_failed",
                        title=getattr(item, "title", "unknown"),
                        error=error,
                    )
                    continue
    return sorted(set(samples))

_plex_cache = {}


async def plex_operation(operation, runtime=None, description="Plex operation"):
    """Run a blocking Plex operation with the configured bounded retry policy."""
    runtime = runtime or {}
    retries = max(1, int(runtime.get("plex_retries", 3)))
    retry_delay = max(0.0, float(runtime.get("plex_retry_delay", 1.0)))
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            async with runtime_slot({"runtime": runtime}, "plex"):
                return await asyncio.to_thread(operation)
        except CircuitOpenError as error:
            log_plex_event(
                "plex_circuit_open",
                description=description,
                retry_after=error.retry_after,
            )
            raise RuntimeError(
                f"{description} skipped while the Plex circuit is cooling down"
            ) from error
        except Exception as error:
            last_error = error
            log_plex_event(
                "plex_operation_failed",
                description=description,
                attempt=attempt,
                retries=retries,
                error=error,
            )
            if attempt < retries and retry_delay:
                await asyncio.sleep(retry_delay * attempt)
    raise RuntimeError(f"{description} failed after {retries} attempts") from last_error


def _supports_paged_all(section):
    """Return whether a Plex section or test double accepts paging keywords."""
    try:
        parameters = inspect.signature(section.all).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or parameter.name in {"container_start", "container_size", "maxresults"}
        for parameter in parameters
    )


def _library_total(section):
    """Read an uncached Plex library item total when the API exposes one."""
    total_view_size = getattr(section, "totalViewSize", None)
    if callable(total_view_size):
        return int(total_view_size(includeCollections=False))
    total = getattr(section, "totalSize", None)
    if callable(total):
        total = total()
    try:
        return None if total is None else int(total)
    except (TypeError, ValueError):
        return None


def plex_inventory_record(item):
    """Keep only fields needed for cross-library identity reconciliation."""
    ids = _external_ids(item)
    media_type = str(getattr(item, "type", None) or "").lower()
    if media_type in {"show", "shows"}:
        media_type = "tv"
    elif media_type == "movies":
        media_type = "movie"
    return {
        "rating_key": (
            None
            if getattr(item, "ratingKey", None) is None
            else str(item.ratingKey)
        ),
        "title": getattr(item, "title", None),
        "year": getattr(item, "year", None),
        "media_type": media_type,
        "edition": getattr(item, "editionTitle", None)
        or getattr(item, "edition", None),
        "tmdb_id": ids.get("tmdb"),
    }


async def load_plex_library_inventory(section, runtime=None, *, records_only=False):
    """Load a stable Plex inventory through automatic bounded pages.

    PlexAPI batches network calls internally, but MetaFusion historically kept
    every selected library in memory at once. This loader makes paging explicit,
    verifies a stable total and unique rating keys, and can return lightweight
    records for the cross-library discovery pass.
    """
    runtime = runtime or {}
    library_name = getattr(section, "title", "unknown")
    expected_total = await plex_operation(
        lambda: _library_total(section),
        runtime,
        description=f"Read library total for {library_name}",
    )
    page_size = (
        max(1, expected_total)
        if expected_total is not None and expected_total <= 500
        else 200
    )
    supports_paging = _supports_paged_all(section)
    offset = 0
    pages = 0
    seen_rating_keys = set()
    results = []

    while True:
        if supports_paging:
            page = await plex_operation(
                lambda start=offset: list(
                    section.all(
                        container_start=start,
                        container_size=page_size,
                        maxresults=page_size,
                    )
                ),
                runtime,
                description=(
                    f"List library {library_name} page starting at {offset}"
                ),
            )
        else:
            page = await plex_operation(
                lambda: list(section.all()),
                runtime,
                description=f"List library {library_name}",
            )
        pages += 1
        if not page:
            break

        for item in page:
            rating_key = getattr(item, "ratingKey", None)
            if rating_key is not None:
                normalized = str(rating_key)
                if normalized in seen_rating_keys:
                    raise RuntimeError(
                        f"Plex inventory for {library_name} repeated rating key "
                        f"{normalized}; cleanup requires a stable complete inventory"
                    )
                seen_rating_keys.add(normalized)
            results.append(plex_inventory_record(item) if records_only else item)

        if not supports_paging or len(page) < page_size:
            break
        offset += len(page)

    final_total = await plex_operation(
        lambda: _library_total(section),
        runtime,
        description=f"Recheck library total for {library_name}",
    )
    observed_total = len(results)
    if expected_total is not None and final_total is not None:
        if expected_total != final_total:
            raise RuntimeError(
                f"Plex inventory for {library_name} changed during paging "
                f"({expected_total} to {final_total}); retry the run"
            )
        if observed_total != expected_total:
            raise RuntimeError(
                f"Plex inventory for {library_name} was incomplete "
                f"({observed_total} of {expected_total}); cleanup is disabled"
            )

    log_plex_event(
        "plex_inventory_paged",
        library_name=library_name,
        pages=pages,
        items=observed_total,
        page_size=page_size,
    )
    return results


async def get_plex_metadata(
    item,
    _season_cache=None,
    _episode_cache=None,
    _movie_cache=None,
    _runtime_config=None,
    _plex_config=None,
):
    global _plex_cache
    title = getattr(item, "title", None)
    year = getattr(item, "year", None)
    item_key = id(item)
    library_name = "Unknown"
    library_type = (getattr(item, "type", None) or "unknown").lower()
    tmdb_id = imdb_id = tvdb_id = None
    if _season_cache is None:
        _season_cache = {}
    if _episode_cache is None:
        _episode_cache = {}
    if _movie_cache is None:
        _movie_cache = {}
    _plex_config = _plex_config or {}
    path_mappings = _plex_config.get("path_mappings", [])

    try:
        item_key = getattr(item, 'ratingKey', id(item))
        if item_key in _plex_cache:
            return _plex_cache[item_key]
    except Exception as e:
        log_plex_event("plex_failed_extract_item_id", title=title, year=year, error=e)

    try:
        library_section = getattr(item, "librarySection", None)
        library_name = getattr(library_section, "title", None) or "Unknown"
        library_type = (getattr(library_section, "type", None) or getattr(item, "type", None) or "unknown").lower()
        if library_type == "movies":
            library_type = "movie"
        if library_type == "show":
            library_type = "tv"
    except Exception as e:
        log_plex_event("plex_failed_extract_library_type", library_name=library_name, error=e)

    title_year = f"{title} ({year})" if title and year else None
    ratingKey = getattr(item, "ratingKey", None)

    try:
        ids = _external_ids(item)
        tmdb_id = ids["tmdb"]
        imdb_id = ids["imdb"]
        tvdb_id = ids["tvdb"]
    except Exception as e:
        log_plex_event("plex_failed_extract_ids", title=title, year=year, error=e)

    missing_ids = [name for name, val in [("TMDb", tmdb_id), ("IMDb", imdb_id), ("TVDb", tvdb_id)] if not val]
    found_ids = [f"{name}: {val}" for name, val in [("TMDb", tmdb_id), ("IMDb", imdb_id), ("TVDb", tvdb_id)] if val]
    if missing_ids:
        log_plex_event("plex_missing_ids", title=title, year=year, missing_ids=", ".join(missing_ids), found_ids=", ".join(found_ids) if found_ids else "None")

    movie_path = None
    movie_dir = None
    edition_title = getattr(item, "editionTitle", None) or getattr(item, "edition", None)
    if library_type == "movie" or hasattr(item, "iterParts"):
        try:
            if item_key in _movie_cache:
                parts = _movie_cache[item_key]
            else:
                parts = await plex_operation(
                    lambda: list(item.iterParts()),
                    _runtime_config,
                    description=f"Read movie parts for {title} ({year})",
                ) if hasattr(item, 'iterParts') else []
                _movie_cache[item_key] = parts
            if parts:
                part_dirs = {
                    translate_plex_path(part.file, path_mappings).parent
                    for part in parts
                    if getattr(part, "file", None)
                }
                if len(part_dirs) == 1:
                    movie_directory = next(iter(part_dirs))
                    movie_path = movie_directory.name
                    movie_dir = str(movie_directory)
                else:
                    raise ValueError(
                        "Movie parts resolve to multiple directories; refusing an "
                        "ambiguous artwork destination"
                    )
                if not edition_title:
                    edition_match = re.search(
                        r"\{edition-([^}]+)\}", str(parts[0].file), re.IGNORECASE
                    )
                    if edition_match:
                        edition_title = edition_match.group(1).strip()
        except Exception as e:
            log_plex_event("plex_failed_extract_movie_path", title=title, year=year, error=e)

    show_path = None
    show_dir = None
    season_dirs = {}
    season_path_errors = {}
    seasons = []
    episodes = []
    if library_type in ("show", "tv") or hasattr(item, "episodes"):
        try:
            if item_key in _episode_cache:
                episodes = _episode_cache[item_key]
            else:
                episodes = await plex_operation(
                    lambda: list(item.episodes()),
                    _runtime_config,
                    description=f"Read episodes for {title} ({year})",
                ) if hasattr(item, 'episodes') else []
                _episode_cache[item_key] = episodes
            discovered_season_dirs = {}
            for episode in episodes:
                season_number = getattr(
                    episode,
                    "seasonNumber",
                    getattr(episode, "parentIndex", None),
                )
                for media in getattr(episode, 'media', []):
                    for part in getattr(media, 'parts', []):
                        if not getattr(part, "file", None):
                            continue
                        file_path = translate_plex_path(part.file, path_mappings)
                        discovered_season_dirs.setdefault(season_number, set()).add(
                            file_path.parent
                        )
            for season_number, directories in discovered_season_dirs.items():
                if len(directories) == 1:
                    season_dirs[season_number] = str(next(iter(directories)))
                else:
                    season_path_errors[season_number] = (
                        "episodes resolve to multiple directories"
                    )
            locations = [
                str(translate_plex_path(location, path_mappings))
                for location in (getattr(item, "locations", None) or [])
                if location
            ]
            show_directory = _discover_show_directory(
                season_dirs.values(), locations=locations
            )
            if show_directory is not None:
                show_path = show_directory.name
                show_dir = str(show_directory)
            elif season_dirs or locations:
                raise ValueError(
                    "Seasons resolve to multiple show directories; refusing an "
                    "ambiguous artwork destination"
                )
        except Exception as e:
            log_plex_event(
                "plex_failed_extract_show_path",
                title=title,
                year=year,
                error=e,
            )
    
    seasons_episodes = None
    plex_season_artwork = {}
    plex_seasons = set()
    if library_type in ("show", "tv") or episodes:
        try:
            seasons_episodes = {}
            for episode in episodes:
                season_number = getattr(
                    episode,
                    "seasonNumber",
                    getattr(episode, "parentIndex", None),
                )
                episode_number = getattr(
                    episode,
                    "episodeNumber",
                    getattr(episode, "index", None),
                )
                if season_number is None or episode_number is None:
                    continue
                season_number = int(season_number)
                plex_seasons.add(season_number)
                seasons_episodes.setdefault(season_number, []).append(episode_number)
                season_thumb = getattr(episode, "parentThumb", None)
                if season_thumb and season_number not in plex_season_artwork:
                    plex_season_artwork[season_number] = str(season_thumb)
        except Exception as e:
            log_plex_event("plex_failed_extract_seasons_episodes", title=title, year=year, error=e)
        missing_explicit_artwork = plex_seasons - set(plex_season_artwork)
        if hasattr(item, "seasons") and (not plex_seasons or missing_explicit_artwork):
            try:
                if item_key in _season_cache:
                    seasons = _season_cache[item_key]
                else:
                    seasons = await plex_operation(
                        lambda: list(item.seasons()),
                        _runtime_config,
                        description=f"Read explicit seasons for {title} ({year})",
                    )
                    _season_cache[item_key] = seasons
                for season in seasons:
                    season_number = getattr(
                        season,
                        "index",
                        getattr(season, "seasonNumber", None),
                    )
                    if season_number is None:
                        continue
                    season_number = int(season_number)
                    plex_seasons.add(season_number)
                    season_thumb = getattr(season, "thumb", None)
                    if season_thumb:
                        plex_season_artwork.setdefault(
                            season_number,
                            str(season_thumb),
                        )
            except Exception as e:
                log_plex_event(
                    "plex_failed_extract_seasons",
                    title=title,
                    year=year,
                    error=e,
                )
            
    result = {
        "library_name": library_name,
        "library_type": library_type,
        "title": title,
        "year": year,
        "title_year": title_year,
        "ratingKey": ratingKey,
        "updatedAt": (
            getattr(item, "updatedAt", None).isoformat()
            if hasattr(getattr(item, "updatedAt", None), "isoformat")
            else (
                str(getattr(item, "updatedAt", None))
                if getattr(item, "updatedAt", None) is not None
                else None
            )
        ),
        "edition_title": edition_title,
        "plex_provider_tmdb_id": tmdb_id,
        "tmdb_id": tmdb_id,
        "identity_source": "plex_tmdb_guid" if tmdb_id else None,
        "imdb_id": imdb_id,
        "tvdb_id": tvdb_id,
        "movie_path": movie_path,
        "movie_dir": movie_dir, 
        "show_path": show_path,
        "show_dir": show_dir,
        "season_dirs": season_dirs,
        "season_path_errors": season_path_errors,
        "seasons_episodes": seasons_episodes,
        "plex_seasons": sorted(plex_seasons),
        "plex_artwork": {
            "poster": getattr(item, "thumb", None),
            "background": getattr(item, "art", None),
            "seasons": plex_season_artwork,
        },
        "episode_ordering": (
            getattr(item, "episodeOrdering", None)
            or getattr(item, "episodeOrder", None)
        ),
        "server_id": getattr(
            getattr(getattr(item, "librarySection", None), "_server", None),
            "machineIdentifier",
            None,
        ),
        "library_uuid": (
            getattr(getattr(item, "librarySection", None), "uuid", None)
            or getattr(getattr(item, "librarySection", None), "key", None)
            or library_name
        ),
    }
    critical_fields = ["title", "year", "tmdb_id"]
    if library_type in ("movie",):
        critical_fields.append("movie_path")
    if library_type in ("show", "tv"):
        critical_fields.append("show_path")

    missing_critical = [key for key in critical_fields if not result.get(key)]
    if missing_critical:
        log_plex_event("plex_critical_metadata_missing", item_key=item_key, missing_critical=", ".join(missing_critical), result=result)
    _plex_cache[item_key] = result
    return result
