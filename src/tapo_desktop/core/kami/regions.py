"""Kami Home regions: country -> API gateway [spec:kami.auth.region_selection, kami.auth.base_urls].

The app does not discover the region. The account's country decides which gateway is used, and a wrong choice
looks like a failed sign-in or an empty camera list. The table below is written from public country data (ISO 3166-1
alpha-2 codes and English names) grouped by continent: the Americas use the US gateway; Europe and Africa (and
Israel) the EU gateway; the rest of Asia, including the Middle East and Central Asia, and Oceania the Singapore
("sea") gateway; mainland China has its own gateway. The Asia/Europe border cases were checked against the vendor
app's own grouping once; uninhabited territories (Antarctica and similar) are not offered.
"""
from __future__ import annotations

REGIONS = ("us", "eu", "sea", "cn")

_GATEWAYS = {
    "us": "https://gw-us.xiaoyi.com",
    "eu": "https://gw-eu.xiaoyi.com",
    "sea": "https://gw-sg.xiaoyi.com",
    "cn": "https://api.xiaoyi.com",
}

# "CODE Name" per line; one block per region.
_US = """
AG Antigua and Barbuda|AI Anguilla|AR Argentina|AW Aruba|BB Barbados|BL Saint Barthelemy|BM Bermuda|BO Bolivia
BQ Bonaire, Sint Eustatius and Saba|BR Brazil|BS Bahamas|BZ Belize|CA Canada|CL Chile|CO Colombia|CR Costa Rica
CU Cuba|CW Curacao|DM Dominica|DO Dominican Republic|EC Ecuador|FK Falkland Islands|GD Grenada|GF French Guiana
GL Greenland|GP Guadeloupe|GT Guatemala|GY Guyana|HN Honduras|HT Haiti|JM Jamaica|KN Saint Kitts and Nevis
KY Cayman Islands|LC Saint Lucia|MF Saint Martin (French part)|MQ Martinique|MS Montserrat|MX Mexico|NI Nicaragua
PA Panama|PE Peru|PM Saint Pierre and Miquelon|PR Puerto Rico|PY Paraguay|SR Suriname|SV El Salvador
SX Sint Maarten (Dutch part)|TC Turks and Caicos Islands|TT Trinidad and Tobago|US United States|UY Uruguay
VC Saint Vincent and the Grenadines|VE Venezuela|VG British Virgin Islands|VI U.S. Virgin Islands
"""
_EU = """
AD Andorra|AL Albania|AO Angola|AT Austria|AX Aland Islands|BA Bosnia and Herzegovina|BE Belgium|BF Burkina Faso
BG Bulgaria|BI Burundi|BJ Benin|BW Botswana|BY Belarus|CD Congo (Democratic Republic)|CF Central African Republic
CG Congo|CH Switzerland|CI Cote d'Ivoire|CM Cameroon|CV Cabo Verde|CZ Czechia|DE Germany|DJ Djibouti|DK Denmark
DZ Algeria|EE Estonia|EG Egypt|EH Western Sahara|ER Eritrea|ES Spain|ET Ethiopia|FI Finland|FO Faroe Islands
FR France|GA Gabon|GB United Kingdom|GG Guernsey|GH Ghana|GI Gibraltar|GM Gambia|GN Guinea|GQ Equatorial Guinea
GR Greece|GW Guinea-Bissau|HR Croatia|HU Hungary|IE Ireland|IL Israel|IM Isle of Man|IS Iceland|IT Italy|JE Jersey
KE Kenya|KM Comoros|LI Liechtenstein|LR Liberia|LS Lesotho|LT Lithuania|LU Luxembourg|LV Latvia|LY Libya|MA Morocco
MC Monaco|MD Moldova|ME Montenegro|MG Madagascar|MK North Macedonia|ML Mali|MR Mauritania|MT Malta|MU Mauritius
MW Malawi|MZ Mozambique|NA Namibia|NE Niger|NG Nigeria|NL Netherlands|NO Norway|PL Poland|PT Portugal|RE Reunion
RO Romania|RS Serbia|RU Russia|RW Rwanda|SC Seychelles|SD Sudan|SE Sweden|SH Saint Helena|SI Slovenia
SJ Svalbard and Jan Mayen|SK Slovakia|SL Sierra Leone|SM San Marino|SN Senegal|SO Somalia|SS South Sudan
ST Sao Tome and Principe|SZ Eswatini|TD Chad|TG Togo|TN Tunisia|TZ Tanzania|UA Ukraine|UG Uganda|VA Vatican City
XK Kosovo|YT Mayotte|ZA South Africa|ZM Zambia|ZW Zimbabwe
"""
_SEA = """
AE United Arab Emirates|AF Afghanistan|AM Armenia|AS American Samoa|AU Australia|AZ Azerbaijan|BD Bangladesh
BH Bahrain|BN Brunei|BT Bhutan|CC Cocos (Keeling) Islands|CK Cook Islands|CX Christmas Island|CY Cyprus|FJ Fiji
FM Micronesia|GE Georgia|GU Guam|HK Hong Kong|ID Indonesia|IN India|IQ Iraq|IR Iran|JO Jordan|JP Japan
KG Kyrgyzstan|KH Cambodia|KI Kiribati|KP North Korea|KR South Korea|KW Kuwait|KZ Kazakhstan|LA Laos|LB Lebanon
LK Sri Lanka|MH Marshall Islands|MM Myanmar|MN Mongolia|MO Macao|MP Northern Mariana Islands|MV Maldives
MY Malaysia|NC New Caledonia|NF Norfolk Island|NP Nepal|NR Nauru|NU Niue|NZ New Zealand|OM Oman|PF French Polynesia
PG Papua New Guinea|PH Philippines|PK Pakistan|PN Pitcairn|PS Palestine|PW Palau|QA Qatar|SA Saudi Arabia
SB Solomon Islands|SG Singapore|SY Syria|TH Thailand|TJ Tajikistan|TK Tokelau|TL Timor-Leste|TM Turkmenistan
TO Tonga|TR Turkey|TV Tuvalu|TW Taiwan|UM U.S. Minor Outlying Islands|UZ Uzbekistan|VN Vietnam|VU Vanuatu
WF Wallis and Futuna|WS Samoa|YE Yemen
"""


def _parse(block: str, region: str) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for item in block.replace("\n", "|").split("|"):
        item = item.strip()
        if item:
            code, name = item.split(" ", 1)
            out[code] = (name, region)
    return out


_TABLE: dict[str, tuple[str, str]] = {
    **_parse(_US, "us"), **_parse(_EU, "eu"), **_parse(_SEA, "sea"), "CN": ("China", "cn"),
}


def countries() -> list[tuple[str, str]]:
    """``(code, English name)`` of every selectable country, sorted by name."""
    return sorted(((code, name) for code, (name, _) in _TABLE.items()), key=lambda item: item[1])


def region_for(country_code: str) -> str:
    """``"us"`` | ``"eu"`` | ``"sea"`` | ``"cn"`` for an ISO country code; ``ValueError`` if unknown."""
    entry = _TABLE.get((country_code or "").strip().upper())
    if entry is None:
        raise ValueError(f"unknown country code: {country_code!r}")
    return entry[1]


def gateway_url(region: str) -> str:
    """HTTPS base URL of the region's API gateway."""
    try:
        return _GATEWAYS[region]
    except KeyError:
        raise ValueError(f"unknown Kami region: {region!r}") from None
