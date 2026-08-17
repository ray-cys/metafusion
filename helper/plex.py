import asyncio
import re
import time
from pathlib import Path

from plexapi.server import PlexServer

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
            )
            if attempt < retries and retry_delay:
                time.sleep(retry_delay * attempt)
    raise RuntimeError("Unable to connect to Plex") from last_error


def connect_plex_library(config, selected_libraries=None, plex=None):
    if not selected_libraries:
        selected_libraries = config.get("plex_libraries") or ["Movies", "TV Shows"]
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
            )
            if attempt < retries and retry_delay:
                time.sleep(retry_delay * attempt)
    else:
        raise RuntimeError("Unable to retrieve Plex libraries") from last_error

    libraries = [{"title": section.title, "type": section.TYPE} for section in sections]
    all_libraries = libraries.copy()
    detected_names = [lib["title"] for lib in libraries]

    filtered_sections = []
    filtered_libraries = []
    skipped_libraries = []
    for section, lib in zip(sections, libraries):
        if lib['title'] in selected_libraries:
            filtered_sections.append(section)
            filtered_libraries.append(lib)
        else:
            skipped_libraries.append(lib['title'])
    sections = filtered_sections
    libraries = filtered_libraries

    log_plex_event(
        "plex_detected_and_skipped_libraries",
        detected=", ".join(detected_names) if detected_names else "None",
        skipped=", ".join(skipped_libraries) if skipped_libraries else "None"
    )
    if not sections:
        log_plex_event("plex_no_libraries_found")

    return sections, selected_libraries, all_libraries

_plex_cache = {}


async def plex_operation(operation, runtime=None, description="Plex operation"):
    """Run a blocking Plex operation with the configured bounded retry policy."""
    runtime = runtime or {}
    retries = max(1, int(runtime.get("plex_retries", 3)))
    retry_delay = max(0.0, float(runtime.get("plex_retry_delay", 1.0)))
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return await asyncio.to_thread(operation)
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
        for guid in getattr(item, "guids", []):
            if guid.id.startswith("tmdb://"):
                tmdb_id = guid.id.split("://")[1].split("?")[0]
            elif guid.id.startswith("imdb://"):
                imdb_id = guid.id.split("://")[1].split("?")[0]
            elif guid.id.startswith("tvdb://"):
                tvdb_id = guid.id.split("://")[1].split("?")[0]
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
            show_directories = {
                Path(directory).parent for directory in season_dirs.values()
            }
            if len(show_directories) == 1:
                show_directory = next(iter(show_directories))
                show_path = show_directory.name
                show_dir = str(show_directory)
            elif show_directories:
                raise ValueError(
                    "Seasons resolve to multiple show directories; refusing an "
                    "ambiguous artwork destination"
                )
        except Exception as e:
            log_plex_event("plex_failed_extract_show_path", title=title, year=year, error=e)
    
    seasons_episodes = None
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
                seasons_episodes.setdefault(season_number, []).append(episode_number)
        except Exception as e:
            log_plex_event("plex_failed_extract_seasons_episodes", title=title, year=year, error=e)
            
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
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
        "tvdb_id": tvdb_id,
        "movie_path": movie_path,
        "movie_dir": movie_dir, 
        "show_path": show_path,
        "show_dir": show_dir,
        "season_dirs": season_dirs,
        "season_path_errors": season_path_errors,
        "seasons_episodes": seasons_episodes,
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
