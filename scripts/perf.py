"""Performance check: a PR that edits 50 functions must finish in < 3 minutes.

Builds 5 modules x 10 functions; every function is edited (alternating a behavior
change and a pure refactor) so all 50 are compared. Times the full analyze() with
default options and appends the result to PERF.md.

    uv run python scripts/perf.py
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from twinrun.engine import Options, analyze  # noqa: E402

# (signature, base body, behavior-changing edit, refactor edit)
TEMPLATES = [
    ("(x: int, k: int) -> int", "return x * k if x > {c} else x + k",
     ("x > {c}", "x >= {c}"), ("return x * k if x > {c} else x + k", "y = x * k if x > {c} else x + k\n    return y")),
    ("(xs: list[int]) -> int", "return sum(v for v in xs if v % {m} == 0)",
     ("v % {m} == 0", "v % {m} == 1"), ("sum(v for v in xs if v % {m} == 0)", "sum([v for v in xs if v % {m} == 0])")),
    ("(s: str, n: int = {c}) -> str", "return s[:n].upper()",
     ("s[:n]", "s[:n + 1]"), ("return s[:n].upper()", "head = s[:n]\n    return head.upper()")),
    ("(a: float, b: float) -> float", "return round(a / b, 2) if b else 0.0",
     ("round(a / b, 2)", "round(a / b, 1)"), ("return round(a / b, 2) if b else 0.0",
                                               "if not b:\n        return 0.0\n    return round(a / b, 2)")),
    ("(d: dict[str, int], key: str) -> int", "return d.get(key, {c})",
     ("d.get(key, {c})", "d[key]"), ("return d.get(key, {c})", "value = d.get(key, {c})\n    return value")),
]


def build(tmp: Path) -> tuple[Path, Path]:
    base, head = tmp / "base", tmp / "head"
    for root in (base, head):
        (root / "app").mkdir(parents=True)
        (root / "app" / "__init__.py").write_text("")
    for m in range(5):
        b_src, h_src = [], []
        for i in range(10):
            sig, body, change, refactor = TEMPLATES[(m + i) % len(TEMPLATES)]
            fill = {"c": 7 * i + m + 3, "m": i % 4 + 2}
            body_f = body.format(**fill)
            name = f"f{m}_{i}"
            b_src.append(f"def {name}{sig.format(**fill)}:\n    {body_f}\n")
            old, new = change if i % 2 == 0 else refactor
            h_body = body_f.replace(old.format(**fill), new.format(**fill), 1)
            assert h_body != body_f, (name, old)
            h_src.append(f"def {name}{sig.format(**fill)}:\n    {h_body}\n")
        (base / "app" / f"mod{m}.py").write_text("\n\n".join(b_src))
        (head / "app" / f"mod{m}.py").write_text("\n\n".join(h_src))
    return base, head


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="twinrun-perf-"))
    try:
        base, head = build(tmp)
        opts = Options()
        t0 = time.monotonic()
        rep = analyze(base, head, opts)
        dt = time.monotonic() - t0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    n = len(rep.functions)
    inputs = sum(f.inputs for f in rep.functions)
    # Even-numbered functions got a behavior change, odd ones a refactor.
    expected = {f"f{m}_{i}" for m in range(5) for i in range(0, 10, 2)}
    exact = {f.qualname for f in rep.divergent} == expected
    ok = n == 50 and len(rep.compared) == 50 and dt < 180 and exact
    line = (f"| {datetime.now():%Y-%m-%d %H:%M} | {platform.machine()} {platform.mac_ver()[0] or platform.system()} · "
            f"{os.cpu_count()} cores | {n} | {len(rep.compared)} | {len(rep.divergent)} | {inputs} | "
            f"{opts.jobs} | **{dt:.1f}s** | {'PASS' if ok else 'FAIL'} |")
    perf = ROOT / "PERF.md"
    if not perf.exists():
        perf.write_text("# TwinRun performance log\n\nGoal: a 50-function PR in under 3 minutes "
                        "(`uv run python scripts/perf.py`). Half the edits change behavior, half are refactors.\n\n"
                        "| when | machine | functions | compared | diverged | inputs run | jobs | wall time "
                        "| result |\n"
                        "|---|---|---|---|---|---|---|---|---|\n")
    with perf.open("a") as fh:
        fh.write(line + "\n")
    print(f"{n} functions, {len(rep.compared)} compared, {len(rep.divergent)} diverged, "
          f"{inputs} inputs per side, {dt:.1f}s wall; diverged set == the 25 changed functions: {exact} "
          f"-> {'PASS' if ok else 'FAIL'} (limit 180s)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
