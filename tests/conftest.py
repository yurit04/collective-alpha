import datetime as dt
from pathlib import Path

import pytest

from collective_alpha.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_root=tmp_path / "data",
        api_key_file=tmp_path / "api.txt",
        s3_key_file=tmp_path / "s3.txt",
    )


@pytest.fixture
def a_day() -> dt.date:
    return dt.date(2024, 4, 5)
