import asyncio
import re
from plexapi.server import PlexServer
from pathlib import Path
from helper.logging import log_plex_event

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

def connect_plex_library(config, selected_libraries=None):
    if not selected_libraries:
        selected_libraries = config.get("plex_libraries") or ["Movies", "TV Shows"]
    try:
        plex = PlexServer(config["plex"]["url"], config["plex"]["token"])
        log_plex_event("plex_connected", version=plex.version)
    except Exception as e:
        log_plex_event("plex_connect_failed", error=e)
        raise RuntimeError("Unable to connect to Plex") from e

    try:
        sections = list(plex.library.sections())
    except Exception as e:
        log_plex_event("plex_libraries_retrieved_failed", error=e)
        raise RuntimeError("Unable to retrieve Plex libraries") from e

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
async def get_plex_metadata(item, _season_cache=None, _episode_cache=None, _movie_cache=None):
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
                parts = await asyncio.to_thread(lambda: list(item.iterParts())) if hasattr(item, 'iterParts') else []
                _movie_cache[item_key] = parts
            if parts:
                file_path = parts[0].file
                movie_path = Path(file_path).parent.name
                movie_dir = str(Path(file_path).parent)
                if not edition_title:
                    edition_match = re.search(r"\{edition-([^}]+)\}", str(file_path), re.IGNORECASE)
                    if edition_match:
                        edition_title = edition_match.group(1).strip()
        except Exception as e:
            log_plex_event("plex_failed_extract_movie_path", title=title, year=year, error=e)

    show_path = None
    show_dir = None
    if library_type in ("show", "tv") or hasattr(item, "episodes"):
        try:
            if item_key in _episode_cache:
                episodes = _episode_cache[item_key]
            else:
                episodes = await asyncio.to_thread(lambda: list(item.episodes())) if hasattr(item, 'episodes') else []
                _episode_cache[item_key] = episodes
            found = False
            for episode in episodes:
                for media in getattr(episode, 'media', []):
                    for part in getattr(media, 'parts', []):
                        file_path = Path(part.file)
                        show_path = file_path.parent.parent.name
                        show_dir = str(file_path.parent.parent)
                        found = True
                        break
                    if found:
                        break
                if found:
                    break
        except Exception as e:
            log_plex_event("plex_failed_extract_show_path", title=title, year=year, error=e)
    
    seasons_episodes = None
    if library_type in ("show", "tv") or hasattr(item, "seasons"):
        try:
            if item_key in _season_cache:
                seasons = _season_cache[item_key]
            else:
                seasons = await asyncio.to_thread(lambda: list(item.seasons())) if hasattr(item, 'seasons') else []
                _season_cache[item_key] = seasons

            seasons_episodes = {}
            for season in seasons:
                season_key = getattr(season, 'ratingKey', id(season))
                if season_key in _episode_cache:
                    episodes = _episode_cache[season_key]
                else:
                    episodes = await asyncio.to_thread(lambda: list(season.episodes()))
                    _episode_cache[season_key] = episodes
                episode_numbers = [ep.episodeNumber for ep in episodes]
                seasons_episodes[season.index] = episode_numbers
        except Exception as e:
            log_plex_event("plex_failed_extract_seasons_episodes", title=title, year=year, error=e)
            
    result = {
        "library_name": library_name,
        "library_type": library_type,
        "title": title,
        "year": year,
        "title_year": title_year,
        "ratingKey": ratingKey,
        "edition_title": edition_title,
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
        "tvdb_id": tvdb_id,
        "movie_path": movie_path,
        "movie_dir": movie_dir, 
        "show_path": show_path,
        "show_dir": show_dir,
        "seasons_episodes": seasons_episodes,
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
