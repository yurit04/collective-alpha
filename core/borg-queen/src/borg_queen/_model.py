"""RiskModel: build, store and read the factor risk model; forecasts, attribution, bias tests."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from ._estimate import design, factor_cov, regressions, specific_var
from ._styles import FACTORS, STYLES, build_exposures, signals

log = logging.getLogger("borg_queen")
ENV_ROOT = "BORG_QUEEN_DATA_ROOT"
ANNUAL = 252


@dataclass(frozen=True)
class RiskSettings:
    top: int = 1000             # estimation universe: vinculum's top N companies each month end
    hl_vol: float = 84          # factor volatility half-life, trading days (~4 months)
    hl_corr: float = 252        # factor correlation half-life (~1 year)
    hl_vra: float | None = 42   # volatility regime adjustment half-life (None: off)
    hl_spec: float = 84         # specific variance half-life
    min_obs: int = 42           # residuals needed for an own specific variance
    shrink: float = 0.3         # weight of the size-decile mean in specific variance


def _day(x: Any) -> date | None:
    if x is None:
        return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return date.fromisoformat(str(x)[:10])


def default_root() -> Path:
    return Path(os.environ.get(ENV_ROOT) or "~/market_data/borg_queen").expanduser()


class RiskModel:
    """A point-in-time factor risk model over vinculum's top companies.

    Factors: market, 12 Fama-French sectors, 8 styles (see STYLES). Exposures are set at each
    month end; each day's returns are regressed on the latest month end's exposures before that
    day. Variances are daily; forecasts are annualized (x 252)."""

    def __init__(self, root: Path | str | None = None, processor=None, engine=None):
        self.root = Path(root).expanduser() if root else default_root()
        self._processor, self._engine = processor, engine
        self._tables: dict[str, pl.DataFrame] = {}
        self._cov: dict[date, np.ndarray] = {}

    # ------------------------------------------------------------------ dependencies
    @property
    def processor(self):
        if self._processor is None:
            from neural_processor import Processor

            self._processor = Processor()
        return self._processor

    @property
    def engine(self):
        if self._engine is None:
            from unimatrix_zero import Engine

            self._engine = Engine()
        return self._engine

    # ------------------------------------------------------------------ build
    def build(self, start: Any = "2011-01-01", end: Any = None, settings: RiskSettings | None = None) -> dict:
        """Estimate everything from `start` and write it under the data root (overwrites)."""
        st = settings or RiskSettings()
        t0 = time.time()
        s, e = _day(start), _day(end)
        panel = self.processor.panel(signals(), start=s, end=e, top=st.top)
        exposures = build_exposures(panel)
        log.info("exposures: %d month ends, %d rows", exposures["date"].n_unique(), exposures.height)
        rets = self.engine.returns(exposures["security_id"].unique().to_list(), s, e)
        fr, resid = regressions(exposures, rets)
        log.info("factor returns: %d days, median R2 %.3f", fr.height, fr["r2"].median() or float("nan"))
        spec = specific_var(resid, exposures, st.hl_spec, st.min_obs, st.shrink)
        self.root.mkdir(parents=True, exist_ok=True)
        for name, df in (("exposures", exposures), ("factor_returns", fr), ("residuals", resid), ("specific", spec)):
            tmp = self.root / f".{name}.parquet.tmp"
            df.write_parquet(tmp)
            tmp.replace(self.root / f"{name}.parquet")
        info = {"built_at": datetime.now(UTC).isoformat(timespec="seconds"), "start": str(s), "end": str(e) if e else None,
                "first_month": str(exposures["date"].min()), "last_month": str(exposures["date"].max()),
                "days": fr.height, "median_r2": round(float(fr["r2"].median()), 4), "factors": FACTORS,
                "settings": asdict(st), "seconds": round(time.time() - t0, 1)}
        (self.root / "build.json").write_text(json.dumps(info, indent=2))
        self._tables, self._cov = {}, {}
        return info

    # ------------------------------------------------------------------ reads
    def _table(self, name: str) -> pl.DataFrame:
        if name not in self._tables:
            path = self.root / f"{name}.parquet"
            if not path.exists():
                raise FileNotFoundError(f"no risk model at {self.root}: run RiskModel().build() (or `queen build`)")
            self._tables[name] = pl.read_parquet(path)
        return self._tables[name]

    def status(self) -> dict:
        path = self.root / "build.json"
        return json.loads(path.read_text()) if path.exists() else {"built": False, "root": str(self.root)}

    @property
    def settings(self) -> RiskSettings:
        return RiskSettings(**self.status().get("settings", {}))

    def months(self) -> list[date]:
        return sorted(self._table("exposures")["date"].unique().to_list())

    def _month(self, on: Any) -> date:
        d = _day(on)
        ms = [m for m in self.months() if m <= d]
        if not ms:
            raise ValueError(f"no exposures on or before {d}")
        return ms[-1]

    def exposures(self, on: Any = None) -> pl.DataFrame:
        """Exposures of the latest month end on or before `on` (all month ends if None):
        date, security_id, sector, issuer_market_cap, one column per style."""
        ex = self._table("exposures")
        return ex if on is None else ex.filter(pl.col("date") == self._month(on))

    def factor_returns(self, start: Any = None, end: Any = None) -> pl.DataFrame:
        """Daily factor returns: date, one column per factor, r2 (weighted), n (names)."""
        fr = self._table("factor_returns")
        if start is not None:
            fr = fr.filter(pl.col("date") >= _day(start))
        if end is not None:
            fr = fr.filter(pl.col("date") <= _day(end))
        return fr

    def residuals(self, start: Any = None, end: Any = None) -> pl.DataFrame:
        """date, security_id, ret (total return), resid (specific return)."""
        r = self._table("residuals")
        if start is not None:
            r = r.filter(pl.col("date") >= _day(start))
        if end is not None:
            r = r.filter(pl.col("date") <= _day(end))
        return r

    def factor_cov(self, on: Any, annualize: bool = False) -> pl.DataFrame:
        """Factor covariance from factor returns up to and including `on` (a factor column and
        one column per factor). Daily unless `annualize`."""
        c = self._cov_matrix(_day(on)) * (ANNUAL if annualize else 1)
        return pl.DataFrame(c, schema=FACTORS).insert_column(0, pl.Series("factor", FACTORS))

    def _cov_matrix(self, d: date) -> np.ndarray:
        if d not in self._cov:
            st = self.settings
            fr = self._table("factor_returns").filter(pl.col("date") <= d).sort("date")
            self._cov[d] = factor_cov(fr.select(FACTORS).to_numpy(), st.hl_vol, st.hl_corr, st.hl_vra)
        return self._cov[d]

    def specific_var(self, on: Any) -> pl.DataFrame:
        """Daily specific variance at the latest month end on or before `on`: date, security_id,
        spec_var (shrunk), spec_var_raw (own EWMA, null under min_obs), n_obs."""
        return self._table("specific").filter(pl.col("date") == self._month(on))

    def check(self) -> list[tuple[str, bool, str]]:
        """Forecast against realized risk on test portfolios, the market factor against SPY:
        [(name, passed, detail)]."""
        from ._checks import run_checks

        return run_checks(self, self.engine)

    # ------------------------------------------------------------------ forecasts
    def _model_at(self, m: date):
        sp = self.specific_var(m).select("security_id", "spec_var")
        # Sorted after the join (joins keep no row order): _align finds names by searchsorted.
        ex = self.exposures(m).join(sp, on="security_id", how="left").sort("security_id")
        X = design(ex["sector"].to_numpy(), ex.select(list(STYLES)).to_numpy())
        return ex["security_id"].to_numpy(), X, ex["spec_var"].fill_null(np.nan).to_numpy(), self._cov_matrix(m)

    def predict(self, weights: pl.DataFrame, on: Any = None) -> pl.DataFrame:
        """Forecast annualized volatility of portfolios. weights: security_id, weight and a `date`
        column (one forecast per date) unless `on` is given. Uses the model of the latest month end
        on or before each date. Returns date, model_date, total_vol, factor_vol, specific_vol,
        covered (weight of names in the model), uncovered (gross weight outside it)."""
        if on is not None:
            weights = weights.with_columns(pl.lit(_day(on)).alias("date"))
        rows = []
        for (d,), w in weights.group_by(["date"], maintain_order=True):
            m = self._month(d)
            ids, X, sv, F = self._model_at(m)
            x, wv, out = self._align(ids, w)
            xp = X[x].T @ wv
            fvar = float(xp @ F @ xp)
            svar = float(np.nansum(wv ** 2 * sv[x]))
            rows.append((d, m, np.sqrt((fvar + svar) * ANNUAL), np.sqrt(fvar * ANNUAL), np.sqrt(svar * ANNUAL),
                         float(wv.sum()), out))
        return pl.DataFrame(rows, schema={"date": pl.Date, "model_date": pl.Date, "total_vol": pl.Float64,
                                          "factor_vol": pl.Float64, "specific_vol": pl.Float64,
                                          "covered": pl.Float64, "uncovered": pl.Float64}, orient="row").sort("date")

    @staticmethod
    def _align(ids: np.ndarray, w: pl.DataFrame) -> tuple[np.ndarray, np.ndarray, float]:
        w = w.group_by("security_id").agg(pl.col("weight").sum())
        inside = w.filter(pl.col("security_id").is_in(ids))
        out = float(w.filter(~pl.col("security_id").is_in(ids))["weight"].abs().sum())
        pos = np.searchsorted(ids, inside["security_id"].to_numpy())
        return pos, inside["weight"].to_numpy().astype(float), out

    def contributions(self, weights: pl.DataFrame, on: Any) -> pl.DataFrame:
        """Risk decomposition of one portfolio: factor, exposure, factor_vol (annualized),
        contribution (share of total variance; factors plus "specific" sum to 1)."""
        m = self._month(on)
        ids, X, sv, F = self._model_at(m)
        x, wv, _ = self._align(ids, weights)
        xp = X[x].T @ wv
        fx = F @ xp
        svar = float(np.nansum(wv ** 2 * sv[x]))
        total = float(xp @ fx) + svar
        df = pl.DataFrame({"factor": FACTORS, "exposure": xp, "factor_vol": np.sqrt(np.diag(F) * ANNUAL),
                           "contribution": xp * fx / total})
        return pl.concat([df, pl.DataFrame({"factor": ["specific"], "exposure": [None], "factor_vol": [None],
                                            "contribution": [svar / total]},
                                           schema=df.schema)])

    # ------------------------------------------------------------------ realized
    def _held(self, weights: pl.DataFrame) -> pl.DataFrame:
        """Daily holdings: each date's weights held (no drift) on the trading days after it up to
        and including the next date. Returns date (return day), signal_date, security_id, weight."""
        dates = sorted(weights["date"].unique().to_list())
        days = self._table("factor_returns").select("date").sort("date")
        nxt = {d: (dates[i + 1] if i + 1 < len(dates) else date.max) for i, d in enumerate(dates)}
        spans = days.join_asof(pl.DataFrame({"signal_date": dates}).with_columns(pl.col("signal_date").alias("k")),
                               left_on="date", right_on="k", strategy="backward", allow_exact_matches=False)
        spans = spans.filter(pl.col("signal_date").is_not_null()).filter(
            pl.col("date") <= pl.col("signal_date").replace_strict(nxt, return_dtype=pl.Date))
        w = weights.select(pl.col("date").alias("signal_date"), "security_id", "weight")
        return spans.select("date", "signal_date").join(w, on="signal_date")

    def attribute(self, weights: pl.DataFrame) -> pl.DataFrame:
        """Daily return attribution of portfolios held between their dates (weights: date,
        security_id, weight; held without drift until the next date): date, total (realized
        return of the covered names), one column per factor (exposure x factor return),
        specific, uncovered (gross weight without a return that day)."""
        held = self._held(weights)
        ex = self._table("exposures")
        held = held.join_asof(ex.select(pl.col("date").alias("m"), "security_id").unique().sort("m"),
                              left_on="date", right_on="m", by="security_id", strategy="backward",
                              allow_exact_matches=False, check_sortedness=False)
        held = held.join(ex.rename({"date": "m"}), on=["m", "security_id"], how="left")
        held = held.join(self._table("residuals"), on=["date", "security_id"], how="left")
        sec = [pl.when(pl.col("sector") == s).then(pl.col("weight")).otherwise(0.0).alias(s)
               for s in FACTORS[1:13]]
        ok = pl.col("ret").is_not_null() & pl.col("sector").is_not_null()
        h = held.filter(ok)
        daily = h.group_by("date").agg(
            (pl.col("weight") * pl.col("ret")).sum().alias("total"),
            pl.col("weight").sum().alias("market"), *[s.sum() for s in sec],
            *[(pl.col("weight") * pl.col(s)).sum().alias(s) for s in STYLES],
            (pl.col("weight") * pl.col("resid")).sum().alias("specific"))
        miss = held.filter(~ok).group_by("date").agg(pl.col("weight").abs().sum().alias("uncovered"))
        fr = self._table("factor_returns").select("date", *FACTORS)
        d = daily.join(fr, on="date", suffix="_f")
        d = d.with_columns([(pl.col(f) * pl.col(f"{f}_f")).alias(f) for f in FACTORS]).drop([f"{f}_f" for f in FACTORS])
        return (d.join(miss, on="date", how="left").with_columns(pl.col("uncovered").fill_null(0.0))
                .select("date", "total", *FACTORS, "specific", "uncovered").sort("date"))

    def bias(self, weights: pl.DataFrame) -> pl.DataFrame:
        """Forecast against realized risk. For each date of `weights` (held without drift until the
        next date): forecast daily vol from that date's model, the portfolio's daily returns on the
        following days, z = return / forecast. Returns date (signal date), forecast_vol,
        realized_vol (both annualized; realized from that period's days), days, z_sq_mean. The
        bias statistic over any window is sqrt(mean of z^2 over its days): about 1 when right."""
        fc = self.predict(weights).select(pl.col("date").alias("signal_date"),
                                          (pl.col("total_vol") / np.sqrt(ANNUAL)).alias("sd"))
        held = self._held(weights).join(self._table("residuals").select("date", "security_id", "ret"),
                                        on=["date", "security_id"], how="left")
        pr = held.group_by("signal_date", "date").agg((pl.col("weight") * pl.col("ret").fill_null(0)).sum().alias("r"))
        pr = pr.join(fc, on="signal_date").with_columns((pl.col("r") / pl.col("sd")).alias("z"))
        return (pr.group_by("signal_date").agg(
            (pl.col("sd").first() * np.sqrt(ANNUAL)).alias("forecast_vol"),
            (pl.col("r").std() * np.sqrt(ANNUAL)).alias("realized_vol"), pl.len().alias("days"),
            (pl.col("z") ** 2).mean().alias("z_sq_mean"))
            .rename({"signal_date": "date"}).sort("date"))


def bias_stat(b: pl.DataFrame, window: int | None = None) -> pl.DataFrame | float:
    """From RiskModel.bias: the bias statistic over everything (window None) or over rolling
    windows of `window` periods (date, bias)."""
    # A date with no following days, or a zero forecast, has no usable z: leave it out.
    b = b.filter((pl.col("days") > 0) & pl.col("z_sq_mean").is_finite())
    z2 = pl.col("z_sq_mean") * pl.col("days")
    if window is None:
        return float(np.sqrt((b["z_sq_mean"] * b["days"]).sum() / b["days"].sum())) if b.height else float("nan")
    return b.sort("date").select("date", (z2.rolling_sum(window) / pl.col("days").rolling_sum(window))
                                 .sqrt().alias("bias")).drop_nulls()
