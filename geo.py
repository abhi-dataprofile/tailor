"""geo.py — normalize a freeform ATS location string to a country.

ATS feeds give messy locations: "Austin, TX", "Cupertino, CA", "London, UK",
"Bangalore, India", "Remote - US", "Berlin, Germany". To let users filter by
COUNTRY (and have every US state/city count as United States), we resolve each
location to a canonical country once, at crawl time, and store it on the job.

Heuristic, not perfect — but catches the overwhelming majority:
  1. an explicit country name/alias anywhere (word-bounded), longest alias first;
  2. else a comma/slash-separated token that is a US state or Canadian province code.
Returns "" when it can't tell (e.g. bare "Remote").
"""
import re

# US states — 2-letter codes + full names
_US_CODES = {"AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
             "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
             "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
             "VA","WA","WV","WI","WY","DC"}
_US_NAMES = {"alabama","alaska","arizona","arkansas","california","colorado","connecticut",
             "delaware","florida","georgia","hawaii","idaho","illinois","indiana","iowa",
             "kansas","kentucky","louisiana","maine","maryland","massachusetts","michigan",
             "minnesota","mississippi","missouri","montana","nebraska","nevada",
             "new hampshire","new jersey","new mexico","new york","north carolina",
             "north dakota","ohio","oklahoma","oregon","pennsylvania","rhode island",
             "south carolina","south dakota","tennessee","texas","utah","vermont",
             "virginia","washington","west virginia","wisconsin","wyoming",
             "district of columbia","washington dc"}
_CA_CODES = {"ON","QC","BC","AB","MB","SK","NS","NB","NL","PE","NT","YT","NU"}

# country name/alias -> canonical name (checked longest-first so "united states" beats "us")
_COUNTRY = {
    "united states of america": "United States", "united states": "United States",
    "u.s.a.": "United States", "u.s.": "United States", "usa": "United States",
    "us": "United States", "america": "United States",
    "united kingdom": "United Kingdom", "great britain": "United Kingdom",
    "england": "United Kingdom", "scotland": "United Kingdom", "wales": "United Kingdom",
    "britain": "United Kingdom", "uk": "United Kingdom",
    "canada": "Canada", "india": "India", "ireland": "Ireland",
    "germany": "Germany", "deutschland": "Germany", "france": "France",
    "spain": "Spain", "españa": "Spain", "italy": "Italy", "portugal": "Portugal",
    "netherlands": "Netherlands", "the netherlands": "Netherlands", "holland": "Netherlands",
    "belgium": "Belgium", "switzerland": "Switzerland", "austria": "Austria",
    "poland": "Poland", "sweden": "Sweden", "norway": "Norway", "denmark": "Denmark",
    "finland": "Finland", "australia": "Australia", "new zealand": "New Zealand",
    "singapore": "Singapore", "japan": "Japan", "china": "China", "hong kong": "Hong Kong",
    "south korea": "South Korea", "korea": "South Korea", "taiwan": "Taiwan",
    "israel": "Israel", "brazil": "Brazil", "brasil": "Brazil", "mexico": "Mexico",
    "argentina": "Argentina", "chile": "Chile", "colombia": "Colombia",
    "united arab emirates": "United Arab Emirates", "uae": "United Arab Emirates",
    "dubai": "United Arab Emirates", "saudi arabia": "Saudi Arabia",
    "south africa": "South Africa", "nigeria": "Nigeria", "kenya": "Kenya",
    "egypt": "Egypt", "turkey": "Turkey", "türkiye": "Turkey", "greece": "Greece",
    "czech republic": "Czech Republic", "czechia": "Czech Republic", "romania": "Romania",
    "hungary": "Hungary", "ukraine": "Ukraine", "philippines": "Philippines",
    "indonesia": "Indonesia", "vietnam": "Vietnam", "thailand": "Thailand",
    "malaysia": "Malaysia", "pakistan": "Pakistan", "bangladesh": "Bangladesh",
}
_ALIASES = sorted(_COUNTRY.keys(), key=len, reverse=True)   # longest first


def country_of(loc):
    if not loc:
        return ""
    low = loc.lower()
    for alias in _ALIASES:
        if re.search(r"(^|[^a-z])" + re.escape(alias) + r"([^a-z]|$)", low):
            return _COUNTRY[alias]
    for tok in re.split(r"[,/|;()\-–]", loc):
        t = tok.strip()
        if not t:
            continue
        if t.upper() in _US_CODES or t.lower() in _US_NAMES:
            return "United States"
        if t.upper() in _CA_CODES:
            return "Canada"
    return ""
