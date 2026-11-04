"""security_id -> IB contract. First by FIGI: IB looks up the listing's composite FIGI (from the
security master) directly, an exact identity. When IB does not know the FIGI, by symbol: IB is
asked for US stocks with the ticker of the trade date, and a match must agree on the company
name (and, where known, the primary exchange). Checked matches are cached; manual pins go in
contract_overrides.csv under the data root."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

from ._broker import Broker

# Massive primary exchange (MIC) -> IB primaryExchange
MIC_TO_IB = {"XNAS": "NASDAQ", "XNYS": "NYSE", "ARCX": "ARCA", "XASE": "AMEX", "BATS": "BATS",
             "XCBO": "CBOE", "XCHI": "CHX"}
OK = ("ok", "override")
SCHEMA = {"security_id": pl.String, "ticker": pl.String, "ib_symbol": pl.String, "con_id": pl.Int64,
          "ib_exchange": pl.String, "ib_name": pl.String, "name": pl.String, "figi": pl.String,
          "method": pl.String, "status": pl.String, "note": pl.String, "checked_at": pl.Datetime("us", "UTC")}
# Everything from these markers on is a description of the security, not the company's name.
CUT = (" AMERICAN DEPOSITARY", " AMERICAN DEPOSITORY", " DEPOSITARY", " DEPOSITORY", "-NON VOTING", " NON VOTING",
       " NON-VOTING", " SPONSORED", "-SPONSORED", " SPON ", "-SP ", " - SP", " ADR", "-ADR",
       " ADS", "(", ",", " EACH ", " CLASS ", "-CLASS ", " CL ", "-CL ", " COMMON STOCK", " ORDINARY SHARES",
       " SUBORDINATE", " PAR VALUE", "-NY REG", " NY REG")
STOP = {"INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED", "PLC", "SA", "NV", "AG",
        "SE", "LP", "LLC", "HOLDINGS", "HOLDING", "GROUP", "THE", "NEW", "DE", "DEL", "SOCIEDAD", "ANONIMA",
        "JOINT", "STOCK", "JSC", "PJSC", "OJSC", "SHARES", "SHS", "IN"}
ABBREV = {"INTL": "INTERNATIONAL", "TECH": "TECHNOLOGIES", "TECHNOLOGY": "TECHNOLOGIES", "MFG": "MANUFACTURING",
          "SYS": "SYSTEMS", "FINL": "FINANCIAL", "PHARMA": "PHARMACEUTICALS", "SVCS": "SERVICES",
          "AMER": "AMERICAN", "AMERICA": "AMERICAN", "NATL": "NATIONAL", "COS": "COMPANIES", "&": "AND"}


def ib_symbol(ticker: str) -> str:
    """Massive writes share classes with a dot (BRK.B), IB with a space (BRK B)."""
    return ticker.replace(".", " ")


def _tokens(name: str | None) -> list[str]:
    n = " " + (name or "").upper().replace("\u2019", "'").replace("'", "").replace(".", "") + " "
    n = re.sub(r"(?<![A-Z])([A-Z])-(?=[A-Z])", r"\1", n)          # U-HAUL -> UHAUL (keep the letter)
    cut = min((i for i in (n.find(m, 1) for m in CUT) if i > 0), default=len(n))
    words = re.sub(r"[^A-Z0-9&]+", " ", n[:cut]).split()
    return [ABBREV.get(w, w) for w in words if w not in STOP and not w.isdigit() and (len(w) > 1 or w == "&")]


def names_match(a: str | None, b: str | None) -> bool:
    """Same company? After dropping legal forms and security descriptions: the names are equal
    without spaces (Mercado Libre / MERCADOLIBRE), or the first two words agree (the second may
    be an abbreviation of the other: Charter Comm / CHARTER COMMUNICATIONS), or at least half
    the words are shared. First Solar / First Horizon and Apple / Apple Hospitality REIT do not
    match."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    if "".join(ta) == "".join(tb):
        return True
    if len(ta) >= 2 and len(tb) >= 2 and ta[0] == tb[0]:
        x, y = sorted((ta[1], tb[1]), key=len)
        if x == y or (len(x) >= 4 and y.startswith(x)):
            return True
    sa, sb = set(ta), set(tb)
    return len(sa & sb) / len(sa | sb) >= 0.5


class ContractBook:
    def __init__(self, root: Path, store=None, max_age_days: int = 30):
        self.root = Path(root)
        self.path = self.root / "contracts.parquet"
        self.overrides_path = self.root / "contract_overrides.csv"
        self._store = store
        self.max_age = timedelta(days=max_age_days)

    @property
    def store(self):
        if self._store is None:
            from massive_subspace_stream import Store

            self._store = Store()
        return self._store

    def cached(self) -> pl.DataFrame:
        """Checked contracts (a cache in an older layout counts as empty: it is only a cache)."""
        if self.path.exists():
            c = pl.read_parquet(self.path)
            if c.schema == pl.Schema(SCHEMA):
                return c
        return pl.DataFrame(schema=SCHEMA)

    def overrides(self) -> pl.DataFrame:
        if not self.overrides_path.exists():
            return pl.DataFrame(schema={"security_id": pl.String, "con_id": pl.Int64, "note": pl.String})
        return pl.read_csv(self.overrides_path, schema_overrides={"con_id": pl.Int64}, comment_prefix="#")

    def identities(self, ids: list[str], on: date) -> pl.DataFrame:
        """security_id -> ticker, name, primary exchange (MIC) on `on` (else the latest listing)."""
        m = self.store.security_master(None).filter(pl.col("security_id").is_in(ids))
        live = (pl.col("valid_from") <= on) & (pl.col("valid_to").is_null() | (pl.col("valid_to") >= on))
        m = m.with_columns(live.alias("__live")).sort("security_id", "__live", "valid_from")
        return m.group_by("security_id", maintain_order=True).last().select(
            "security_id", "ticker", "name", "primary_exchange", "composite_figi", pl.col("__live").alias("listed"))

    def resolve(self, ids: list[str], on: date, broker: Broker, refresh: bool = False) -> pl.DataFrame:
        """One row per id (SCHEMA). status: ok, override, not_found, ambiguous, name_mismatch,
        no_ticker. Only ok and override may be traded."""
        ids = sorted(set(ids))
        now = datetime.now(UTC)
        ident = {r["security_id"]: r for r in self.identities(ids, on).iter_rows(named=True)}
        cache = {r["security_id"]: r for r in self.cached().iter_rows(named=True)}
        pins = {r["security_id"]: r for r in self.overrides().iter_rows(named=True)}
        out = []
        for sid in ids:
            idr = ident.get(sid)
            ticker = idr["ticker"] if idr else None
            if sid in pins:
                p = pins[sid]
                out.append({**dict.fromkeys(SCHEMA), "security_id": sid, "ticker": ticker,
                            "ib_symbol": ib_symbol(ticker) if ticker else None, "con_id": p["con_id"],
                            "name": idr["name"] if idr else None, "method": "override", "status": "override",
                            "note": p.get("note"),
                            "checked_at": now})
                continue
            if not ticker:
                out.append({**dict.fromkeys(SCHEMA), "security_id": sid, "status": "no_ticker",
                            "note": "not in the security master", "checked_at": now})
                continue
            c = cache.get(sid)
            if (not refresh and c and c["ticker"] == ticker and c["status"] == "ok"
                    and c["checked_at"] and now - c["checked_at"] < self.max_age):
                out.append(c)
                continue
            out.append(self._check(sid, ticker, idr["name"], idr["primary_exchange"], idr["composite_figi"],
                                   idr["listed"], broker, now))
        res = pl.DataFrame(out, schema=SCHEMA)
        self._save(res.filter(pl.col("status") != "override"))
        return res

    def _check(self, sid, ticker, name, mic, figi, listed, broker: Broker, now) -> dict:
        sym = ib_symbol(ticker)
        base = {**dict.fromkeys(SCHEMA), "security_id": sid, "ticker": ticker, "ib_symbol": sym, "name": name,
                "figi": figi, "checked_at": now}
        stk = (pl.col("sec_type") == "STK") & (pl.col("currency") == "USD")
        notes = [] if listed else ["not listed on the date in the security master"]
        if figi:
            d = broker.contract_by_figi(figi).filter(stk)
            if d.height > 1:
                return {**base, "method": "figi", "status": "ambiguous", "note": f"{d.height} IB contracts for {figi}"}
            if d.height == 1:
                r = d.row(0, named=True)
                if r["symbol"] != sym:
                    notes.append(f"IB symbol {r['symbol']} (security master: {sym})")
                return {**base, "ib_symbol": r["symbol"], "con_id": r["con_id"], "ib_exchange": r["primary_exchange"],
                        "ib_name": r["long_name"], "method": "figi", "status": "ok", "note": "; ".join(notes) or None}
        base["method"] = "symbol"
        d = broker.contract_details(sym).filter(stk & (pl.col("symbol") == sym))
        if d.is_empty():
            return {**base, "status": "not_found", "note": f"IB knows neither FIGI {figi} nor a US stock {sym}"}
        want = MIC_TO_IB.get(mic or "")
        same = d.filter(pl.col("primary_exchange") == want) if want else d
        cand = same if same.height else d
        if cand.height > 1:
            return {**base, "status": "ambiguous", "note": f"{cand.height} IB contracts for {sym}"}
        r = cand.row(0, named=True)
        row = {**base, "con_id": r["con_id"], "ib_exchange": r["primary_exchange"], "ib_name": r["long_name"]}
        if not names_match(name, r["long_name"]):
            return {**row, "status": "name_mismatch", "note": f"{name!r} vs IB {r['long_name']!r}"}
        if want and r["primary_exchange"] != want:
            notes.append(f"exchange {r['primary_exchange']} (security master: {want})")
        return {**row, "status": "ok", "note": "; ".join(notes) or None}

    def _save(self, rows: pl.DataFrame) -> None:
        if rows.is_empty():
            return
        self.root.mkdir(parents=True, exist_ok=True)
        old = self.cached().filter(~pl.col("security_id").is_in(rows["security_id"].to_list()))
        tmp = self.path.with_suffix(".tmp")
        pl.concat([old, rows.select(list(SCHEMA))]).sort("security_id").write_parquet(tmp)
        tmp.replace(self.path)

    def by_con_id(self) -> dict[int, str]:
        """con_id -> security_id from checked contracts and overrides."""
        m = {r["con_id"]: r["security_id"] for r in self.cached().filter(pl.col("status") == "ok").iter_rows(named=True)}
        m.update({r["con_id"]: r["security_id"] for r in self.overrides().iter_rows(named=True)})
        return m
