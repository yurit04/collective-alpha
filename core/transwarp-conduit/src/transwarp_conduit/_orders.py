"""Order sheets: target weights + current shares -> whole-share orders, with per-order and
per-sheet checks. Pure functions; Conduit gathers the inputs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from pathlib import Path

import polars as pl

from ._config import Limits

TOL = 1e-6
SEND, NONE, SMALL = "send", "none", "skip: small"


@dataclass
class OrderSheet:
    """Orders for one rebalance. `orders` has one row per name (see make_orders); `issues`
    block submission; `warnings` do not."""

    sheet_id: str
    created_at: datetime
    price_date: date
    account: str
    capital: float
    net_liquidation: float
    order_type: str
    positions_hash: str
    orders: pl.DataFrame
    unmanaged: pl.DataFrame
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    status: str = "planned"            # planned / blocked / submitted

    @property
    def to_send(self) -> pl.DataFrame:
        return self.orders.filter(pl.col("status") == SEND)

    def summary(self) -> dict:
        o, s = self.orders, self.to_send
        return {"sheet_id": self.sheet_id, "status": self.status, "price_date": str(self.price_date),
                "capital": round(self.capital, 2), "orders": s.height,
                "buy_usd": round(float(s.filter(pl.col("action") == "BUY")["trade_value"].abs().sum()), 2),
                "sell_usd": round(float(s.filter(pl.col("action") == "SELL")["trade_value"].abs().sum()), 2),
                "turnover": round(float(s["trade_value"].abs().sum()) / self.capital, 4),
                "gross_after": round(float(o["post_weight"].abs().sum()), 4),
                "net_after": round(float(o["post_weight"].sum()), 4),
                "est_cost_usd": round(float(s["est_cost_usd"].sum()), 2),
                "skipped_small": int((o["status"] == SMALL).sum()),
                "blocked": int(o["status"].str.starts_with("blocked").sum()),
                "unmanaged_positions": self.unmanaged.height, "issues": self.issues, "warnings": self.warnings}

    def save(self, directory: Path) -> Path:
        d = Path(directory) / self.sheet_id
        d.mkdir(parents=True, exist_ok=True)
        self.orders.write_parquet(d / "orders.parquet")
        self.unmanaged.write_parquet(d / "unmanaged.parquet")
        meta = {k: v for k, v in asdict(self).items() if k not in ("orders", "unmanaged")}
        (d / "sheet.json").write_text(json.dumps(meta, default=str, indent=1))
        return d

    @classmethod
    def load(cls, directory: Path, sheet_id: str) -> OrderSheet:
        d = Path(directory) / sheet_id
        if not (d / "sheet.json").exists():
            raise FileNotFoundError(f"no order sheet {sheet_id}")
        meta = json.loads((d / "sheet.json").read_text())
        meta["created_at"] = datetime.fromisoformat(meta["created_at"])
        meta["price_date"] = date.fromisoformat(meta["price_date"])
        return cls(orders=pl.read_parquet(d / "orders.parquet"), unmanaged=pl.read_parquet(d / "unmanaged.parquet"),
                   **meta)


def positions_hash(positions: pl.DataFrame) -> str:
    """Fingerprint of the account's stock positions (con_id, shares)."""
    rows = sorted((int(c), float(q)) for c, q in positions.select("con_id", "shares").iter_rows() if q)
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:16]


def make_orders(targets: pl.DataFrame, held: pl.DataFrame, contracts: pl.DataFrame, market: pl.DataFrame,
                capital: float, limits: Limits, costs=None, shortable: dict | None = None,
                drop_blocked: bool = False) -> tuple[pl.DataFrame, list[str], list[str]]:
    """targets: security_id, weight (fractions of capital). held: security_id, con_id, shares
    (managed positions only; a held name without a target is sold). contracts: ContractBook rows.
    market: security_id, price, adv, vol. Returns (orders, issues, warnings).

    Orders columns: security_id, ticker, ib_symbol, con_id, ib_exchange, contract_status, price,
    adv, vol, target_weight, current_shares, target_shares, trade_shares, action, trade_value,
    current_weight, post_weight, est_cost_bps, est_cost_usd, shortable, status, note."""
    from unimatrix_zero import CostModel

    t = targets.group_by("security_id").agg(pl.col("weight").sum().alias("target_weight"))
    h = held.group_by("security_id").agg(pl.col("shares").sum().alias("current_shares"),
                                         pl.col("con_id").first().alias("held_con_id"))
    o = (t.join(h, on="security_id", how="full", coalesce=True)
         .join(contracts.select("security_id", "ticker", "ib_symbol", "con_id", "ib_exchange",
                                pl.col("status").alias("contract_status"), pl.col("note").alias("contract_note")),
               on="security_id", how="left")
         .join(market.select("security_id", "price", "adv", "vol"), on="security_id", how="left")
         .with_columns(pl.col("target_weight").fill_null(0.0), pl.col("current_shares").fill_null(0.0),
                       pl.coalesce("con_id", "held_con_id").alias("con_id")))
    price_ok = pl.col("price").is_not_null() & (pl.col("price") > 0)
    o = o.with_columns(pl.when(price_ok).then((pl.col("target_weight") * capital / pl.col("price")).round(0))
                       .otherwise(pl.col("current_shares")).alias("target_shares"))
    o = o.with_columns((pl.col("target_shares") - pl.col("current_shares")).alias("trade_shares"))
    o = o.with_columns((pl.col("trade_shares") * pl.col("price")).alias("trade_value"),
                       (pl.col("current_shares") * pl.col("price") / capital).alias("current_weight"))
    cm = replace(costs or CostModel(), aum=capital)
    o = o.with_columns((pl.col("trade_value").abs() / capital).alias("__tw"))
    o = o.with_columns((cm.unit_cost(pl.col("__tw"), 1.0) * 1e4).alias("est_cost_bps"))
    o = o.with_columns((pl.col("est_cost_bps") / 1e4 * pl.col("trade_value").abs()).fill_null(0.0).alias("est_cost_usd"))
    sh = shortable or {}
    o = o.with_columns(pl.col("con_id").replace_strict(sh, default=None, return_dtype=pl.Float64).alias("shortable"))

    L = limits
    trade = pl.col("trade_shares")
    tv = pl.col("trade_value").abs()
    closing = (pl.col("target_shares") == 0) & (pl.col("current_shares") != 0)
    new_short = pl.max_horizontal(-pl.col("target_shares"), 0) - pl.max_horizontal(-pl.col("current_shares"), 0)
    # Without a price the target and current shares are set equal (trade 0), so the price check
    # must come first: a name that should be bought, sold or closed is blocked, never skipped.
    wanted = (pl.col("target_weight") != 0) | (pl.col("current_shares") != 0)
    status = (pl.when(~price_ok & wanted).then(pl.lit("blocked: no price"))
              .when(trade == 0).then(pl.lit(NONE))
              .when(~pl.col("contract_status").is_in(["ok", "override"]).fill_null(False))
              .then(pl.lit("blocked: contract ") + pl.col("contract_status").fill_null("missing"))
              .when(~closing & ((tv < L.min_trade_usd) | (tv < L.min_trade_weight * capital))).then(pl.lit(SMALL))
              .when(tv > L.max_order_usd).then(pl.lit("blocked: order size"))
              .when(pl.col("adv").is_not_null() & (tv > L.max_participation * pl.col("adv")))
              .then(pl.lit("blocked: participation"))
              .when((new_short > 0) & pl.col("shortable").is_not_null() & (pl.col("shortable") < new_short))
              .then(pl.lit("blocked: not shortable"))
              .otherwise(pl.lit(SEND)))
    o = o.with_columns(status.alias("status"))
    note = (pl.when((new_short > 0) & pl.col("shortable").is_null() & (pl.col("status") == SEND))
            .then(pl.lit("borrow unknown")).otherwise(pl.col("contract_note")))
    o = o.with_columns(note.alias("note"))
    blocked = o.filter(pl.col("status").str.starts_with("blocked"))
    warnings: list[str] = []
    issues: list[str] = []
    if blocked.height:
        msg = "; ".join(f"{r['ticker'] or r['security_id']}: {r['status'][9:]}" for r in blocked.head(20).iter_rows(named=True))
        if drop_blocked:
            warnings.append(f"{blocked.height} order(s) dropped: {msg}")
        else:
            issues.append(f"{blocked.height} order(s) blocked: {msg}")
    sent = pl.col("status") == SEND
    o = o.with_columns(pl.when(sent).then(pl.col("trade_shares")).otherwise(0.0).alias("__done"))
    o = o.with_columns(((pl.col("current_shares") + pl.col("__done")) * pl.col("price").fill_null(0) / capital)
                       .alias("post_weight"),
                       pl.when(trade > 0).then(pl.lit("BUY")).when(trade < 0).then(pl.lit("SELL"))
                       .otherwise(None).alias("action"))
    big = o.filter(pl.col("post_weight").abs() > L.max_weight + TOL)
    if big.height:
        issues.append(f"{big.height} name(s) above max_weight {L.max_weight:.1%}: "
                      + ", ".join(f"{r['ticker']} {r['post_weight']:.2%}" for r in big.head(10).iter_rows(named=True)))
    gross, net = float(o["post_weight"].abs().sum()), float(o["post_weight"].sum())
    turn = float(o.filter(sent)["trade_value"].abs().sum()) / capital
    if gross > L.max_gross + TOL:
        issues.append(f"gross after trading {gross:.2f} > max_gross {L.max_gross:.2f}")
    if abs(net) > L.max_net + TOL:
        issues.append(f"net after trading {net:+.2f} beyond max_net {L.max_net:.2f}")
    if turn > L.max_turnover + TOL:
        issues.append(f"turnover {turn:.2f} > max_turnover {L.max_turnover:.2f}")
    unknown = o.filter(pl.col("note") == "borrow unknown").height
    if unknown:
        warnings.append(f"{unknown} short(s) with unknown borrow availability")
    cols = ["security_id", "ticker", "ib_symbol", "con_id", "ib_exchange", "contract_status", "price", "adv", "vol",
            "target_weight", "current_shares", "target_shares", "trade_shares", "action", "trade_value",
            "current_weight", "post_weight", "est_cost_bps", "est_cost_usd", "shortable", "status", "note"]
    return o.select(cols).sort(pl.col("trade_value").abs(), descending=True, nulls_last=True), issues, warnings
