import datetime as dt

from collective_alpha.execution.broker import PaperBroker
from collective_alpha.execution.orders import Book, Fill, Order, Position, targets_to_orders

D = dt.date


def test_position_math():
    p = Position("S", "A")
    p.apply(Fill("S", "A", 100, 10.0, D(2024, 1, 2), cost=1.0))
    p.apply(Fill("S", "A", 100, 12.0, D(2024, 1, 3)))
    assert p.qty == 200 and abs(p.avg_price - 11.0) < 1e-12 and p.realized_pnl == -1.0
    p.apply(Fill("S", "A", -150, 13.0, D(2024, 1, 4)))
    assert p.qty == 50 and abs(p.realized_pnl - (150 * 2.0 - 1.0)) < 1e-9 and abs(p.avg_price - 11.0) < 1e-12
    p.apply(Fill("S", "A", -80, 9.0, D(2024, 1, 5)))  # flips short through zero
    assert p.qty == -30 and p.avg_price == 9.0 and abs(p.realized_pnl - (299.0 + 50 * (9.0 - 11.0))) < 1e-9


def test_targets_to_orders():
    book = Book(cash=50_000.0, positions={"S1": Position("S1", "A", 100, 10.0)})
    prices = {"S1": 10.0, "S2": 20.0, "S3": 5.0}
    orders = targets_to_orders(
        {"S1": 0.005, "S2": -0.10},
        book,
        prices,
        {"S1": "A", "S2": "B"},
        D(2024, 1, 2),
        capital=100_000,
        min_notional=200,
    )
    by = {o.security_id: o for o in orders}
    assert by["S1"].qty == -50  # 100 -> 50 shares (0.5% of 100k / $10)
    assert by["S2"].qty == -500  # short 10% of 100k at $20
    # a name in the book but not in targets is closed
    book2 = Book(cash=0.0, positions={"S3": Position("S3", "C", 300, 5.0)})
    o2 = targets_to_orders({}, book2, prices, {}, D(2024, 1, 2), capital=100_000)
    assert o2[0].qty == -300
    # tiny trades are skipped
    o3 = targets_to_orders(
        {"S3": 0.001}, Book(cash=100_000), prices, {}, D(2024, 1, 2), capital=100_000, min_notional=200
    )
    assert o3 == []


def test_paper_broker_lifecycle(tmp_path):
    b = PaperBroker(tmp_path / "p.sqlite", initial_cash=100_000, commission_bps=10, slippage_bps=100)
    ids = b.submit([Order("S1", "A", 100, 10.0, D(2024, 1, 2)), Order("S2", "B", -50, 20.0, D(2024, 1, 2))])
    assert len(ids) == 2 and len(b.pending()) == 2
    fills = b.fill_pending(D(2024, 1, 3), {"S1": 10.0})  # S2 has no price -> stays pending
    assert len(fills) == 1 and abs(fills[0].price - 10.10) < 1e-12 and len(b.pending()) == 1
    book = b.book()
    assert book.positions["S1"].qty == 100
    assert abs(book.cash - (100_000 - 100 * 10.10 - 100 * 10.10 * 0.001)) < 1e-9
    m = b.mark(D(2024, 1, 3), {"S1": 11.0})
    assert abs(m["nav"] - (book.cash + 1100)) < 0.01 and m["positions"] == 1
    b.close()
    # state persists across instances
    b2 = PaperBroker(tmp_path / "p.sqlite")
    assert b2.book().positions["S1"].qty == 100 and len(b2.pending()) == 1 and len(b2.nav_history()) == 1
    assert b2.cancel_pending() == 1 and b2.pending() == []
    b2.close()
