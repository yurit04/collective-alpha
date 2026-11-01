"""Security master: a stable `security_id` for every (ticker, date) in the bars.

fetch.py      month-end reference snapshots and cached dated ticker lookups (Massive REST)
build.py      episodes -> segments -> securities (pure; asks for the lookups it still needs)
actions.py    splits/dividends mapped to securities, dividend factors per security
checks.py     coverage and regression checks
overrides.csv manual corrections (one documented row per case)

See docs/decisions/0002-security-master.md for the reasoning.
"""
