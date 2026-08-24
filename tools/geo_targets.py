"""Country -> Google geo target constant ID.

Google addresses locations by numeric geo target constant ID everywhere: the
Ads API resource name `geoTargetConstants/{id}`, and the Ads Transparency
Center's region filter. Callers -- and the model -- speak ISO country codes, so
one table translates, and it lives here rather than in either caller so the two
cannot drift apart.

For countries the ID is `2000 + the ISO 3166-1 numeric code` (US 840 -> 2840,
Israel 376 -> 2376, UK 826 -> 2826). That is a Google convention rather than a
promise, so the table is written out rather than computed: it was checked entry
by entry against ads transparency, which answers for a valid region and returns
nothing for an invalid one. Sub-country targets (cities, regions, DMAs) do NOT
follow the rule and are deliberately absent -- look those up through the API.
Full list: https://developers.google.com/google-ads/api/data/geotargets
"""
from __future__ import annotations

from typing import Any

COUNTRY_GEO_TARGET_IDS: dict[str, str] = {
    "AD": "2020", "AE": "2784", "AF": "2004", "AG": "2028", "AL": "2008",
    "AM": "2051", "AO": "2024", "AR": "2032", "AT": "2040", "AU": "2036",
    "AW": "2533", "AZ": "2031", "BA": "2070", "BB": "2052", "BD": "2050",
    "BE": "2056", "BF": "2854", "BG": "2100", "BH": "2048", "BI": "2108",
    "BJ": "2204", "BN": "2096", "BO": "2068", "BR": "2076", "BS": "2044",
    "BT": "2064", "BW": "2072", "BY": "2112", "BZ": "2084", "CA": "2124",
    "CD": "2180", "CF": "2140", "CG": "2178", "CH": "2756", "CI": "2384",
    "CL": "2152", "CM": "2120", "CN": "2156", "CO": "2170", "CR": "2188",
    "CV": "2132", "CY": "2196", "CZ": "2203", "DE": "2276", "DJ": "2262",
    "DK": "2208", "DM": "2212", "DO": "2214", "DZ": "2012", "EC": "2218",
    "EE": "2233", "EG": "2818", "ER": "2232", "ES": "2724", "ET": "2231",
    "FI": "2246", "FJ": "2242", "FM": "2583", "FR": "2250", "GA": "2266",
    "GB": "2826", "GD": "2308", "GE": "2268", "GH": "2288", "GM": "2270",
    "GN": "2324", "GQ": "2226", "GR": "2300", "GT": "2320", "GW": "2624",
    "GY": "2328", "HK": "2344", "HN": "2340", "HR": "2191", "HT": "2332",
    "HU": "2348", "ID": "2360", "IE": "2372", "IL": "2376", "IN": "2356",
    "IQ": "2368", "IS": "2352", "IT": "2380", "JM": "2388", "JO": "2400",
    "JP": "2392", "KE": "2404", "KG": "2417", "KH": "2116", "KM": "2174",
    "KN": "2659", "KR": "2410", "KW": "2414", "KZ": "2398", "LA": "2418",
    "LB": "2422", "LC": "2662", "LI": "2438", "LK": "2144", "LR": "2430",
    "LS": "2426", "LT": "2440", "LU": "2442", "LV": "2428", "LY": "2434",
    "MA": "2504", "MC": "2492", "MD": "2498", "ME": "2499", "MG": "2450",
    "MK": "2807", "ML": "2466", "MM": "2104", "MN": "2496", "MR": "2478",
    "MT": "2470", "MU": "2480", "MV": "2462", "MW": "2454", "MX": "2484",
    "MY": "2458", "MZ": "2508", "NA": "2516", "NE": "2562", "NG": "2566",
    "NI": "2558", "NL": "2528", "NO": "2578", "NP": "2524", "NZ": "2554",
    "OM": "2512", "PA": "2591", "PE": "2604", "PG": "2598", "PH": "2608",
    "PK": "2586", "PL": "2616", "PT": "2620", "PY": "2600", "QA": "2634",
    "RO": "2642", "RS": "2688", "RU": "2643", "RW": "2646", "SA": "2682",
    "SB": "2090", "SC": "2690", "SD": "2729", "SE": "2752", "SG": "2702",
    "SI": "2705", "SK": "2703", "SL": "2694", "SN": "2686", "SO": "2706",
    "SR": "2740", "SV": "2222", "SZ": "2748", "TD": "2148", "TG": "2768",
    "TH": "2764", "TJ": "2762", "TL": "2626", "TM": "2795", "TN": "2788",
    "TO": "2776", "TR": "2792", "TT": "2780", "TW": "2158", "TZ": "2834",
    "UA": "2804", "UG": "2800", "UK": "2826", "US": "2840", "UY": "2858",
    "UZ": "2860", "VC": "2670", "VE": "2862", "VN": "2704", "VU": "2548",
    "WS": "2882", "YE": "2887", "ZA": "2710", "ZM": "2894", "ZW": "2716",
}

# Display names, so a tool can answer "which regions can I ask for?" without a
# network call. Keyed the same as the IDs above; the UK alias is intentionally
# absent so the list reads as one row per country.
COUNTRY_NAMES: dict[str, str] = {
    "AD": "Andorra",
    "AE": "United Arab Emirates",
    "AF": "Afghanistan",
    "AG": "Antigua and Barbuda",
    "AL": "Albania",
    "AM": "Armenia",
    "AO": "Angola",
    "AR": "Argentina",
    "AT": "Austria",
    "AU": "Australia",
    "AW": "Aruba",
    "AZ": "Azerbaijan",
    "BA": "Bosnia and Herzegovina",
    "BB": "Barbados",
    "BD": "Bangladesh",
    "BE": "Belgium",
    "BF": "Burkina Faso",
    "BG": "Bulgaria",
    "BH": "Bahrain",
    "BI": "Burundi",
    "BJ": "Benin",
    "BN": "Brunei",
    "BO": "Bolivia",
    "BR": "Brazil",
    "BS": "Bahamas",
    "BT": "Bhutan",
    "BW": "Botswana",
    "BY": "Belarus",
    "BZ": "Belize",
    "CA": "Canada",
    "CD": "Congo (DRC)",
    "CF": "Central African Republic",
    "CG": "Congo",
    "CH": "Switzerland",
    "CI": "Cote d'Ivoire",
    "CL": "Chile",
    "CM": "Cameroon",
    "CN": "China",
    "CO": "Colombia",
    "CR": "Costa Rica",
    "CV": "Cabo Verde",
    "CY": "Cyprus",
    "CZ": "Czechia",
    "DE": "Germany",
    "DJ": "Djibouti",
    "DK": "Denmark",
    "DM": "Dominica",
    "DO": "Dominican Republic",
    "DZ": "Algeria",
    "EC": "Ecuador",
    "EE": "Estonia",
    "EG": "Egypt",
    "ER": "Eritrea",
    "ES": "Spain",
    "ET": "Ethiopia",
    "FI": "Finland",
    "FJ": "Fiji",
    "FM": "Micronesia",
    "FR": "France",
    "GA": "Gabon",
    "GB": "United Kingdom",
    "GD": "Grenada",
    "GE": "Georgia",
    "GH": "Ghana",
    "GM": "Gambia",
    "GN": "Guinea",
    "GQ": "Equatorial Guinea",
    "GR": "Greece",
    "GT": "Guatemala",
    "GW": "Guinea-Bissau",
    "GY": "Guyana",
    "HK": "Hong Kong",
    "HN": "Honduras",
    "HR": "Croatia",
    "HT": "Haiti",
    "HU": "Hungary",
    "ID": "Indonesia",
    "IE": "Ireland",
    "IL": "Israel",
    "IN": "India",
    "IQ": "Iraq",
    "IS": "Iceland",
    "IT": "Italy",
    "JM": "Jamaica",
    "JO": "Jordan",
    "JP": "Japan",
    "KE": "Kenya",
    "KG": "Kyrgyzstan",
    "KH": "Cambodia",
    "KM": "Comoros",
    "KN": "Saint Kitts and Nevis",
    "KR": "South Korea",
    "KW": "Kuwait",
    "KZ": "Kazakhstan",
    "LA": "Laos",
    "LB": "Lebanon",
    "LC": "Saint Lucia",
    "LI": "Liechtenstein",
    "LK": "Sri Lanka",
    "LR": "Liberia",
    "LS": "Lesotho",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "LV": "Latvia",
    "LY": "Libya",
    "MA": "Morocco",
    "MC": "Monaco",
    "MD": "Moldova",
    "ME": "Montenegro",
    "MG": "Madagascar",
    "MK": "North Macedonia",
    "ML": "Mali",
    "MM": "Myanmar",
    "MN": "Mongolia",
    "MR": "Mauritania",
    "MT": "Malta",
    "MU": "Mauritius",
    "MV": "Maldives",
    "MW": "Malawi",
    "MX": "Mexico",
    "MY": "Malaysia",
    "MZ": "Mozambique",
    "NA": "Namibia",
    "NE": "Niger",
    "NG": "Nigeria",
    "NI": "Nicaragua",
    "NL": "Netherlands",
    "NO": "Norway",
    "NP": "Nepal",
    "NZ": "New Zealand",
    "OM": "Oman",
    "PA": "Panama",
    "PE": "Peru",
    "PG": "Papua New Guinea",
    "PH": "Philippines",
    "PK": "Pakistan",
    "PL": "Poland",
    "PT": "Portugal",
    "PY": "Paraguay",
    "QA": "Qatar",
    "RO": "Romania",
    "RS": "Serbia",
    "RU": "Russia",
    "RW": "Rwanda",
    "SA": "Saudi Arabia",
    "SB": "Solomon Islands",
    "SC": "Seychelles",
    "SD": "Sudan",
    "SE": "Sweden",
    "SG": "Singapore",
    "SI": "Slovenia",
    "SK": "Slovakia",
    "SL": "Sierra Leone",
    "SN": "Senegal",
    "SO": "Somalia",
    "SR": "Suriname",
    "SV": "El Salvador",
    "SZ": "Eswatini",
    "TD": "Chad",
    "TG": "Togo",
    "TH": "Thailand",
    "TJ": "Tajikistan",
    "TL": "Timor-Leste",
    "TM": "Turkmenistan",
    "TN": "Tunisia",
    "TO": "Tonga",
    "TR": "Turkiye",
    "TT": "Trinidad and Tobago",
    "TW": "Taiwan",
    "TZ": "Tanzania",
    "UA": "Ukraine",
    "UG": "Uganda",
    "US": "United States",
    "UY": "Uruguay",
    "UZ": "Uzbekistan",
    "VC": "Saint Vincent and the Grenadines",
    "VE": "Venezuela",
    "VN": "Vietnam",
    "VU": "Vanuatu",
    "WS": "Samoa",
    "YE": "Yemen",
    "ZA": "South Africa",
    "ZM": "Zambia",
    "ZW": "Zimbabwe",
}


# id -> code, for reading an ID back out of a Google response. Built from the
# table above rather than typed twice. GB wins over its UK alias because the
# alias exists for input only.
_ID_TO_COUNTRY: dict[str, str] = {
    gid: code for code, gid in sorted(COUNTRY_GEO_TARGET_IDS.items())
    if code != "UK"
}


def country_for_geo_target_id(geo_id: Any) -> str:
    """Two-letter code for a numeric geo target ID, or '' when it is not a country."""
    return _ID_TO_COUNTRY.get(str(geo_id).strip(), "")


def geo_target_id(country_code: str) -> str:
    """Resolve a caller-supplied country to a numeric geo target constant ID.

    Accepts a two-letter ISO code ('IL', 'us') or a numeric ID already ('2376'),
    so callers that already hold an ID keep working. Raises ValueError naming
    what to pass instead -- Google's own answer to a bad ID is BAD_RESOURCE_ID
    with no hint about what a good one looks like.
    """
    value = str(country_code or "").strip()
    if value.isdigit():
        return value
    resolved = COUNTRY_GEO_TARGET_IDS.get(value.upper())
    if not resolved:
        raise ValueError(
            f"Unknown country_code {country_code!r}. Pass a two-letter ISO country "
            f"code such as 'US', 'IL' or 'GB', or a numeric geo target constant ID "
            f"such as '2376' for Israel."
        )
    return resolved


def country_name(country_code: str) -> str:
    """Display name for a code, or the code itself when it is not a country."""
    return COUNTRY_NAMES.get(str(country_code or "").strip().upper(), str(country_code))
