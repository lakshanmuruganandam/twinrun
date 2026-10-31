# TwinRun — Behavior Diff for pull requests

[![CI](https://github.com/lakshanmuruganandam/twinrun/actions/workflows/ci.yml/badge.svg)](https://github.com/lakshanmuruganandam/twinrun/actions/workflows/ci.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue) ![License: MIT](https://img.shields.io/badge/license-MIT-green)

A code diff shows what text changed. **TwinRun shows what the code now *does* differently.**

On every pull request, TwinRun finds the Python functions the PR touched, generates inputs
for them, runs **both versions** (merge-base and head) on the **same inputs** inside a
sandbox, and posts a **Behavior Diff** check with the smallest input that makes them disagree:

| function | divergence | smallest input | base output | head output |
|---|---|---|---|---|
| `shipping_cost` | value change ×9 | `shipping_cost(40.0)` | `5.99` | `0.0` |
| `age_group` | value change ×4 | `age_group(18)` | `'adult'` | `'teen'` |

![A real Behavior Diff check run on twinrun-playground PR #1, failing under behavior lock](docs/check-run.png)

*Real check run from the end-to-end test ([docs/E2E.md](docs/E2E.md)). The PR lowered a module constant and changed `<` to `<=`. Neither function's tests would have flagged it.*

A reviewer sees right away that `age_group(18)` changed, without having to work it out from a `<` → `<=`.
A PR that claims to be "just a refactor" can *prove* it: add the `refactor` label, and the
check fails if behavior changed anywhere.

## How it works

1. **AST diff, not line diff.** The merge-base and head are parsed. A function is a candidate when
   its syntax tree changed, or when module-level code around it changed (a constant, an import).
   Reformatting, comments, and docstrings are ignored. A reformat-only PR produces no candidates.
2. **Inputs.** [Hypothesis](https://hypothesis.works) strategies are built from type hints and
   literal defaults. Unhinted parameters get their types from **call sites and tests** in the repo,
   whose literal arguments are also replayed as seeds. Every constant in the function (and module)
   becomes a boundary probe `c-1, c, c+1`, and a deterministic **boundary sweep** tries each one in
   every numeric argument slot. That's how `>` vs `>=` and off-by-ones get hit without luck.
3. **Run both sides in a sandbox.** Each side runs in its own long-lived worker process
   (see [Sandbox](#sandbox)). TwinRun records the return value, the exception type and message,
   stdout, and the arguments after the call (to catch in-place mutation).
4. **Normalize and group.** Floats are compared with a tolerance, sets and dicts ignore order, objects
   are compared by their fields, and memory addresses are stripped. Divergences are grouped into
   *new exception, removed exception, type change, value change, exception change, timeout,
   argument mutation change, stdout change*.
5. **Shrink.** For each group, Hypothesis' `find()` searches for the simplest input that still shows
   that kind of divergence. The table shows the shrunk input. If shrinking didn't produce one,
   the row is marked "(not shrunk)" and shows the smallest input actually seen.
6. **Report.** A check run named **Behavior Diff**, plus **one** PR comment that is edited in
   place on every push (it is found by a hidden marker, so no comment spam).

## Behavior lock

| PR labels | divergence found | check conclusion |
|---|---|---|
| none | no | ✅ success |
| none | yes | 🟡 neutral, for information only |
| `refactor` or `no-behavior-change` | no | ✅ success |
| `refactor` or `no-behavior-change` | yes | ❌ **failure** |

Adding or removing one of those labels re-runs the check. Make **Behavior Diff** a required status
check in branch protection, and "pure refactor" PRs can't quietly change behavior.

## 60-second setup

```bash
git clone https://github.com/lakshanmuruganandam/twinrun.git && cd twinrun
uv sync
uv run twinrun create-app            # opens GitHub; click "Create GitHub App" once
#  -> writes .env (APP_ID, PRIVATE_KEY_PATH, WEBHOOK_SECRET, SMEE_URL) and the .pem, both git-ignored
#  -> prints the install link: install the app on the repos you want checked

npx smee-client --url "$(grep SMEE_URL .env | cut -d= -f2)" --target http://localhost:8000/webhook &
uv run twinrun serve                 # webhook server on :8000
```

Open a PR in an installed repo, and the **Behavior Diff** check appears within seconds.

`create-app` uses GitHub's [manifest flow](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest)
with `app-manifest.json`. The app is private, with permissions `checks: write`,
`pull_requests: write`, `contents: read`, `metadata: read`, and it listens to the
`pull_request` event. Webhook signatures (`X-Hub-Signature-256`) are verified on every request.

Try it locally without GitHub:

```bash
uv run twinrun analyze path/to/base-checkout path/to/head-checkout --label refactor
```

## Sandbox

The code under test is untrusted, so TwinRun stacks three layers:

1. **OS sandbox.** On macOS each worker runs under `sandbox-exec` with a profile that denies all
   network access and every file write outside the worker's own temp dir. On Linux it runs under
   `unshare -rn` (a fresh network namespace with no interfaces) when the host allows it; otherwise
   TwinRun says `sandbox: audit-hook-only` in every report and relies on layers 2 and 3.
2. **Audit hook** inside the worker (`sys.addaudithook`, which can't be removed once installed).
   It blocks sockets, `subprocess`/`os.system`/`fork`/`exec`, `ctypes`, and writes outside the
   temp dir, and records what was attempted so the report can say so.
3. **Limits.** A per-call alarm (1s), CPU/file-size rlimits, and a hard wall-clock deadline
   enforced by the parent: a worker that ignores the alarm is `SIGKILL`ed and restarted.

The reply channel only accepts plain values. The parent's unpickler refuses every class or global,
so hostile code that writes to the protocol pipe can't execute anything in the server.
`tests/test_sandbox.py` runs hostile code that builds its names at runtime to dodge static
checks. A real TCP listener must see zero connections, and a sentinel file must never appear.

## Limits (read these)

- **Python only.** Other languages are ignored.
- **Pure-ish functions work best**: module-level functions, `@staticmethod`s and `@classmethod`s
  whose arguments are built-in types (`int`, `float`, `str`, `bool`, `bytes`, `None`, and lists,
  dicts, tuples, sets, `Optional` and `Union` of those).
- **Skipped, and the report says so** (in a "Skipped" section with the reason for each function):
  instance methods (they need an object), parameters with custom-class hints, unhinted parameters
  with no literal call site to learn from, and **I/O-heavy code**. That covers functions that
  visibly call `open`, `requests`, `subprocess`, `socket`, …, and functions whose every call was
  blocked by the sandbox. "No divergence" means *no divergence among the inputs tried*. It is
  evidence, not a proof.
- The code is imported with TwinRun's own interpreter. Modules whose third-party imports aren't
  installed there are skipped with the import error shown.
- Nondeterministic functions (time, randomness) can show spurious divergences. `PYTHONHASHSEED` is
  pinned to reduce this.

## Evidence

| claim | how to check |
|---|---|
| tests pass | `uv run pytest -q` |
| ≥ 18/20 behavior changes caught, 0/10 refactor false alarms | `uv run python scripts/bench.py` → [BENCH.md](BENCH.md) |
| 50-function PR in < 3 min | `uv run python scripts/perf.py` → [PERF.md](PERF.md) |
| sandbox blocks network / writes / fork, kills hangs | `uv run pytest -q tests/test_sandbox.py` |
| end to end on GitHub | [docs/E2E.md](docs/E2E.md) |

## Layout

```
src/twinrun/
  astdiff.py     changed-function detection (AST fingerprints, module context)
  inputs.py      Hypothesis strategies from hints / defaults / call sites / constants
  worker.py      runs inside the sandbox; stdlib only
  sandbox.py     spawns, talks to, and kills workers
  compare.py     normalizer, divergence kinds, display
  engine.py      diff -> plan -> run both sides -> group -> shrink
  render.py      check-run output, PR comment, behavior-lock verdict
  github_app.py  webhook handling, check runs, upserted comment
  server.py      FastAPI app (/webhook)
  cli.py         twinrun analyze | serve | create-app
```

## License

MIT © 2026 Lakshan Muruganandam
