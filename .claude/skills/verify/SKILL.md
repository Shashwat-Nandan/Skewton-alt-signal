---
name: verify
description: How to exercise this repo's data loaders and backtest CLIs end-to-end on the deploy host (read-only surfaces, no Kite auth needed).
---

# Verifying changes in taleb-karpathy-kite

Env: `.venv/bin/python` (the systemd units use it too). This machine IS the
deploy host — real market data lives in `data_cache/`.

## Read-only CLI surfaces (safe to drive anytime)

- **Options backtest / chain loaders**:
  `.venv/bin/python -m research.backtest --data data_cache/NIFTY_<from>_<to>.parquet`
  (a one-month chains file runs in ~2 min; look for "Loading historical
  data from …", "Seeded N daily spot samples from …" — the latter proves the
  LIVE strategy's `_load_spot_history` glob, and "Starting backtest: N ticks").
- **Pair screener / raw bhavcopy loaders** (same loader the LIVE pair runner
  uses): `python -m core.screen_pairs --output <scratch>/pair_candidates_test.csv` —
  NEVER let it write the default `data_cache/pair_candidates*.csv`
  (live-runner input). ~25 s; check `last_data_date` in the output is the
  latest session.
- **Kalman trend loaders**: `python -m research.backtest_kalman_trend --csv
  data_cache/NIFTY_5minute.parquet` (CMA-ES fit is slow — minutes).

## Do NOT drive live

- Any `run_paper*.py` / `runners/run.py` (paper/live runners, need Kite session).
- Fetch scripts hitting NSE/Kite (`fetch_*.py`) — a fresh Kite login while a
  live runner is active invalidates its token (2026-06-17 incident). Their
  logic is covered by tests/test_fetch_*.py.

## Gotchas

- Local TZ is CET; backtest logs an ERROR about market-hours gate — that's
  environmental, not a regression.
- Full test suite: `.venv/bin/python -m pytest tests/ -q` takes ~18 min.
