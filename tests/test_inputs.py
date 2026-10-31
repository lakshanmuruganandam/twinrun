import ast

import pytest

from twinrun.astdiff import diff_trees
from twinrun.engine import draw_inputs
from twinrun.inputs import CallSiteIndex, Constants, Unsupported, build_plan, strategy_for


def plan_for(make_pr, base_src, head_src, extra=None, name=None):
    files_b = {"m.py": base_src, **(extra or {})}
    files_h = {"m.py": head_src, **(extra or {})}
    base, head = make_pr(files_b, files_h)
    cands = diff_trees(base, head).candidates
    cand = next(c for c in cands if name is None or c.qualname == name)
    return build_plan(cand, CallSiteIndex(head))


def ann(src):
    return ast.parse(src, mode="eval").body


@pytest.mark.parametrize("hint, check", [
    ("int", lambda v: type(v) is int),
    ("float", lambda v: type(v) is float),
    ("str", lambda v: type(v) is str),
    ("bool", lambda v: type(v) is bool),
    ("list[int]", lambda v: isinstance(v, list) and all(type(x) is int for x in v)),
    ("dict[str, float]", lambda v: isinstance(v, dict) and all(type(k) is str for k in v)),
    ("tuple[int, str]", lambda v: isinstance(v, tuple) and len(v) == 2),
    ("tuple[int, ...]", lambda v: isinstance(v, tuple)),
    ("set[int]", lambda v: isinstance(v, set)),
    ("Optional[int]", lambda v: v is None or type(v) is int),
    ("int | None", lambda v: v is None or type(v) is int),
    ("Union[int, str]", lambda v: type(v) in (int, str)),
    ("'int'", lambda v: type(v) is int),
])
def test_strategies_from_hints(hint, check):
    values = draw_inputs(strategy_for(ann(hint), Constants()), 40)
    assert values and all(check(v) for v in values)


def test_unknown_hint_is_unsupported():
    with pytest.raises(Unsupported):
        strategy_for(ann("Customer"), Constants())


def test_boundary_constants_are_generated(make_pr):
    src = "def f(x: int) -> bool:\n    return x > 1234\n"
    plan = plan_for(make_pr, src, src.replace("x > 1234", "x >= 1234"))
    xs = {args[0] for args, _ in draw_inputs(plan.strategy, 150)}
    assert {1233, 1234, 1235} <= xs


def test_unhinted_types_come_from_call_sites(make_pr):
    src = "def f(a, b):\n    return a * b\n"
    extra = {"tests/test_m.py": "from m import f\n\ndef test_f():\n    assert f(3, 'x') == 'xxx'\n"}
    plan = plan_for(make_pr, src, src.replace("a * b", "b * a"), extra)
    draws = draw_inputs(plan.strategy, 60)
    assert all(type(a) is int and type(b) is str for (a, b), _ in draws)
    assert ((3, "x"), {}) in plan.seeds
    assert any("call site" in n for n in plan.notes)


def test_unhinted_without_call_sites_is_skipped_with_reason(make_pr):
    src = "def f(a):\n    return a\n"
    with pytest.raises(Unsupported, match="no type hint"):
        plan_for(make_pr, src, src.replace("return a", "return a or 1"))


def test_defaults_used_for_unhinted_params(make_pr):
    src = "def f(x: int, scale=2):\n    return x * scale\n"
    plan = plan_for(make_pr, src, src.replace("x * scale", "scale * x"))
    draws = draw_inputs(plan.strategy, 50)
    assert all(len(args) == 2 for args, _ in draws)


def test_io_heavy_and_instance_methods_are_skipped(make_pr):
    src = "def f(p: str) -> str:\n    return open(p).read()\n"
    with pytest.raises(Unsupported, match="I/O"):
        plan_for(make_pr, src, src.replace("read()", "read().strip()"))
    cls = "class K:\n    def m(self, x: int) -> int:\n        return x\n"
    with pytest.raises(Unsupported, match="instance method"):
        plan_for(make_pr, cls, cls.replace("return x", "return x + 1"))


def test_classmethod_drops_cls(make_pr):
    src = "class K:\n    @classmethod\n    def c(cls, x: int) -> int:\n        return x\n"
    plan = plan_for(make_pr, src, src.replace("return x", "return -x"))
    assert [p.name for p in plan.params] == ["x"]


def test_signature_change_is_noted(make_pr):
    src = "def f(a: int, b: int) -> int:\n    return a - b\n"
    plan = plan_for(make_pr, src, "def f(b: int, a: int) -> int:\n    return a - b\n")
    assert any("signature changed" in n for n in plan.notes)
