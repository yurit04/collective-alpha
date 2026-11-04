"""Factor exposures: the market, 12 sectors and 8 styles, standardized per date."""

from __future__ import annotations

import polars as pl
from neural_processor import SECTORS

# style -> neural-processor signals averaged into it (after standardizing each). Raw signs, not
# signal directions: an exposure says what a stock is (big, volatile, cheap), not what to buy.
STYLES: dict[str, tuple[str, ...]] = {
    "size": ("size",),
    "value": ("book_to_market", "earnings_yield", "cash_flow_yield"),
    "momentum": ("momentum_12_1",),
    "volatility": ("volatility_3m",),
    "beta": ("beta_12m",),
    "profitability": ("gross_profitability", "roe"),
    "leverage": ("liabilities_to_assets",),
    "liquidity": ("turnover",),
}
LOG_SIGNALS = {"volatility_3m", "turnover"}   # positive and skewed: standardized in logs
WINSOR = 0.01                                  # per-date tails clipped before standardizing
CLIP = 3.0                                     # final exposures clipped to +-3

MARKET = "market"
FACTORS: list[str] = [MARKET, *SECTORS, *STYLES]


def signals() -> list[str]:
    """The neural-processor signals the styles need."""
    return sorted({s for comps in STYLES.values() for s in comps})


def _standardize(col: str, cap: str = "issuer_market_cap") -> pl.Expr:
    """Per date: clip the tails, then (x - cap-weighted mean) / equal-weighted std. Nulls stay
    null. The cap-weighted mean of 0 makes the cap-weighted universe style-neutral."""
    x = pl.col(col)
    x = x.clip(x.quantile(WINSOR).over("date"), x.quantile(1 - WINSOR).over("date"))
    w = pl.when(x.is_not_null()).then(pl.col(cap)).otherwise(None)
    mean = (x * w).sum().over("date") / w.sum().over("date")
    return ((x - mean) / x.std().over("date")).alias(col)


def build_exposures(panel: pl.DataFrame) -> pl.DataFrame:
    """panel: date, security_id, sector, issuer_market_cap and the signals in `signals()`.
    Returns date, security_id, sector, issuer_market_cap, one column per style. A missing
    style is 0 (the cap-weighted average); a missing sector is "Other"."""
    need = signals()
    missing = [c for c in need if c not in panel.columns]
    if missing:
        raise ValueError(f"panel lacks signals {missing}")
    df = panel.filter(pl.col("issuer_market_cap") > 0).select(
        "date", "security_id", pl.col("sector").fill_null("Other"), "issuer_market_cap",
        *[(pl.col(c).log() if c in LOG_SIGNALS else pl.col(c)).cast(pl.Float64).alias(c) for c in need])
    df = df.with_columns([pl.when(pl.col(c).is_finite()).then(pl.col(c)).otherwise(None).alias(c) for c in need])
    df = df.with_columns([_standardize(c) for c in need])
    df = df.with_columns([pl.mean_horizontal(*comps).alias(style) for style, comps in STYLES.items()])
    df = df.with_columns([_standardize(s) for s in STYLES])
    df = df.with_columns([pl.col(s).fill_null(0.0).clip(-CLIP, CLIP).alias(s) for s in STYLES])
    # Re-centre after clipping so the cap-weighted universe stays exactly style-neutral.
    df = df.with_columns([(pl.col(s) - (pl.col(s) * pl.col("issuer_market_cap")).sum().over("date")
                           / pl.col("issuer_market_cap").sum().over("date")).alias(s) for s in STYLES])
    df = df.with_columns(pl.when(pl.col("sector").is_in(SECTORS)).then(pl.col("sector"))
                         .otherwise(pl.lit("Other")).alias("sector"))
    return df.select("date", "security_id", "sector", "issuer_market_cap", *STYLES).sort("date", "security_id")
