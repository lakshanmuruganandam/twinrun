from twinrun import compare as C
from twinrun.engine import Options, analyze
from twinrun.worker import canonical


def ok(v, stdout="", args=((), {})):
    return {"kind": "ok", "value": canonical(v), "type": type(v).__name__, "stdout": stdout,
            "args_after": canonical((list(args[0]), args[1]))}


def exc(t, m=""):
    return {"kind": "exc", "type": t, "msg": m, "stdout": "", "args_after": None}


def test_normalizer_float_tolerance_and_orders():
    assert C.classify(ok(0.1 + 0.2), ok(0.3)) is None
    assert C.classify(ok({3, 1, 2}), ok({1, 2, 3})) is None
    assert C.classify(ok({"b": 1, "a": 2}), ok({"a": 2, "b": 1})) is None
    assert C.classify(ok(float("nan")), ok(float("nan"))) is None
    assert C.classify(ok(0.3), ok(0.31)) == C.VALUE


def test_normalizer_strips_memory_addresses():
    class P:
        __slots__ = ()

    a, b = P(), P()
    assert C.classify(ok(a), ok(b)) is None


def test_objects_compare_by_fields():
    class Pt:
        def __init__(self, x):
            self.x = x

    assert C.classify(ok(Pt(1)), ok(Pt(1))) is None
    assert C.classify(ok(Pt(1)), ok(Pt(2))) == C.VALUE


def test_kinds():
    assert C.classify(ok(1), exc("ValueError")) == C.NEW_EXC
    assert C.classify(exc("ValueError"), ok(1)) == C.REMOVED_EXC
    assert C.classify(exc("ValueError", "a"), exc("KeyError", "a")) == C.EXC_CHANGE
    assert C.classify(exc("ValueError", "a"), exc("ValueError", "a")) is None
    assert C.classify(ok(1), ok(1.0)) == C.TYPE
    assert C.classify(ok(1), {"kind": "timeout"}) == C.TIMEOUT
    assert C.classify({"kind": "timeout"}, {"kind": "timeout"}) is None
    assert C.classify(ok(1, "a"), ok(1, "b")) == C.STDOUT
    assert C.classify(ok(1, args=(([1, 2],), {})), ok(1, args=(([2, 1],), {}))) == C.MUTATION
    assert C.classify({"kind": "blocked", "what": "x"}, ok(1)) is None


def test_display():
    assert C.show_outcome(ok([1, "a", None])) == "[1, 'a', None]"
    assert C.show_outcome(exc("ValueError", "bad")) == "raises ValueError('bad')"
    assert C.show_call("f", (1, "x"), {"k": 2}) == "f(1, 'x', k=2)"
    assert len(C.show_outcome(ok("x" * 500))) <= 80


def test_grouping_and_shrinking(make_pr):
    base = "def f(x: int, y: int) -> int:\n    if x > 500:\n        return x\n    return y\n"
    head = ("def f(x: int, y: int) -> int:\n    if x > 500:\n        raise ValueError('big')\n"
            "    if y < 0:\n        return -y\n    return y\n")
    b, h = make_pr({"m.py": base}, {"m.py": head})
    rep = analyze(b, h, Options(max_examples=200))
    (fr,) = rep.functions
    kinds = {g.kind: g for g in fr.groups}
    assert set(kinds) == {C.NEW_EXC, C.VALUE}
    # Shrunk to the simplest witnesses: the boundary x=501 and y=-1.
    assert kinds[C.NEW_EXC].call.startswith("f(501,")
    assert kinds[C.VALUE].call == "f(0, -1)" and kinds[C.VALUE].shrunk
    assert kinds[C.NEW_EXC].count > 1


def test_regression_test_generation():
    t = C.regression_test("pkg.m", "f", ((18,), {}), ok("adult"), ok("teen"), C.VALUE)
    assert "from pkg.m import f" in t and "assert f(18) == 'adult'  # head: 'teen'" in t
    t = C.regression_test("m", "f", ((0.5,), {}), ok(0.25), ok(0.5), C.VALUE)
    assert "pytest.approx(0.25)" in t and t.startswith("import pytest")
    t = C.regression_test("m", "f", ((0,), {}), exc("ZeroDivisionError", "integer division"),
                          exc("ZeroDivisionError", "division by zero"), C.EXC_CHANGE)
    assert "pytest.raises(ZeroDivisionError, match='^integer\\\\ division$')" in t
    t = C.regression_test("m", "K.s", ((1,), {}), ok(1), ok(2), C.VALUE)
    assert "from m import K" in t and "K.s(1)" in t
    # Not expressible as plain Python -> no test rather than a wrong one
    assert C.regression_test("m", "f", ((float("nan"),), {}), ok(1), ok(2), C.VALUE) is None
    assert C.regression_test("m", "f", ((1,), {}), ok(1), {"kind": "timeout"}, C.TIMEOUT) is None


def test_generated_tests_really_pass_on_base_and_fail_on_head(make_pr, tmp_path):
    import os
    import subprocess
    import sys

    base = "def f(x: int) -> int:\n    if x < 0:\n        raise ValueError('negative')\n    return x // 2\n"
    head = "def f(x: int) -> int:\n    return abs(x) // 2 if x != 7 else 0\n"
    b, h = make_pr({"m.py": base}, {"m.py": head})
    rep = analyze(b, h, Options(max_examples=100))
    tests = [g.test for f in rep.divergent for g in f.groups if g.test]
    assert len(tests) >= 2
    for i, src in enumerate(tests):
        tf = tmp_path / f"test_gen_{i}.py"
        tf.write_text(src)
        rcs = [subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tf)],
                              cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(side)},
                              capture_output=True).returncode for side in (b, h)]
        assert rcs == [0, 1], (src, rcs)


def test_default_arguments_are_dropped_from_the_shown_input(make_pr):
    base = "def ship(total: float, express: bool = False) -> float:\n    return 0.0 if total >= 50 else 5.99\n"
    b, h = make_pr({"m.py": base}, {"m.py": base.replace(">= 50", ">= 40")})
    (fr,) = analyze(b, h).functions
    assert fr.groups[0].call.startswith("ship(4") and "False" not in fr.groups[0].call
