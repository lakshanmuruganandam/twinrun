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
