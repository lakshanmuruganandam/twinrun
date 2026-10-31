# TwinRun performance log

Goal: a 50-function PR in under 3 minutes (`uv run python scripts/perf.py`). Half the edits change behavior, half are refactors.

| when | machine | functions | compared | diverged | inputs run | jobs | wall time | result |
|---|---|---|---|---|---|---|---|---|
| 2026-10-31 16:23 | arm64 27.2 · 10 cores | 50 | 50 | 25 | 7652 | 8 | **7.2s** | PASS |
| 2026-10-31 16:24 | arm64 27.2 · 10 cores | 50 | 50 | 25 | 7652 | 8 | **7.6s** | PASS |
