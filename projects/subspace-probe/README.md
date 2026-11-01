# subspace-probe

Exploratory data analysis of the collective's data sources. Each source gets its own
notebook folder, so new sources can be added without crowding the project.

```
notebooks/
  massive/        Massive (formerly Polygon.io) equity data via massive-subspace-stream
src/subspace_probe/   small shared helpers (chart style, timing)
```

## Massive notebooks

| Notebook | What it shows |
|---|---|
| `00_tour.ipynb` | `Store` basics: what is stored, datasets, calendar and sessions, pandas vs polars, lazy scans |
| `01_daily_bars.ipynb` | Price adjustment (raw / splits / total return), point-in-time universe, market-wide history since 2003 |
| `02_minute_bars.ipynb` | Intraday volume and volatility patterns, regular vs extended hours, early closes, overnight vs intraday returns |
| `03_trades_quotes.ipynb` | On-demand tick data: trade sizes and conditions, bid-ask spreads, bars rebuilt from trades |
| `04_reference_fundamentals.ipynb` | Tickers, splits, dividends, IPOs, financial statements (point-in-time), short data, news |
| `05_realtime.ipynb` | The live stream service: status, today's live data, following new rows as they arrive |
| `06_identity.ipynb` | The security master: renamed and reused tickers, `security_id`, history by security, adjustment and universes that follow the company |

## Running

```bash
uv sync --all-packages
uv run jupyter lab projects/subspace-probe/notebooks
```

In VS Code or Cursor, open a notebook and pick the workspace `.venv` kernel.

Notes:
- Notebooks read data only through `massive_subspace_stream.Store` (public API).
- Outputs are committed so the notebooks read well on GitHub. Re-run them to refresh.
- Real-time data comes from the stream service (`subspace stream status`). The notebooks
  never open their own websocket, because Massive allows one connection per API key.
