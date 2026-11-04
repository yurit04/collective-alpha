"""Company-name matching between Massive's reference names and the SEC's name history.

Massive abbreviates (QUALITY SYS INC, BERKSHIRE HATHAWAY INC DEL), the SEC spells out and
adds state suffixes (QUALITY SYSTEMS INC, TASER INTERNATIONAL INC). Names are compared as sets
of significant tokens: legal forms, share-class words and state tags are dropped, common
abbreviations expanded, and a token matches another if one is a prefix of the other.
"""

from __future__ import annotations

import re

import polars as pl

# Words that carry no identity: legal forms, share descriptions, state/country tags.
_STOP = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "COS", "LTD", "LIMITED", "PLC", "LLC",
    "LP", "L", "P", "SA", "S", "A", "NV", "N", "V", "AG", "SE", "AB", "AS", "ASA", "OYJ", "SPA", "BV",
    "THE", "OF", "AND", "DE", "DEL", "NEW", "OLD", "HOLDING", "HOLDINGS", "HLDGS", "HLDG", "GROUP", "GRP",
    "CLASS", "CL", "COM", "COMMON", "STOCK", "SHARES", "SHS", "SH", "ORD", "ORDINARY", "ADR", "ADS",
    "SPONSORED", "SPON", "DEPOSITARY", "DEPOSITORY", "REPSTG", "REP", "EACH", "UNSPONSORED",
    "PAR", "VALUE", "USD", "NPV", "REGISTERED", "NEW YORK", "NY", "MD", "NJ", "PA", "CA", "TX", "FL", "MN", "OH", "VA", "MA", "IL", "WA", "GA", "CT", "CAN", "BC", "ON", "IN", "B", "C", "I", "II",
    "III", "ET", "AL", "FKA", "DBA", "F", "STK", "UNIT", "UNITS", "WI",
}
# ADR and share descriptions appended to Massive names ("... Sponsored ADR (Each Representing 5 ...)").
_TAIL = re.compile(r"\b(SPONSORED|UNSPONSORED|AMERICAN DEPOSITARY|ADR|ADS|CLASS [A-Z]\b|COMMON STOCK|"
                   r"ORDINARY SHARES|COM STK|COM UNIT|COMMON SHARES|SHARES OF BENEFICIAL).*$")
# First tokens too common to identify a company on their own.
_GENERIC = {"AMERICAN", "FIRST", "UNITED", "NATIONAL", "GENERAL", "INTERNATIONAL", "NORTH", "SOUTH",
            "WESTERN", "EASTERN", "PACIFIC", "ATLANTIC", "GLOBAL", "US", "NEW", "CAPITAL", "CENTRAL",
            "SOUTHERN", "NORTHERN", "COMMUNITY", "CITIZENS", "PEOPLES", "SECURITY", "GREAT", "DOVER"}
_EXPAND = {
    "INTL": "INTERNATIONAL", "INTERNATL": "INTERNATIONAL", "SYS": "SYSTEMS", "TECH": "TECHNOLOGIES", "TECHS": "TECHNOLOGIES", "SVCS": "SERVICES", "SVC": "SERVICE",
    "MGMT": "MANAGEMENT", "MGT": "MANAGEMENT", "FINL": "FINANCIAL", "FIN": "FINANCIAL", "NATL": "NATIONAL",
    "CTR": "CENTER", "CTRS": "CENTERS", "INDS": "INDUSTRIES", "IND": "INDUSTRIES", "PHARMA": "PHARMACEUTICALS",
    "PPTYS": "PROPERTIES", "PPTY": "PROPERTY", "RLTY": "REALTY", "COMMUN": "COMMUNICATIONS",
    "COMMUNS": "COMMUNICATIONS", "ENTMT": "ENTERTAINMENT", "BANCORPORATION": "BANCORP", "BK": "BANK",
    "MTG": "MORTGAGE", "INVT": "INVESTMENT", "INVTS": "INVESTMENTS", "ELEC": "ELECTRIC", "LABS": "LABORATORIES",
    "LAB": "LABORATORIES", "MFG": "MANUFACTURING", "PRODS": "PRODUCTS", "PROD": "PRODUCTS", "RES": "RESOURCES",
    "ENGR": "ENGINEERING", "DEV": "DEVELOPMENT", "SOLUTNS": "SOLUTIONS", "ASSOC": "ASSOCIATES", "AMER": "AMERICA",
    "GEN": "GENERAL", "MED": "MEDICAL", "HLTH": "HEALTH", "HEALTHCARE": "HEALTH", "SEMICONDUCTR": "SEMICONDUCTOR",
}
_SPLIT = re.compile(r"[^A-Z0-9]+")


def tokens(name: str | None) -> tuple[str, ...]:
    """Significant tokens of a company name, in order."""
    if not name:
        return ()
    up = name.upper().replace("&", " AND ").replace("'", "")
    up = _TAIL.sub("", up) or up
    raw = _SPLIT.split(up)
    out = []
    for t in raw:
        if not t:
            continue
        t = _EXPAND.get(t, t)
        if t in _STOP or (len(t) == 1 and not t.isdigit()):
            continue
        out.append(t)
    return tuple(out)


def _tok_match(a: str, b: str) -> bool:
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 3 and long_.startswith(short)


def similarity(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    """Share of the shorter name's tokens found in the other. The first tokens must match,
    except for reordered names whose tokens all match (HILFIGER TOMMY vs TOMMY HILFIGER);
    names written as one word match their spaced form (JVSPAC vs JV SPAC)."""
    if not a or not b:
        return 0.0
    ja, jb = "".join(a), "".join(b)
    if ja == jb or (min(len(ja), len(jb)) >= 6 and (ja.startswith(jb) or jb.startswith(ja))):
        return 1.0
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    hits = sum(any(_tok_match(s, t) for t in long_) for s in short)
    score = hits / len(short)
    if not _tok_match(a[0], b[0]) and not (score == 1.0 and len(short) >= 2):
        return 0.0
    # A generic first word ("FIRST", "AMERICAN") alone proves nothing.
    if len(short) == 1 and short[0] in _GENERIC and len(long_) > 1:
        return 0.0
    return score


def names_match(a: str | None, b: str | None, threshold: float = 0.6) -> bool:
    return similarity(tokens(a), tokens(b)) >= threshold


def key(name: str | None) -> str:
    """A strict match key: significant tokens joined (used to find a CIK by name)."""
    return " ".join(tokens(name))


def key_expr(col: str) -> pl.Expr:
    return pl.col(col).map_elements(key, return_dtype=pl.String)


# Securities that are not common equity even when Massive types them CS or ADRC: warrants,
# rights, SPAC units, notes and baby bonds, trust preferreds, exchange-traded products.
NOT_EQUITY = re.compile(
    r"\b(WTS?|WARRANTS?|WT\d|RIGHTS?|RTS|UNITS?,? EACH CONSISTING|NOTES?|NTS|DEBENTURES?|DEB|"
    r"SPARQS|PERQS|TRUPS|QUIPS|TOPRS|CAP SECS|TRUST PFD|SUBORDINATED|SR NT|SENIOR NT|SENIOR NOTES?|"
    r"DUE \d|\d+(\.\d+)?\s?%|ETF|ETN|ISHARES|PROSHARES|SPDR)\b")
# Untyped securities (old snapshots rarely carry `type`) get a wider net: preferreds, funds,
# partnership and trust units, preferred depositary shares. These words also appear in real
# equity names (Energy Transfer LP Common Units, Banco Bradesco ADS each representing one
# preferred share, Senior Housing Properties Trust), so they are not used when Massive's
# type already says common stock or ADR.
NOT_EQUITY_UNTYPED = re.compile(
    r"\b(UNITS?|PFD|PREFERRED|PREF|SENIOR|FUND|INDEX|TRUST UNITS?|DEPOSITARY SHARES? EACH REPRESENTING)\b")
TYPED_EQUITY = ("CS", "ADRC", "OS", "GDR", "NYRS")


def looks_equity(name: str | None, sec_type: str | None = None) -> bool:
    """False for names that describe warrants, units, notes, baby bonds, ... With a Massive
    equity `type` only those are excluded; untyped names are also screened for preferreds,
    funds and partnership units."""
    if not name:
        return True
    up = name.upper()
    if NOT_EQUITY.search(up):
        return False
    return sec_type in TYPED_EQUITY or not NOT_EQUITY_UNTYPED.search(up)


# Depositary receipts by name. Massive types some ADSs as common stock (Novartis, HSBC, Taomee,
# iSoftStone) or leaves the type empty, and types an ADS that later converted to ordinary
# shares by what it is today (Fanhua was ADRC until 2025, CS after).
ADR_NAME = re.compile(
    r"\bADRS?\b|\bADS\b(?!-TEC)|AMERICAN DEPOSITA?R(?:Y|IES)|DEPOSITA?RY (?:SHARE|RECEIPT)|\bSPONS?(?:ORED)? ADR|\bGDRS?\b")
_WORDS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5, "SIX": 6, "SEVEN": 7, "EIGHT": 8, "NINE": 9, "TEN": 10,
          "TWENTY": 20, "THIRTY": 30, "FORTY": 40, "FIFTY": 50, "HUNDRED": 100, "HALF": 0.5, "ONE-HALF": 0.5,
          "ONE HALF": 0.5, "ONE-FOURTH": 0.25, "ONE-QUARTER": 0.25, "ONE-TENTH": 0.1}
_NUM = r"(\d+/\d+|\d*\.\d+|\d+|ONE[- ]HALF|ONE[- ](?:FOURTH|QUARTER|TENTH)|HALF|ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN|TWENTY|THIRTY|FORTY|FIFTY|HUNDRED)"
_UNIT = r"\s*(?:OF (?:AN? |ONE )?)?(?:ORD|OR\b|COM|CMSH|SH|EQ|REG|UTS|CL|CLASS|COMMON|ORDINARY|NON|PREF|SER)"
_RATIO = [
    re.compile(r"\((\d+)\)\s*(?:CLASS|ORD|COM|SH|COMMON|ORDINARY)"),                       # "five hundred (500) Class A"
    re.compile(r"(?:EACH |EA |EC )?(?:REPRESENTING|REPRESENTS|REPSTG|REPTSG|REPTSTG|REPTG|REPRSTG|REPT|REPS|REPR|REP|RP)"
               r"\.?\s*\(?" + _NUM + r"\)?" + _UNIT),                                       # "EACH REPSTG 10 ORD"
    re.compile(r"\(\s*" + _NUM + _UNIT),                                                      # "ADS (4 ORD SHS)", "(5 UTS)"
    re.compile(r"\b" + _NUM + r"\s*(?:ORD|COM|SH)[A-Z]*\b"),                                 # "ADS 5 ORD SHS"
]
# "each two representing three Ordinary Shares", "each ten (10) ADSs representing one (1) Class A",
# "4 ADS REPTGS 1 ORD": N depositary shares for M shares, so M / N per ADS.
_PAREN = r"\s*(?:\(\d+\)\s*)?"
_GROUP = [
    re.compile(r"\bEACH\s+" + _NUM + _PAREN + r"(?:ADSS?|ADRS?|AMERICAN DEPOSITARY SHARES)?\s*REPRESENT(?:ING|S)?\s+"
               + _NUM + _PAREN + _UNIT),
    re.compile(r"\b" + _NUM + r"\s+(?:ADSS?|ADRS?)\s+(?:REPRESENT(?:ING|S)?|REPTGS?|REPSTG)\s+" + _NUM + _PAREN + _UNIT),
]


# A former ADR whose latest name says it is ordinary shares now (Fanhua -> "AIFU Inc. Class A
# Ordinary Share", Piedmont Lithium -> "Common Stock") has converted.
CONVERTED = re.compile(r"\bORDINARY SHARES?\b|\bCOMMON STOCK\b|\bCOMMON SHARES?\b")


def looks_converted(name: str | None) -> bool:
    return bool(name and CONVERTED.search(name.upper()) and not looks_adr(name))


def looks_adr(name: str | None) -> bool:
    return bool(name and ADR_NAME.search(name.upper()))


def adr_ratio(name: str | None) -> float | None:
    """Ordinary shares per depositary share stated in a name ("ADS EACH REPSTG 10 ORD SHS" -> 10,
    "ADS(RP1/4 COM)" -> 0.25, "1 ADS REPTG .25 SH" -> 0.25), or None."""
    if not looks_adr(name):
        return None
    up = name.upper()
    for rx in _GROUP:
        m = rx.search(up)
        if m:
            n, k = _number(m.group(1)), _number(m.group(2))
            val = k / n if n and k else None
            if val and 1e-3 <= val <= 1e4:
                return float(val)
    for rx in _RATIO:
        m = rx.search(up)
        if not m:
            continue
        val = _number(m.group(1))
        if val and 1e-3 <= val <= 1e4:
            return float(val)
    return None


def _number(tok: str | None) -> float | None:
    tok = tok.replace(" ", "-") if tok else ""
    if "/" in tok:
        a, b = tok.split("/")
        return float(a) / float(b) if float(b) else None
    if tok.replace(".", "", 1).isdigit():
        return float(tok)
    return _WORDS.get(tok, _WORDS.get(tok.replace("-", " ")))
