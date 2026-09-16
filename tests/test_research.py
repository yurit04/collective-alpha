import datetime as dt
import random

import numpy as np
import polars as pl

from collective_alpha.research.screen import benjamini_hochberg, cluster_by_ic, ic_series

D = dt.date


def test_benjamini_hochberg_controls_discoveries():
    # all null: at q=0.10 we expect at most a tenth of a discovery, i.e. usually none
    rng = np.random.default_rng(0)
    p_null = rng.uniform(size=200)
    assert benjamini_hochberg(p_null, 0.10).sum() <= 2
    # a handful of genuinely tiny p-values are found
    p = np.concatenate([np.array([1e-8, 1e-7, 1e-6, 1e-5]), rng.uniform(size=196)])
    keep = benjamini_hochberg(p, 0.10)
    assert keep[:4].all() and keep.sum() < 20
    # a raw t of 2 (p about 0.046) is not enough in a family this size
    p_marginal = np.concatenate([np.array([0.046]), rng.uniform(size=199)])
    assert not benjamini_hochberg(p_marginal, 0.10)[0]
    assert benjamini_hochberg(np.array([]), 0.10).size == 0


def test_ic_series_recovers_a_known_relationship():
    rng = random.Random(1)
    rows = []
    for day in range(60):
        for _i in range(80):
            fwd = rng.gauss(0, 0.02)
            rows.append(
                {
                    "date": D(2024, 1, 1) + dt.timedelta(days=day),
                    "good": 0.6 * fwd + rng.gauss(0, 0.02),  # correlated
                    "noise": rng.gauss(0, 1),  # not
                    "fwd_ret_1d": fwd,
                }
            )
    df = pl.DataFrame(rows)
    good = ic_series(df, "good", "fwd_ret_1d", min_names=20)
    noise = ic_series(df, "noise", "fwd_ret_1d", min_names=20)
    assert good.height == 60 and float(good["ic"].mean()) > 0.3
    assert abs(float(noise["ic"].mean())) < 0.1
    # a thin cross-section is dropped rather than reported
    assert ic_series(df, "good", "fwd_ret_1d", min_names=200).height == 0


def test_cluster_groups_duplicates_and_separates_independents():
    n = 200
    rng = np.random.default_rng(2)
    base = rng.normal(size=n)
    wide = pl.DataFrame(
        {
            "date": [D(2024, 1, 1) + dt.timedelta(days=i) for i in range(n)],
            "a": base,
            "a_copy": base * 0.9 + rng.normal(scale=0.2, size=n),  # same bet
            "a_flipped": -base,  # same bet, opposite sign
            "independent": rng.normal(size=n),
        }
    )
    c = cluster_by_ic(wide, threshold=0.5)
    assert c["a"] == c["a_copy"] == c["a_flipped"]  # sign is ignored: still one bet
    assert c["independent"] != c["a"]
