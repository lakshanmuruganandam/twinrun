"""Benchmark: build 30 fixture PRs in tests/fixtures and score TwinRun on them.

Every fixture is a small but realistic repo (package `lib/` + `tests/`). The base is
identical for all fixtures; each head applies ONE edit:
  * 20 edits change behavior (off-by-one, flipped boundary, new/removed exception,
    swapped args, rounding, None handling, constants, order, in-place mutation).
  * 10 edits are pure refactors (rename a local, extract a helper, reorder
    independent statements, reformat, add hints, rewrite a loop).

Edits are plain text substitutions applied to the base source, and each must match
exactly once, so a typo can't silently produce an identical head.

Pass criteria (from the goal): >= 18/20 changes caught, 0/10 refactors flagged.
A change counts as caught only if the *edited function* is reported as diverging.

    uv run python scripts/bench.py            # build fixtures + run + write BENCH.md
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT / "src"))

from twinrun.engine import Options, analyze  # noqa: E402

BASE: dict[str, str] = {
    "lib/__init__.py": '"""Tiny shop library used by the TwinRun benchmark."""\n',
    "lib/pricing.py": textwrap.dedent('''\
        """Prices, discounts, shipping."""

        FREE_SHIPPING_MIN = 50


        def shipping_cost(subtotal: float, express: bool = False) -> float:
            if subtotal >= FREE_SHIPPING_MIN:
                base = 0.0
            else:
                base = 5.99
            return base + (12.0 if express else 0.0)


        def apply_discount(price: float, percent: int) -> float:
            if percent < 0 or percent > 100:
                raise ValueError("percent out of range")
            return round(price * (100 - percent) / 100, 2)


        def bulk_price(unit, qty):
            total = unit * qty
            if qty >= 10:
                total *= 0.95
            return round(total, 2)


        def total_with_tax(amount: float, rate: float = 0.05) -> float:
            return round(amount * (1 + rate), 2)


        def invoice_total(items: list[float], rate: float = 0.05) -> float:
            return total_with_tax(sum(items), rate)
        '''),
    "lib/text.py": textwrap.dedent('''\
        """String helpers."""


        def slugify(title: str) -> str:
            out = []
            for ch in title.lower():
                if ch.isalnum():
                    out.append(ch)
                elif out and out[-1] != "-":
                    out.append("-")
            return "".join(out).strip("-")


        def truncate(s: str, width: int = 10) -> str:
            if len(s) <= width:
                return s
            return s[: width - 1] + "…"


        def word_count(text):
            return len([w for w in text.split() if w])


        def initials(name: str) -> str:
            parts = name.split()
            return "".join(p[0].upper() for p in parts)
        '''),
    "lib/stats.py": textwrap.dedent('''\
        """Small statistics helpers."""


        def mean(xs: list[float]) -> float:
            if not xs:
                raise ValueError("mean of empty list")
            return sum(xs) / len(xs)


        def median(xs: list[int]) -> float:
            if not xs:
                raise ValueError("median of empty list")
            s = sorted(xs)
            n = len(s)
            mid = n // 2
            if n % 2:
                return float(s[mid])
            return (s[mid - 1] + s[mid]) / 2


        def clamp(x: int, lo: int, hi: int) -> int:
            return max(lo, min(hi, x))


        def histogram(values: list[int], bucket: int = 10) -> dict[int, int]:
            out = {}
            for v in values:
                k = (v // bucket) * bucket
                out[k] = out.get(k, 0) + 1
            return out


        def moving_average(xs: list[float], window: int = 3) -> list[float]:
            if window <= 0:
                raise ValueError("window must be positive")
            return [sum(xs[i:i + window]) / window for i in range(len(xs) - window + 1)]
        '''),
    "lib/dates.py": textwrap.dedent('''\
        """Calendar arithmetic on plain ints."""


        def is_leap(year: int) -> bool:
            return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


        def days_in_month(year: int, month: int) -> int:
            if not 1 <= month <= 12:
                raise ValueError(f"bad month {month}")
            if month == 2:
                return 29 if is_leap(year) else 28
            return 30 if month in (4, 6, 9, 11) else 31


        def quarter(month: int) -> int:
            if not 1 <= month <= 12:
                raise ValueError("bad month")
            return (month - 1) // 3 + 1
        '''),
    "lib/users.py": textwrap.dedent('''\
        """User-facing helpers."""
        from typing import Optional


        def display_name(first: str, last: Optional[str] = None) -> str:
            if last:
                return f"{first} {last}"
            return first


        def age_group(age: int) -> str:
            if age < 13:
                return "child"
            if age < 18:
                return "teen"
            if age < 65:
                return "adult"
            return "senior"


        def merge_tags(a: list[str], b: list[str]) -> list[str]:
            seen = []
            for t in a + b:
                if t not in seen:
                    seen.append(t)
            return seen


        def password_ok(pw: str, min_len: int = 8) -> bool:
            return len(pw) >= min_len and any(c.isdigit() for c in pw)
        '''),
    "tests/test_lib.py": textwrap.dedent('''\
        from lib.pricing import bulk_price, shipping_cost
        from lib.text import word_count, slugify
        from lib.users import password_ok


        def test_bulk():
            assert bulk_price(2.5, 4) == 10.0
            assert bulk_price(1.0, 12) == 11.4


        def test_words():
            assert word_count("hello big world") == 3
            assert word_count("") == 0


        def test_misc():
            assert slugify("Hello World") == "hello-world"
            assert shipping_cost(10.0) == 5.99
            assert password_ok("hunter22")
        '''),
}

# (name, kind, category, file, target function, [(old, new), ...], description)
CASES = [
    # ------------------------------------------------------------ behavior changes
    ("truncate_off_by_one", "change", "off-by-one", "lib/text.py", "truncate",
     [('return s[: width - 1] + "…"', 'return s[: width] + "…"')], "keeps one char too many"),
    ("shipping_boundary", "change", "flipped boundary", "lib/pricing.py", "shipping_cost",
     [("if subtotal >= FREE_SHIPPING_MIN:", "if subtotal > FREE_SHIPPING_MIN:")], ">= became >"),
    ("age_group_boundary", "change", "flipped boundary", "lib/users.py", "age_group",
     [("if age < 18:", "if age <= 18:")], "18-year-olds become teens"),
    ("clamp_new_exception", "change", "new exception", "lib/stats.py", "clamp",
     [("    return max(lo, min(hi, x))",
       '    if lo > hi:\n        raise ValueError("lo > hi")\n    return max(lo, min(hi, x))')],
     "now raises when lo > hi"),
    ("days_removed_validation", "change", "removed exception", "lib/dates.py", "days_in_month",
     [('    if not 1 <= month <= 12:\n        raise ValueError(f"bad month {month}")\n', "")],
     "month 13 no longer raises"),
    ("invoice_swapped_args", "change", "swapped args", "lib/pricing.py", "invoice_total",
     [("return total_with_tax(sum(items), rate)", "return total_with_tax(rate, sum(items))")],
     "amount and rate swapped"),
    ("discount_rounding", "change", "rounding change", "lib/pricing.py", "apply_discount",
     [("return round(price * (100 - percent) / 100, 2)", "return int(price * (100 - percent)) / 100")],
     "truncates instead of rounding"),
    ("display_name_none", "change", "None handling", "lib/users.py", "display_name",
     [("if last:", "if last is not None:")], 'last="" now adds a trailing space'),
    ("moving_average_off_by_one", "change", "off-by-one", "lib/stats.py", "moving_average",
     [("range(len(xs) - window + 1)", "range(len(xs) - window)")], "drops the last window"),
    ("median_even", "change", "value change", "lib/stats.py", "median",
     [("return (s[mid - 1] + s[mid]) / 2", "return float(s[mid])")], "even-length median wrong"),
    ("leap_400_rule", "change", "value change", "lib/dates.py", "is_leap",
     [("(year % 100 != 0 or year % 400 == 0)", "year % 100 != 0")], "forgets the 400-year rule"),
    ("slugify_digits", "change", "value change", "lib/text.py", "slugify",
     [("if ch.isalnum():", "if ch.isalpha():")], "drops digits"),
    ("word_count_split", "change", "value change", "lib/text.py", "word_count",
     [("text.split()", 'text.split(" ")')], "tabs/newlines no longer split words"),
    ("histogram_negative", "change", "rounding change", "lib/stats.py", "histogram",
     [("k = (v // bucket) * bucket", "k = int(v / bucket) * bucket")], "negatives bucket toward zero"),
    ("quarter_off_by_one", "change", "off-by-one", "lib/dates.py", "quarter",
     [("return (month - 1) // 3 + 1", "return month // 3 + 1")], "March becomes Q2"),
    ("bulk_price_boundary", "change", "flipped boundary", "lib/pricing.py", "bulk_price",
     [("if qty >= 10:", "if qty > 10:")], "unhinted; boundary at qty=10"),
    ("merge_tags_order", "change", "value change", "lib/users.py", "merge_tags",
     [("    seen = []\n    for t in a + b:\n        if t not in seen:\n            seen.append(t)\n    return seen",
       "    return sorted(set(a + b))")], "loses insertion order"),
    ("password_boundary", "change", "flipped boundary", "lib/users.py", "password_ok",
     [("len(pw) >= min_len", "len(pw) > min_len")], "exactly-min-length passwords rejected"),
    ("free_shipping_constant", "change", "constant change", "lib/pricing.py", "shipping_cost",
     [("FREE_SHIPPING_MIN = 50", "FREE_SHIPPING_MIN = 40")],
     "function untouched; module constant changed"),
    ("median_mutates_input", "change", "in-place mutation", "lib/stats.py", "median",
     [("    s = sorted(xs)\n", "    xs.sort()\n    s = xs\n")], "sorts the caller's list in place"),
    # ------------------------------------------------------------ pure refactors
    ("rename_local_slugify", "refactor", "rename a local", "lib/text.py", "slugify",
     [("    out = []\n", "    chars = []\n"), ("            out.append(ch)", "            chars.append(ch)"),
      ('        elif out and out[-1] != "-":\n            out.append("-")\n    return "".join(out).strip("-")',
       '        elif chars and chars[-1] != "-":\n            chars.append("-")\n'
       '    return "".join(chars).strip("-")')],
     "out -> chars"),
    ("extract_helper_discount", "refactor", "extract a helper", "lib/pricing.py", "apply_discount",
     [('def apply_discount(price: float, percent: int) -> float:\n    if percent < 0 or percent > 100:\n'
       '        raise ValueError("percent out of range")\n',
       'def _check_percent(percent):\n    if percent < 0 or percent > 100:\n'
       '        raise ValueError("percent out of range")\n\n\n'
       'def apply_discount(price: float, percent: int) -> float:\n    _check_percent(percent)\n')],
     "validation moved to _check_percent"),
    ("reorder_median", "refactor", "reorder independent statements", "lib/stats.py", "median",
     [("    s = sorted(xs)\n    n = len(s)\n    mid = n // 2\n",
       "    n = len(xs)\n    mid = n // 2\n    s = sorted(xs)\n")], "compute n/mid before sorting"),
    ("reformat_stats", "refactor", "reformat", "lib/stats.py", "*",
     [("    return [sum(xs[i:i + window]) / window for i in range(len(xs) - window + 1)]",
       "    return [\n        sum(xs[i : i + window]) / window  # one value per full window\n"
       "        for i in range(len(xs) - window + 1)\n    ]"),
      ("        raise ValueError(\"mean of empty list\")", "        raise ValueError('mean of empty list')"),
      ("def clamp(x: int, lo: int, hi: int) -> int:\n    return max(lo, min(hi, x))",
       "def clamp(x: int, lo: int, hi: int) -> int:\n    \"\"\"Clamp x into [lo, hi].\"\"\"\n"
       "    return max(lo,\n               min(hi, x))")],
     "black-style reformat + docstring + quotes"),
    ("rename_local_bulk", "refactor", "rename a local", "lib/pricing.py", "bulk_price",
     [("    total = unit * qty\n    if qty >= 10:\n        total *= 0.95\n    return round(total, 2)",
       "    amount = unit * qty\n    if qty >= 10:\n        amount *= 0.95\n    return round(amount, 2)")],
     "total -> amount"),
    ("word_count_generator", "refactor", "rewrite a loop", "lib/text.py", "word_count",
     [("return len([w for w in text.split() if w])", "return sum(1 for w in text.split() if w)")],
     "list comprehension -> generator sum"),
    ("extract_helper_tags", "refactor", "extract a helper", "lib/users.py", "merge_tags",
     [("def merge_tags(a: list[str], b: list[str]) -> list[str]:\n    seen = []\n    for t in a + b:\n"
       "        if t not in seen:\n            seen.append(t)\n    return seen",
       "def _unique(seq):\n    seen = []\n    for t in seq:\n        if t not in seen:\n"
       "            seen.append(t)\n    return seen\n\n\n"
       "def merge_tags(a: list[str], b: list[str]) -> list[str]:\n    return _unique(a + b)")],
     "dedupe loop moved to _unique"),
    ("add_hints_bulk", "refactor", "add type hints", "lib/pricing.py", "bulk_price",
     [("def bulk_price(unit, qty):", "def bulk_price(unit: float, qty: int) -> float:")], "adds annotations"),
    ("shipping_ternary", "refactor", "rewrite a branch", "lib/pricing.py", "shipping_cost",
     [("    if subtotal >= FREE_SHIPPING_MIN:\n        base = 0.0\n    else:\n        base = 5.99\n",
       "    base = 0.0 if subtotal >= FREE_SHIPPING_MIN else 5.99\n")], "if/else -> conditional expression"),
    ("leap_early_returns", "refactor", "rewrite a branch", "lib/dates.py", "is_leap",
     [("    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)",
       "    if year % 400 == 0:\n        return True\n    if year % 100 == 0:\n        return False\n"
       "    return year % 4 == 0")], "boolean expression -> early returns"),
]


def build() -> list[dict]:
    if FIXTURES.exists():
        shutil.rmtree(FIXTURES)
    metas = []
    for i, (name, kind, cat, file, target, edits, desc) in enumerate(CASES, 1):
        case = FIXTURES / f"{i:02d}_{name}"
        for side in ("base", "head"):
            for rel, src in BASE.items():
                p = case / side / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(src)
        head_file = case / "head" / file
        src = head_file.read_text()
        for old, new in edits:
            n = src.count(old)
            if n != 1:
                raise SystemExit(f"{name}: edit matched {n} times, expected 1: {old!r}")
            src = src.replace(old, new)
        head_file.write_text(src)
        meta = {"name": name, "kind": kind, "category": cat, "file": file, "target": target,
                "description": desc}
        (case / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        metas.append({**meta, "dir": case})
    return metas


def check_test(test_src: str, case_dir: Path) -> str:
    """Run a generated regression test against base and head. 'ok' = passes on base, fails on head."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "test_twinrun_generated.py"
        f.write_text(test_src)
        rc = {}
        for side in ("base", "head"):
            env = {**os.environ, "PYTHONPATH": str(case_dir / side)}
            rc[side] = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(f)],
                                      cwd=tmp, env=env, capture_output=True).returncode
    return "ok" if rc["base"] == 0 and rc["head"] == 1 else f"BAD(base rc={rc['base']}, head rc={rc['head']})"


def run(metas: list[dict], opts: Options) -> list[dict]:
    rows = []
    for m in metas:
        t0 = time.monotonic()
        rep = analyze(m["dir"] / "base", m["dir"] / "head", opts)
        dt = time.monotonic() - t0
        div = rep.divergent
        target_div = [f for f in div if m["target"] in ("*", f.qualname) and f.file == m["file"]]
        if m["kind"] == "change":
            ok = bool(target_div)
            outcome = "caught" if ok else "MISSED"
        else:
            ok = not div
            outcome = "clean" if ok else "FALSE ALARM"
        g = (target_div or div or [None])[0]
        first = g.groups[0] if g and g.groups else None
        tests = [x.test for f in target_div for x in f.groups if x.test] if m["kind"] == "change" else []
        test_results = [check_test(t, m["dir"]) for t in tests]
        test_cell = ("—" if m["kind"] != "change" else
                     "none" if not tests else
                     f"{sum(r == 'ok' for r in test_results)}/{len(tests)} ✓" if all(r == "ok" for r in test_results)
                     else "; ".join(test_results))
        rows.append({
            **{k: m[k] for k in ("name", "kind", "category", "target")},
            "outcome": outcome, "ok": ok, "seconds": round(dt, 2),
            "candidates": len(rep.functions), "compared": len(rep.compared),
            "evidence": f"{first.kind}: {first.call} → base {first.base} / head {first.head}" if first else "",
            "tests": test_cell, "tests_ok": all(r == "ok" for r in test_results), "n_tests": len(tests),
            "n_tests_ok": sum(r == "ok" for r in test_results),
        })
        print(f"{m['name']:<28} {m['kind']:<9} {outcome:<12} {dt:5.1f}s  {rows[-1]['evidence'][:90]}")
    return rows


def write_md(rows: list[dict], opts: Options) -> str:
    changes = [r for r in rows if r["kind"] == "change"]
    refactors = [r for r in rows if r["kind"] == "refactor"]
    caught = sum(r["ok"] for r in changes)
    alarms = sum(not r["ok"] for r in refactors)
    passed = caught >= 18 and alarms == 0 and all(r["tests_ok"] for r in rows)
    lines = [
        "# TwinRun benchmark",
        "",
        f"Generated by `uv run python scripts/bench.py` · {len(rows)} fixture PRs in `tests/fixtures/` · "
        f"{opts.max_examples} generated inputs per function, shrinking on.",
        "",
        f"- **Behavior changes caught: {caught}/{len(changes)}** (goal ≥ 18)",
        f"- **False alarms on pure refactors: {alarms}/{len(refactors)}** (goal 0)",
        f"- **Generated regression tests that pass on base and fail on head: "
        f"{sum(r['n_tests_ok'] for r in rows)}/{sum(r['n_tests'] for r in rows)}** "
        f"({sum(1 for r in changes if r['n_tests'])}/{len(changes)} caught changes came with a test)",
        f"- Result: **{'PASS' if passed else 'FAIL'}** · total {sum(r['seconds'] for r in rows):.1f}s",
        "",
        "| # | fixture | kind | category | outcome | compared | regression test | time | evidence (smallest input) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        ev = r["evidence"].replace("|", "\\|")
        lines.append(f"| {i} | `{r['name']}` | {r['kind']} | {r['category']} | {r['outcome']} | "
                     f"{r['compared']} | {r['tests']} | {r['seconds']}s | {ev} |")
    lines += ["", "A change is *caught* only when the edited function itself is reported as diverging. "
              "A refactor is *clean* only when no function at all diverges; the *compared* column shows the "
              "refactored function really ran (the reformat fixture has 0 because its AST is unchanged, "
              "which is the point).",
              "",
              "Caveat: the fixtures were written together with TwinRun, so treat this as a regression suite, "
              "not an independent evaluation. Pure, small functions with built-in argument types are the "
              "best case for this approach; see README → Limits."]
    return "\n".join(lines) + "\n"


def main() -> int:
    opts = Options()
    metas = build()
    print(f"built {len(metas)} fixtures in {FIXTURES.relative_to(ROOT)}")
    rows = run(metas, opts)
    md = write_md(rows, opts)
    (ROOT / "BENCH.md").write_text(md)
    print()
    print(md)
    changes = [r for r in rows if r["kind"] == "change"]
    refactors = [r for r in rows if r["kind"] == "refactor"]
    tests_ok = all(r["tests_ok"] for r in rows)
    return 0 if sum(r["ok"] for r in changes) >= 18 and all(r["ok"] for r in refactors) and tests_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
