"""A fast slice of the benchmark, so `pytest` alone catches a regression in detection quality.
The full 30-fixture run is `uv run python scripts/bench.py`."""

import json
from pathlib import Path

import pytest

from twinrun.engine import Options, analyze

FIX = Path(__file__).parent / "fixtures"
CASES = ["03_age_group_boundary", "11_leap_400_rule", "16_bulk_price_boundary", "19_free_shipping_constant",
         "20_median_mutates_input", "22_extract_helper_discount", "24_reformat_stats", "30_leap_early_returns"]


@pytest.mark.parametrize("case", CASES)
def test_fixture(case):
    d = FIX / case
    if not d.exists():
        pytest.skip("run scripts/bench.py to build fixtures")
    meta = json.loads((d / "meta.json").read_text())
    rep = analyze(d / "base", d / "head", Options(max_examples=100))
    if meta["kind"] == "change":
        assert any(f.qualname == meta["target"] for f in rep.divergent), meta
    else:
        assert rep.divergent == [], [(f.qualname, f.groups) for f in rep.divergent]
