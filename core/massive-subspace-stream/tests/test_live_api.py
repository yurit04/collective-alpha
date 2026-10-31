"""Tests against the real Massive API. Skipped by default; run with: uv run pytest -m live"""

from datetime import timedelta

import pytest
from massive_subspace_stream import Ingestor, Store, load_settings
from massive_subspace_stream._dates import today_et

pytestmark = pytest.mark.live


@pytest.fixture
def live_settings(tmp_path, monkeypatch):
    monkeypatch.undo()  # restore real credentials removed by the autouse fixture
    s = load_settings(data_root=tmp_path / "data")
    if not (s.api_key and s.s3_access_key_id):
        pytest.skip("Massive credentials not configured")
    return s


def test_recent_day_bars_and_reference(live_settings):
    ing = Ingestor(live_settings)
    end = today_et()
    result = ing.flat("bars_day", end - timedelta(days=7), end)
    assert result.ok and result.done >= 1
    assert ing.rest(["exchanges"])[0].ok
    df = Store(settings=live_settings).bars("AAPL", start=end - timedelta(days=7))
    assert df.height >= 1 and (df["close"] > 0).all()
