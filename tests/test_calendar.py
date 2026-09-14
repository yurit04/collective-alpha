import datetime as dt

from collective_alpha.calendar import is_trading_day, previous_trading_day, trading_days


def test_trading_days_skip_weekend_and_holiday():
    days = trading_days(dt.date(2024, 7, 3), dt.date(2024, 7, 8))
    assert days == [dt.date(2024, 7, 3), dt.date(2024, 7, 5), dt.date(2024, 7, 8)]
    assert not is_trading_day(dt.date(2024, 7, 4))


def test_previous_trading_day():
    assert previous_trading_day(dt.date(2024, 7, 8)) == dt.date(2024, 7, 5)  # Monday -> Friday
    assert previous_trading_day(dt.date(2024, 7, 6)) == dt.date(2024, 7, 5)  # Saturday -> Friday
