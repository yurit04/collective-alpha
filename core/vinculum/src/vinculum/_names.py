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
