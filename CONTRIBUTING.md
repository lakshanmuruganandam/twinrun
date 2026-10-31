# Contributing

Thanks for helping. Issues and pull requests are welcome.

## Setup

```bash
git clone https://github.com/lakshanmuruganandam/twinrun.git && cd twinrun
uv sync
```

## Before you open a pull request

```bash
uv run ruff check .                 # lint
uv run pytest -q                    # unit tests
uv run python scripts/bench.py      # detection quality must stay at >= 18/20 caught, 0/10 false alarms
```

CI runs the same steps on macOS and Linux.

## Guidelines

- One change per pull request, with a short description of what and why.
- New behavior needs a test. If it affects what TwinRun detects, add a fixture to `scripts/bench.py`.
- A false alarm on a pure refactor is a bug. Please report it with the two versions of the function.
- Never commit `.env` or `*.pem` files.
