"""transwarp-conduit offline: SimBroker, a fake security master, fake market data, a fixed clock."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from transwarp_conduit import (
    Conduit,
    ConduitError,
    ConduitSettings,
    ContractBook,
    Journal,
    Limits,
    SimBroker,
    ib_symbol,
    is_paper,
    load_settings,
    make_orders,
    names_match,
)

DAY = date(2026, 11, 4)                                       # a Wednesday
MORNING = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)            # 10:00 ET
SECURITIES = [  # security_id, ticker, name, MIC, IB con_id, IB name, price, adv
    ("BBG_AAPL", "AAPL", "Apple Inc.", "XNAS", 265598, "APPLE INC", 200.0, 1e10),
    ("BBG_MSFT", "MSFT", "Microsoft Corp", "XNAS", 272093, "MICROSOFT CORP", 400.0, 8e9),
    ("BBG_BRKB", "BRK.B", "BERKSHIRE HATHAWAY Class B", "XNYS", 72063691, "BERKSHIRE HATHAWAY INC-CL B", 500.0, 3e9),
    ("BBG_XOM", "XOM", "Exxon Mobil Corporation", "XNYS", 13977, "EXXON MOBIL CORP", 100.0, 2e9),
    ("BBG_IBM", "IBM", "International Business Machines Corp", "XNYS", 8314, "INTL BUSINESS MACHINES CORP", 250.0, 1e9),
    ("BBG_THIN", "THIN", "Thin Trading Inc", "XNAS", 999001, "THIN TRADING INC", 10.0, 5e4),
]


class FakeStore:
    def security_master(self, tickers=None):
        return pl.DataFrame({"ticker": [s[1] for s in SECURITIES], "valid_from": [date(2010, 1, 1)] * len(SECURITIES),
                             "valid_to": [None] * len(SECURITIES), "security_id": [s[0] for s in SECURITIES],
                             "name": [s[2] for s in SECURITIES], "primary_exchange": [s[3] for s in SECURITIES],
                             "composite_figi": [s[0] for s in SECURITIES]},
                            schema_overrides={"valid_to": pl.Date})

    def sessions(self, start, end):
        if start.weekday() >= 5:
            return pl.DataFrame(schema={"date": pl.Date, "market_open": pl.Datetime("ns", "UTC"),
                                        "market_close": pl.Datetime("ns", "UTC")})
        return pl.DataFrame({"date": [start], "market_open": [datetime(start.year, start.month, start.day, 14, 30, tzinfo=UTC)],
                             "market_close": [datetime(start.year, start.month, start.day, 21, 0, tzinfo=UTC)]})


class FakeEngine:
    def returns(self, ids, start, end):
        rows = [{"security_id": s[0], "date": DAY - timedelta(days=1), "raw_close": s[6], "adv": s[7], "vol": 0.02}
                for s in SECURITIES if s[0] in ids]
        return pl.DataFrame(rows, schema={"security_id": pl.String, "date": pl.Date, "raw_close": pl.Float64,
                                          "adv": pl.Float64, "vol": pl.Float64})


def sim(**kw) -> SimBroker:
    contracts = {ib_symbol(s[1]): [{"con_id": s[4], "primary_exchange": {"XNAS": "NASDAQ", "XNYS": "NYSE"}[s[3]],
                                    "long_name": s[5]}] for s in SECURITIES}
    return SimBroker(contracts=contracts, **kw)


def conduit(tmp_path, broker=None, clock=MORNING, **settings) -> Conduit:
    s = ConduitSettings(root=str(tmp_path / "root"), **settings)
    return Conduit(s, broker=broker or sim(), store=FakeStore(), engine=FakeEngine(), clock=lambda: clock)


def targets(**w) -> pl.DataFrame:
    return pl.DataFrame({"security_id": [f"BBG_{k}" for k in w], "weight": list(w.values())})


PRICES = {s[4]: s[6] for s in SECURITIES}


REAL_PAIRS = [
    ("YPF Sociedad Anonima American Depositary Shares", "YPF S.A.-SPONSORED ADR"),
    ("McDonald's Corporation", "MC DONALD'S-CORP"), ("United Microelectronic Corp.", "UNITED MICROELECTRON-SP ADR"),
    ("Dillard's, Inc. Class A Common Stock", "DILLARDS INC-CL A"),
    ("PRUDENTIAL PLC ADS (REP 2 ORD SHARES)", "PRUDENTIAL PLC-ADR"), ("Mercado Libre, Inc", "MERCADOLIBRE INC"),
    ("Ternium S.A. American Depositary Shares", "TERNIUM SA-SPONSORED ADR"),
    ("Suzano S.A. American Depositary Shares (each representing one)", "SUZANO SA - SPON ADR"),
    ("Charter Comm Inc Del CL A New", "CHARTER COMMUNICATIONS INC-A"),
    ("Cloudflare, Inc. Class A common stock, par value $0.001 per share", "CLOUDFLARE INC - CLASS A"),
    ("Alibaba Group Holding Limited American Depositary Shares", "ALIBABA GROUP HOLDING-SP ADR"),
    ("Shopify Inc. Class A subordinate voting shares", "SHOPIFY INC - CLASS A"),
    ("Sea Limited American Depositary Shares, each representing one Class A", "SEA LTD-ADR"),
    ("NIO Inc. American depositary shares, each representing one", "NIO INC - ADR"),
    ("Joint Stock Company Kaspi.kz American Depositary Shares", "JSC KASPI.KZ ADR"),
    ("KE Holdings Inc. American Depositary Shares (each representing three)", "KE HOLDINGS INC-ADR"),
    ("XPeng Inc. American depositary shares, each representing two", "XPENG INC - ADR"),
    ("Haleon plc American Depositary Shares (Each representing two)", "HALEON PLC-ADR"),
    ("ASML Holding NV", "ASML HOLDING NV-NY REG SHS"), ("Ferrovial N.V. Ordinary Shares", "FERROVIAL NV"),
    ("Nebius Group N.V. Class A Ordinary Shares", "NEBIUS GROUP NV"), ("Stellantis N.V.", "STELLANTIS NV"),
    ("Elastic N.V.", "ELASTIC NV"), ("Brown-Forman Corporation Class B", "BROWN-FORMAN CORP-CLASS B"),
    ("Joint Stock Company Kaspi.kz American Depository Shares", "JSC KASPI.KZ ADR"),
    ("U-Haul Holding Company", "U-HAUL HOLDING CO-NON VOTING"),
]


# ---------------------------------------------------------------------------- settings, names
def test_names_and_symbols():
    assert names_match("Apple Inc.", "APPLE INC")
    assert names_match("International Business Machines Corp", "INTL BUSINESS MACHINES CORP")
    assert names_match("BERKSHIRE HATHAWAY Class B", "BERKSHIRE HATHAWAY INC-CL B")
    assert names_match("Taiwan Semiconductor Manufacturing Company Ltd.", "TAIWAN SEMICONDUCTOR-SP ADR")
    for a, b in REAL_PAIRS:                                   # security master vs IB, 2026-11-04
        assert names_match(a, b), (a, b)
    assert not names_match("First Solar, Inc.", "FIRST HORIZON CORP")
    assert not names_match("Apple Inc.", "APPLE HOSPITALITY REIT INC")
    assert not names_match("American Airlines Group Inc.", "AMERICAN EXPRESS CO")
    assert not names_match("Arrowhead Research Corporation", "ARROWHEAD PHARMACEUTICALS IN")   # renamed: FIGI finds it
    assert ib_symbol("BRK.B") == "BRK B" and ib_symbol("AAPL") == "AAPL"
    assert is_paper("DU1234567") and not is_paper("U1234567")


def test_load_settings(tmp_path, monkeypatch):
    cfg = tmp_path / "collective.toml"
    cfg.write_text('[transwarp_conduit]\nport = 4001\ncapital = 50000\n[transwarp_conduit.limits]\nmax_weight = 0.05\n')
    monkeypatch.setenv("TRANSWARP_CLIENT_ID", "23")
    s = load_settings(cfg, account="DU1")
    assert (s.port, s.capital, s.client_id, s.account, s.limits.max_weight) == (4001, 50000.0, 23, "DU1", 0.05)
    with pytest.raises(ConduitError):
        load_settings(cfg, password="x")


# ---------------------------------------------------------------------------- contracts
def test_contract_book(tmp_path):
    b = sim()
    b.contracts["XOM"].append({"con_id": 5, "primary_exchange": "NYSE", "long_name": "EXXON OTHER"})   # ambiguous
    b.contracts["IBM"] = [{"con_id": 8314, "primary_exchange": "NYSE", "long_name": "IBM CREDIT LLC"}]  # wrong company
    b.contracts["MSFT"] = [{"con_id": 272093, "primary_exchange": "ARCA", "long_name": "MICROSOFT CORP"}]
    del b.contracts["THIN"]
    book = ContractBook(tmp_path, FakeStore())
    r = {x["security_id"]: x for x in book.resolve([s[0] for s in SECURITIES] + ["BBG_GONE"], DAY, b).iter_rows(named=True)}
    assert r["BBG_AAPL"]["status"] == "ok" and r["BBG_AAPL"]["con_id"] == 265598
    assert r["BBG_BRKB"]["status"] == "ok" and r["BBG_BRKB"]["ib_symbol"] == "BRK B"
    assert r["BBG_MSFT"]["status"] == "ok" and "exchange ARCA" in r["BBG_MSFT"]["note"]
    assert r["BBG_XOM"]["status"] == "ambiguous"
    assert r["BBG_IBM"]["status"] == "name_mismatch"
    assert r["BBG_THIN"]["status"] == "not_found"
    assert r["BBG_GONE"]["status"] == "no_ticker"
    # cached: a second resolve does not ask the broker again
    calls = []
    b.contract_details = lambda sym: calls.append(sym) or sim().contract_details("NONE")
    again = book.resolve(["BBG_AAPL"], DAY, b)
    assert calls == [] and again["con_id"][0] == 265598
    # by FIGI: an exact identity, even when the name changed or the symbol differs
    b.figis["BBG_IBM"] = "IBM"
    b.contract_details = lambda sym: sim().contract_details("NONE")
    r = book.resolve(["BBG_IBM"], DAY, b, refresh=True).row(0, named=True)
    assert (r["method"], r["status"], r["con_id"]) == ("figi", "ok", 8314)
    # a manual pin wins
    (tmp_path / "contract_overrides.csv").write_text("security_id,con_id,note\nBBG_IBM,8314,checked by hand\n")
    assert book.resolve(["BBG_IBM"], DAY, b)["status"][0] == "override"
    assert book.by_con_id()[8314] == "BBG_IBM"


# ---------------------------------------------------------------------------- order rules
def _inputs(tmp_path, b=None):
    b = b or sim()
    contracts = ContractBook(tmp_path, FakeStore()).resolve([s[0] for s in SECURITIES], DAY, b)
    market = FakeEngine().returns([s[0] for s in SECURITIES], None, None).rename({"raw_close": "price"})
    return contracts, market


EMPTY_HELD = pl.DataFrame(schema={"security_id": pl.String, "con_id": pl.Int64, "shares": pl.Float64})


def test_make_orders_rules(tmp_path):
    contracts, market = _inputs(tmp_path)
    held = pl.DataFrame({"security_id": ["BBG_XOM", "BBG_IBM"], "con_id": [13977, 8314], "shares": [1000.0, 4.0]})
    t = targets(AAPL=0.02, MSFT=-0.02, BRKB=0.0006, IBM=0.0, THIN=0.01)   # BRKB: 1 share, $500
    lim = Limits(max_order_usd=15_000, min_trade_usd=1_000)
    o, issues, warns = make_orders(t, held, contracts, market, 1_000_000, lim, shortable={272093: 10.0})
    r = {x["security_id"]: x for x in o.iter_rows(named=True)}
    assert r["BBG_AAPL"]["target_shares"] == 100 and r["BBG_AAPL"]["status"] == "blocked: order size"   # $20k > $15k
    assert r["BBG_MSFT"]["status"] == "blocked: order size"
    assert r["BBG_BRKB"]["status"] == "skip: small" and r["BBG_BRKB"]["post_weight"] == 0
    assert r["BBG_IBM"]["status"] == "send" and r["BBG_IBM"]["trade_shares"] == -4      # closing: never too small
    assert r["BBG_XOM"]["action"] == "SELL" and r["BBG_XOM"]["trade_shares"] == -1000   # held, no target: sold
    assert r["BBG_THIN"]["status"] == "blocked: participation"                          # $10k > 10% of $50k a day
    assert issues and "blocked" in issues[0]
    o2, issues2, warns2 = make_orders(t, held, contracts, market, 1_000_000, Limits(), shortable={272093: 10.0},
                                      drop_blocked=True)
    r2 = {x["security_id"]: x for x in o2.iter_rows(named=True)}
    assert r2["BBG_MSFT"]["status"] == "blocked: not shortable"                          # 50 shares, 10 available
    assert not any("blocked" in i for i in issues2) and "dropped" in warns2[0]
    assert r2["BBG_AAPL"]["status"] == "send" and r2["BBG_AAPL"]["est_cost_bps"] > 1.0  # commission + spread
    assert r2["BBG_AAPL"]["est_cost_usd"] == pytest.approx(r2["BBG_AAPL"]["est_cost_bps"] / 1e4 * 20_000)


def test_no_price_blocks_instead_of_skipping(tmp_path):
    contracts, market = _inputs(tmp_path)
    market = market.filter(pl.col("security_id") != "BBG_AAPL")                       # AAPL has no close
    held = pl.DataFrame({"security_id": ["BBG_XOM"], "con_id": [13977], "shares": [100.0]})
    o, issues, _ = make_orders(targets(AAPL=0.02, MSFT=0.01), held, contracts, market.filter(
        pl.col("security_id") != "BBG_XOM"), 1_000_000, Limits())
    r = {x["security_id"]: x for x in o.iter_rows(named=True)}
    assert r["BBG_AAPL"]["status"] == "blocked: no price"                              # a target without a price
    assert r["BBG_XOM"]["status"] == "blocked: no price"                               # a position to close without one
    assert r["BBG_MSFT"]["status"] == "send"
    assert len(issues) == 1 and "2 order(s) blocked" in issues[0] and "no price" in issues[0]


def test_sheet_limits(tmp_path):
    contracts, market = _inputs(tmp_path)
    t = targets(AAPL=0.05, MSFT=0.04, XOM=0.03)
    _, issues, _ = make_orders(t, EMPTY_HELD, contracts, market, 1_000_000, Limits(max_order_usd=1e9))
    text = " ".join(issues)
    assert "max_weight" in text and "AAPL" in text and "net after trading" in text
    _, issues, _ = make_orders(targets(AAPL=0.02), EMPTY_HELD, contracts, market, 1_000_000,
                               Limits(max_turnover=0.01))
    assert any("turnover" in i for i in issues)


# ---------------------------------------------------------------------------- workflow
def test_plan_submit_fill_reconcile(tmp_path):
    b = sim()
    c = conduit(tmp_path, b)
    sheet = c.plan(targets(AAPL=0.02, MSFT=-0.02, XOM=0.01))
    assert sheet.status == "planned" and sheet.to_send.height == 3 and not sheet.issues
    assert sheet.capital == 1_000_000 and sheet.price_date == DAY - timedelta(days=1)
    again = c.sheet(sheet.sheet_id)                                 # saved and reloaded
    assert again.orders.equals(sheet.orders) and again.positions_hash == sheet.positions_hash
    placed = c.submit(sheet.sheet_id)
    assert placed.height == 3 and set(placed["order_type"]) == {"MOC"} and set(placed["order_ref"]) == {sheet.sheet_id}
    with pytest.raises(ConduitError, match="submitted"):
        c.submit(sheet.sheet_id)
    b.fill(PRICES)
    r = c.reconcile(sheet.sheet_id)
    assert (r["diff"] == 0).all() and (r["unfilled"] == 0).all()
    assert dict(zip(r["ticker"], r["actual"])) == {"AAPL": 100, "MSFT": -50, "XOM": 100}
    assert c.journal.fills().height == 3 and c.sync_fills().height == 3          # no duplicates
    h = c.holdings()
    assert dict(zip(h["security_id"], h["weight"].round(4))) == {"BBG_AAPL": 0.02, "BBG_MSFT": -0.02, "BBG_XOM": 0.01}
    # next rebalance: a name dropped from the targets is sold; a held name stays if still a target
    nxt = c.plan(targets(AAPL=0.02, MSFT=-0.02))
    r = {x["ticker"]: x for x in nxt.orders.iter_rows(named=True)}
    assert r["XOM"]["trade_shares"] == -100 and r["AAPL"]["status"] == "none"
    assert [e["event"] for e in c.journal.events()] == ["plan", "submitting", "submit", "fills", "reconcile", "plan"]


def test_partial_fill_shows_unfilled(tmp_path):
    b = sim()
    c = conduit(tmp_path, b)
    sheet = c.plan(targets(AAPL=0.02))
    c.submit(sheet.sheet_id)
    b.fill(PRICES, fraction=0.5)
    r = c.reconcile(sheet.sheet_id).row(0, named=True)
    assert r["filled"] == 50 and r["diff"] == 0 and r["unfilled"] == 50


def test_unmanaged_positions_left_alone(tmp_path):
    b = sim(holdings={424242: 10.0, 13977: 30.0}, marks={424242: 50.0, 13977: 100.0})
    c = conduit(tmp_path, b)
    sheet = c.plan(targets(AAPL=0.02))
    assert sheet.unmanaged["con_id"].to_list() == [13977, 424242] or set(sheet.unmanaged["con_id"]) == {13977, 424242}
    assert "XOM" not in sheet.orders["ticker"].to_list()           # XOM was never ordered here: not touched
    assert any("not managed" in w for w in sheet.warnings)


def test_submit_refusals(tmp_path):
    b = sim()
    c = conduit(tmp_path, b)
    blocked = c.plan(targets(AAPL=0.10))                            # above max_weight
    assert blocked.status == "blocked"
    with pytest.raises(ConduitError, match="blocked"):
        c.submit(blocked.sheet_id)
    s = c.plan(targets(AAPL=0.02))
    b.holdings[265598] = 5.0                                        # someone traded in between
    with pytest.raises(ConduitError, match="positions changed"):
        c.submit(s.sheet_id)
    del b.holdings[265598]
    late = conduit(tmp_path, b, clock=datetime(2026, 11, 4, 20, 50, tzinfo=UTC))    # 15:50 ET
    with pytest.raises(ConduitError, match="too late"):
        late.submit(s.sheet_id)
    tomorrow = conduit(tmp_path, b, clock=MORNING + timedelta(days=1))
    with pytest.raises(ConduitError, match="another day"):
        tomorrow.submit(s.sheet_id)
    assert c.journal.submitted().is_empty()                          # nothing was sent


def test_live_account_needs_explicit_opt_in(tmp_path):
    b = sim(account_no="U7654321")
    c = conduit(tmp_path, b)
    s = c.plan(targets(AAPL=0.02))
    with pytest.raises(ConduitError, match="live account"):
        c.submit(s.sheet_id)
    with pytest.raises(ConduitError, match="settings live"):
        c.submit(s.sheet_id, live=True)
    ok = conduit(tmp_path, b, live=True, account="U7654321")
    assert ok.submit(s.sheet_id, live=True).height == 1


def test_open_orders_block_a_new_sheet_and_cancel(tmp_path):
    b = sim()
    c = conduit(tmp_path, b)
    first = c.plan(targets(AAPL=0.02))
    c.submit(first.sheet_id)
    second = c.plan(targets(AAPL=0.02, MSFT=0.01))
    with pytest.raises(ConduitError, match="still open"):
        c.submit(second.sheet_id)
    assert c.cancel(first.sheet_id) == 1
    assert c.orders(first.sheet_id)["status"].to_list() == ["Cancelled"]
    assert c.submit(second.sheet_id).height == 2


def test_capital_setting_and_journal_dedupe(tmp_path):
    c = conduit(tmp_path, capital=100_000.0)
    s = c.plan(targets(AAPL=0.02))
    assert s.capital == 100_000 and s.orders.filter(pl.col("ticker") == "AAPL")["target_shares"][0] == 10
    j = Journal(tmp_path / "j")
    f = sim().executions()
    assert j.record_fills(f) == 0
    row = pl.DataFrame([{"exec_id": "E1", "time": MORNING, "account": "DU1", "con_id": 1, "symbol": "X", "side": "BOT",
                         "shares": 1.0, "price": 1.0, "commission": 0.0, "order_id": 1, "perm_id": 1, "order_ref": "s"}],
                       schema=f.schema)
    assert j.record_fills(row) == 1 and j.record_fills(row) == 0


def test_settings_replace_keeps_limits():
    s = replace(ConduitSettings(), port=4001)
    assert s.limits == Limits() and s.data_root().name == "transwarp_conduit"


# ---------------------------------------------------------------------------- IB adapter (fake ib_async objects)
def _fake_ib_broker(readonly=True):
    from types import SimpleNamespace as NS

    import ib_async
    from transwarp_conduit._ib import IBBroker

    stk = NS(conId=265598, symbol="AAPL", secType="STK", currency="USD", primaryExchange="NASDAQ")
    order = NS(orderId=7, permId=70, action="BUY", totalQuantity=100, orderType="MOC", orderRef="S1", account="DU1")
    trade = NS(contract=stk, order=order, orderStatus=NS(status="Submitted", filled=0, remaining=100, avgFillPrice=0,
                                                         permId=70))
    fill = NS(contract=stk, time=MORNING, commissionReport=NS(commission=0.35),
              execution=NS(execId="X1", acctNumber="DU1", side="BOT", shares=100, price=200.5, orderId=7, permId=70,
                           orderRef="S1"))

    class FakeIB:
        def accountSummary(self, account):
            return [NS(tag="NetLiquidation", value="1000000", currency="USD"),
                    NS(tag="TotalCashValue", value="250000", currency="USD"), NS(tag="Other", value="1", currency="USD")]

        def positions(self, account):
            return [NS(contract=stk, position=100.0, avgCost=199.0), NS(contract=NS(secType="OPT"), position=1, avgCost=1)]

        def reqContractDetails(self, c):
            return [NS(contract=stk, longName="APPLE INC", stockType="COMMON")]

        def reqAllOpenOrders(self):
            return [trade]

        def reqCompletedOrders(self, apiOnly):
            return []

        def reqExecutions(self, f):
            return [fill]

        def sleep(self, s):
            pass

        def isConnected(self):
            return False

    b = object.__new__(IBBroker)
    b.s, b.readonly, b._ib_mod, b.ib, b._account = ConduitSettings(), readonly, ib_async, FakeIB(), "DU1"
    return b


def test_ib_adapter_mapping():
    b = _fake_ib_broker()
    assert b.account()["net_liquidation"] == 1e6 and b.account()["cash"] == 2.5e5
    assert b.positions().to_dicts() == [{"con_id": 265598, "symbol": "AAPL", "shares": 100.0, "avg_cost": 199.0}]
    d = b.contract_details("AAPL").row(0, named=True)
    assert (d["con_id"], d["primary_exchange"], d["long_name"]) == (265598, "NASDAQ", "APPLE INC")
    o = b.orders().row(0, named=True)
    assert (o["order_ref"], o["status"], o["quantity"]) == ("S1", "Submitted", 100.0)
    f = b.executions().row(0, named=True)
    assert (f["exec_id"], f["price"], f["commission"], f["order_ref"]) == ("X1", 200.5, 0.35, "S1")
    with pytest.raises(ConduitError, match="read-only"):
        b.place([])
    with pytest.raises(ConduitError, match="read-only"):
        b.cancel_all()


@pytest.mark.live
def test_paper_gateway_read_only():
    """Needs IB Gateway (paper, port 4002) running and logged in. Reads only."""
    c = Conduit()
    st = c.status()
    assert st["paper"] and st["net_liquidation"] > 0
    with c.session() as b:
        r = c.book.resolve(["BBG000B9XRY4", "BBG000DWG505"], c.today(), b)       # AAPL, BRK.B
    assert set(r["status"]) == {"ok"}
