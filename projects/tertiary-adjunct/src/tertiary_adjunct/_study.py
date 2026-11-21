"""Study: the protocol run end to end, with every expensive step cached under _cache/
(git-ignored): signal panel -> month-end ICs -> scores -> alpha -> optimized books -> whole
shares -> backtest -> IBKR commission -> figures."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from ._book import FORMS, benchmark, constraints, ib_commission, round_to_shares, with_commission
from ._protocol import PROJECT, load, signal_dates, signals
from ._scores import month_end_ics, score_names, scores, signal_panel
from ._stats import figures


def _d(x: Any) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


@dataclass
class Result:
    form: str
    score: str
    weights: pl.DataFrame                 # rounded targets by signal date (shares, price)
    diag: pl.DataFrame                    # one row per rebalance (borg-queen Solution.info)
    daily: pl.DataFrame                   # the engine's daily frame, net after IBKR commission
    market: pl.DataFrame | None           # SPY daily total return
    commission: pl.DataFrame
    settings: dict = field(default_factory=dict)

    def figures(self, col: str = "net") -> dict:
        return figures(self.daily, self.market if self.form == "long_only" else None, col) | {
            "form": self.form, "score": self.score, **{k: self.settings[k] for k in ("lag", "costs") if k in self.settings}}


class Study:
    def __init__(self, protocol: dict | None = None, processor=None, engine=None, risk=None,
                 cache: Path | str | None = None):
        self.p = protocol or load()
        if processor is None or engine is None:
            from massive_subspace_stream import Store
            from vinculum import Vinculum

            v, store = Vinculum(), Store()
            if processor is None:
                from neural_processor import Processor

                processor = Processor(vinculum=v, store=store)
            if engine is None:
                from unimatrix_zero import Engine

                engine = Engine(vinculum=v, store=store)
        self.processor, self.engine = processor, engine
        if risk is None:
            from borg_queen import RiskModel

            risk = RiskModel(processor=processor, engine=engine)
        self.risk = risk
        self.cache = Path(cache or PROJECT / "_cache")
        self.capital = float(self.p["capital"])

    # ------------------------------------------------------------------ cache
    def _cached(self, name: str, make) -> pl.DataFrame:
        path = self.cache / f"{name}.parquet"
        if path.exists():
            return pl.read_parquet(path)
        df = make()
        path.parent.mkdir(parents=True, exist_ok=True)
        df.write_parquet(path)
        return df

    def _key(self, **kw) -> str:
        blob = json.dumps({"protocol": self.p, **kw}, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:10]

    # ------------------------------------------------------------------ dates and data
    def last_session(self) -> date:
        """The newest session with stored daily bars (SPY's)."""
        m = self.engine.market(date.today() - timedelta(days=30))
        return m["date"].max()

    def dates(self, start: Any = None, end: Any = None, kinds=None) -> list[date]:
        s = _d(start or self.p["periods"]["signals_from"])
        e = _d(end) if end else self.last_session()
        cal = self.engine.store.trading_days(s - timedelta(days=40), e + timedelta(days=40))
        cal = [d if isinstance(d, date) else d.date() for d in cal]
        kinds = tuple(kinds or [k for k in self.p["rebalance"]["days"]])
        return signal_dates(cal, s, e, kinds)

    def month_ends(self, start: Any = None, end: Any = None) -> list[date]:
        return self.dates(start, end, ("month_end",))

    def panel(self) -> pl.DataFrame:
        """Every protocol signal on every signal date from signals_from to the last session."""
        end = self.last_session()
        return self._cached(f"panel-{end}-{self._key(part='panel')}",
                            lambda: signal_panel(self.processor, self.p, self.dates(end=end)))

    def ics(self) -> pl.DataFrame:
        panel = self.panel()
        end = panel["date"].max()
        return self._cached(f"ics-{end}-{self._key(part='ics')}", lambda: month_end_ics(
            self.engine, panel, signals(self.p), self.month_ends(end=end), self.p["rebalance"]["lag"]))

    def scored(self) -> pl.DataFrame:
        panel = self.panel()
        end = panel["date"].max()
        keep = ["date", "security_id", "ticker", "sector", "issuer_market_cap"]
        return self._cached(f"scores-{end}-{self._key(part='scores')}",
                            lambda: scores(panel, self.p, self.ics()).select(*keep, *score_names(self.p)))

    def alpha(self, score: str) -> pl.DataFrame:
        from borg_queen import alpha_from_score

        a = self.p["alpha"]
        return alpha_from_score(self.scored(), score, self.risk, ic=a["ic"], horizon_days=a["horizon_days"])

    # ------------------------------------------------------------------ books
    def costs(self, **kw):
        """The protocol's CostModel: fitted spreads, impact at the capital, borrow; commission 0
        (IBKR's per-share commission is charged on the trades instead)."""
        from unimatrix_zero import CostModel

        return CostModel(**{"commission_bps": 0.0, "aum": self.capital, **kw})

    def build(self, form: str, score: str, start: Any, end: Any) -> tuple[pl.DataFrame, pl.DataFrame]:
        """Optimized target weights by signal date (unrounded) and the per-rebalance diagnostics."""
        from borg_queen import Optimizer, Portfolio

        if form not in FORMS:
            raise ValueError(f"form must be one of {FORMS}")
        s, e = _d(start), _d(end)
        key = self._key(part="build", form=form, score=score, start=s, end=e)
        wpath, dpath = self.cache / f"w-{form}-{score}-{s}-{e}-{key}.parquet", self.cache / f"d-{form}-{score}-{s}-{e}-{key}.parquet"
        if wpath.exists() and dpath.exists():
            return pl.read_parquet(wpath), pl.read_parquet(dpath)
        opt = Optimizer(self.risk, self.costs(), constraints(self.p, form))
        bm = benchmark(self.scored(), self.p["universe"]["benchmark_top"]) if form == "long_only" else None
        alpha = self.alpha(score)
        w, diag = Portfolio(opt, engine=self.engine).build(alpha, s, e, self.p["rebalance"]["lag"], benchmark=bm)
        diag = diag.select([c for c in diag.columns if diag[c].dtype != pl.Object])
        wpath.parent.mkdir(parents=True, exist_ok=True)
        w.write_parquet(wpath)
        diag.write_parquet(dpath)
        return w, diag

    def prices(self, ids: list[str], start: date, end: date) -> pl.DataFrame:
        r = self.engine.returns(ids, start - timedelta(days=15), end)
        return r.select("security_id", "date", "raw_close")

    def backtest(self, form: str, score: str, start: Any, end: Any, lag: int | None = None,
                 cost_kw: dict | None = None, rounded: bool = True) -> Result:
        """The book through the engine: whole shares at the capital (unless rounded=False), the
        protocol's costs (or CostModel overrides in cost_kw), IBKR commission."""
        s, e = _d(start), _d(end)
        lag = self.p["rebalance"]["lag"] if lag is None else lag
        w, diag = self.build(form, score, s, e)
        ids = w["security_id"].unique().to_list()
        px = self.prices(ids, s, e)
        if rounded:
            sh = self.p["shares"]
            cap = diag.select("date", (pl.col("nav") * self.capital).alias("capital"))
            mw = self.p[form]["max_weight"]
            w = round_to_shares(w, px, cap, mw, sh["min_position"], sh["min_shares_at_max"], form == "long_only")
        bt = self.engine.run(w, s, e, lag=lag, costs=self.costs(**(cost_kw or {})), market="SPY")
        c = self.p["costs"]
        comm = ib_commission(bt.trades, bt.daily, px, self.capital, c["commission_per_share"], c["commission_min"],
                             c["commission_max_pct"])
        daily = with_commission(bt.daily, comm)
        return Result(form, score, w, diag, daily, bt.market, comm,
                      {"lag": lag, "costs": cost_kw or "protocol", "rounded": rounded})


    # ------------------------------------------------------------------ one rebalance (paper trading)
    def targets(self, form: str, score: str, on: Any = None, holdings: pl.DataFrame | None = None,
                capital: float | None = None) -> tuple[pl.DataFrame, dict]:
        """The book for one signal date, as `transwarp_conduit.Conduit.plan` takes it: security_id,
        ticker, weight (whole shares at `capital`), shares, price. on: a signal date (default:
        the latest one in the panel). holdings: the current book as weights (Conduit.holdings()),
        so the optimizer trades from it; None starts from cash. Returns (targets, Solution.info)."""
        from borg_queen import Optimizer

        sc = self.scored()
        d = _d(on) if on else sc["date"].max()
        if d not in set(sc["date"].unique().to_list()):
            raise ValueError(f"{d} is not a signal date in the panel (latest: {sc['date'].max()})")
        cap = float(capital or self.capital)
        alpha = self.alpha(score).filter(pl.col("date") == d)
        r = self.engine.returns(alpha["security_id"].to_list(), d - timedelta(days=120), d)
        mk = r.filter(pl.col("date") <= d).group_by("security_id").agg(pl.all().sort_by("date").last())
        bm = None
        if form == "long_only":
            bm = benchmark(sc.filter(pl.col("date") == d), self.p["universe"]["benchmark_top"])
        opt = Optimizer(self.risk, self.costs(aum=cap), constraints(self.p, form))
        sol = opt.solve(alpha, d, holdings, mk, 1.0, benchmark=bm)
        w = sol.weights.with_columns(pl.lit(d).alias("date"))
        ids = w["security_id"].to_list() + (holdings["security_id"].to_list() if holdings is not None else [])
        px = self.prices(ids, d, d)
        sh = self.p["shares"]
        out = round_to_shares(w, px, cap, self.p[form]["max_weight"], sh["min_position"], sh["min_shares_at_max"],
                              form == "long_only")
        tick = sc.filter(pl.col("date") == d).select("security_id", "ticker")
        out = (out.join(tick, on="security_id", how="left")
               .select("date", "security_id", "ticker", "weight", "shares", "price").sort("weight", descending=True))
        info = sol.info | {"form": form, "score": score, "capital": cap, "names_after_rounding": out.height,
                           "gross_after_rounding": float(out["weight"].abs().sum())}
        return out, info


def doubled_costs(daily: pl.DataFrame) -> pl.DataFrame:
    """The daily frame with every cost charged twice: trading costs, borrow and commission."""
    extra = pl.col("cost").fill_null(0) + pl.col("borrow").fill_null(0) + pl.col("commission_frac").fill_null(0)
    return daily.with_columns((pl.col("net") - extra).alias("net"))


def selection_series(result: Result) -> pl.DataFrame:
    """The daily series a form is judged on: long-short its net return; long-only its net
    return above SPY's. Columns: date, r."""
    d = result.daily.select("date", "net")
    if result.form == "long_only":
        d = d.join(result.market.select("date", "ret"), on="date").select("date", (pl.col("net") - pl.col("ret")).alias("r"))
    else:
        d = d.rename({"net": "r"})
    return d.drop_nulls().sort("date")


def selection_figure(result: Result) -> float:
    """Net Sharpe ratio (long-short) or net information ratio against SPY (long-only)."""
    from ._stats import sharpe

    return sharpe(selection_series(result)["r"])


def development(study: Study, forms=FORMS) -> dict[tuple[str, str], Result]:
    """Every (form, score) of the protocol over the development years."""
    s, e = study.p["periods"]["start"], study.p["periods"]["dev_end"]
    return {(f, c): study.backtest(f, c, s, e) for f in forms for c in score_names(study.p)}


def choose(results: dict[tuple[str, str], Result]) -> dict[str, dict]:
    """Per form: the score with the best selection figure, its figure, the deflated Sharpe ratio
    of its series against all of the form's trials, and every trial's figure."""
    from ._stats import deflated_sharpe, sharpe

    out = {}
    for form in sorted({f for f, _ in results}):
        trials = {c: r for (f, c), r in results.items() if f == form}
        figs = {c: selection_figure(r) for c, r in trials.items()}
        best = max(figs, key=lambda c: figs[c])
        per_period = [sharpe(selection_series(r)["r"], annualize=False) for r in trials.values()]
        out[form] = {"score": best, "figure": figs[best], "trials": figs,
                     "deflated_sharpe": deflated_sharpe(selection_series(trials[best])["r"], per_period)}
    return out
