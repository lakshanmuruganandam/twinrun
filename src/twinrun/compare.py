"""Normalize and compare two outcomes from the sandbox, and render them for humans."""

from __future__ import annotations

import math

REL_TOL = 1e-9
ABS_TOL = 1e-12

NEW_EXC = "new exception"
REMOVED_EXC = "removed exception"
EXC_CHANGE = "exception change"
VALUE = "value change"
TYPE = "type change"
TIMEOUT = "timeout"
STDOUT = "stdout change"
MUTATION = "argument mutation change"

KIND_ORDER = [NEW_EXC, REMOVED_EXC, TYPE, VALUE, EXC_CHANGE, TIMEOUT, MUTATION, STDOUT]


def trees_equal(a, b) -> bool:
    """Structural equality with a float tolerance. Trees come from worker.canonical."""
    if isinstance(a, (tuple, list)) and isinstance(b, (tuple, list)):
        if len(a) != len(b):
            return False
        if len(a) == 2 and a[0] == "float" and b[0] == "float":
            x, y = a[1], b[1]
            if x == "nan" or y == "nan":
                return x == y
            if math.isinf(x) or math.isinf(y):
                return x == y
            return math.isclose(x, y, rel_tol=REL_TOL, abs_tol=ABS_TOL)
        return all(trees_equal(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def classify(base: dict, head: dict) -> str | None:
    """Return the divergence kind, or None when the two runs behave the same.

    ``blocked`` outcomes are not divergences; the engine handles them separately.
    """
    bk, hk = base["kind"], head["kind"]
    if "blocked" in (bk, hk):
        return None
    if bk == "timeout" and hk == "timeout":
        return None
    if "timeout" in (bk, hk):
        return TIMEOUT
    if bk == "exc" and hk == "exc":
        if base["type"] != head["type"] or base["msg"] != head["msg"]:
            return EXC_CHANGE
        return _side_effects(base, head)
    if bk == "ok" and hk == "exc":
        return NEW_EXC
    if bk == "exc" and hk == "ok":
        return REMOVED_EXC
    if base["type"] != head["type"]:
        return TYPE
    if not trees_equal(base["value"], head["value"]):
        return VALUE
    return _side_effects(base, head)


def _side_effects(base: dict, head: dict) -> str | None:
    if base.get("args_after") is not None and head.get("args_after") is not None \
            and not trees_equal(base["args_after"], head["args_after"]):
        return MUTATION
    if base.get("stdout", "") != head.get("stdout", ""):
        return STDOUT
    return None


# ----------------------------------------------------------------- display

def show_tree(t, limit: int = 80) -> str:
    s = _show(t)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _show(t) -> str:
    if not isinstance(t, (tuple, list)) or not t:
        return repr(t)
    tag = t[0]
    if tag in ("NoneType",):
        return "None"
    if tag in ("bool", "int", "str", "bytes", "bytearray"):
        return repr(t[1])
    if tag == "float":
        return "nan" if t[1] == "nan" else repr(t[1])
    if tag == "complex":
        return repr(complex(*t[1]))
    if tag == "list":
        return "[" + ", ".join(_show(x) for x in t[1]) + "]"
    if tag == "tuple":
        inner = ", ".join(_show(x) for x in t[1])
        return "(" + inner + ("," if len(t[1]) == 1 else "") + ")"
    if tag in ("set", "frozenset"):
        if not t[1]:
            return f"{tag}()"
        inner = "{" + ", ".join(_show(x) for x in t[1]) + "}"
        return inner if tag == "set" else f"frozenset({inner})"
    if tag == "dict":
        return "{" + ", ".join(f"{_show(k)}: {_show(v)}" for k, v in t[1]) + "}"
    if tag == "iter":
        return "<iter " + "[" + ", ".join(_show(x) for x in t[1]) + "]>"
    if tag == "obj":
        fields = t[2][1] if isinstance(t[2], (tuple, list)) and len(t[2]) == 2 else []
        return f"{t[1]}(" + ", ".join(f"{_show(k).strip(chr(39))}={_show(v)}" for k, v in fields) + ")"
    if tag == "repr":
        return t[2]
    if tag == "deep":
        return "…"
    if len(t) == 2 and isinstance(t[1], str):  # enums / subclasses: (qualname, repr)
        return t[1]
    return repr(t)


def show_outcome(o: dict, limit: int = 80, kind: str | None = None) -> str:
    """Render one side. For side-effect divergences, show the side effect itself."""
    k = o["kind"]
    if kind == MUTATION and o.get("args_after") is not None:
        after = o["args_after"][1][0]  # ('tuple', [('list', positional...), kwargs])
        s = f"args after call: {_show(after)[1:-1]}"
    elif kind == STDOUT and k in ("ok", "exc"):
        s = f"prints {o.get('stdout', '')!r}"
    elif k == "ok":
        s = show_tree(o["value"], limit)
    elif k == "exc":
        s = f"raises {o['type']}({o['msg']!r})" if o["msg"] else f"raises {o['type']}"
    elif k == "timeout":
        s = f"timeout ({o['how']})" if o.get("how") else "timeout"
    elif k == "blocked":
        s = f"blocked ({o['what']})"
    else:
        s = repr(o)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def show_call(name: str, args: tuple, kwargs: dict, limit: int = 80) -> str:
    parts = [repr(a) for a in args] + [f"{k}={v!r}" for k, v in kwargs.items()]
    s = f"{name}({', '.join(parts)})"
    return s if len(s) <= limit else s[: limit - 1] + "…"


def input_size(call: tuple[tuple, dict]) -> tuple[int, str]:
    """Smaller is simpler: shorter repr first, then lexicographic for stability."""
    r = repr(call)
    return (len(r), r)


# ----------------------------------------------------------------- regression tests

_BUILTIN_EXC = {n for n, v in __import__("builtins").__dict__.items()
                if isinstance(v, type) and issubclass(v, BaseException)}


def _literal(tree) -> str | None:
    """Python source for a canonical tree, or None when it isn't a plain literal."""
    if not isinstance(tree, (tuple, list)) or not tree:
        return None
    if tree[0] in ("obj", "repr", "iter", "deep") or tree[0] not in (
            "NoneType", "bool", "int", "float", "str", "bytes", "list", "tuple", "set", "frozenset", "dict",
            "complex"):
        return None
    src = _show(tree)
    try:
        import ast

        ast.literal_eval(src)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None  # nan, inf, nested objects...
    return src


def _has_float(tree) -> bool:
    if isinstance(tree, (tuple, list)):
        if len(tree) == 2 and tree[0] == "float":
            return True
        return any(_has_float(x) for x in tree)
    return False


def regression_test(module: str, qualname: str, call: tuple, base: dict, head: dict, kind: str) -> str | None:
    """A ready-to-paste pytest test that pins the merge-base behavior for this input.

    Returns None when the input or the expected value can't be written as plain Python."""
    import ast
    import re as _re

    args, kwargs = call
    try:
        if ast.literal_eval(repr((tuple(args), dict(kwargs)))) != (tuple(args), dict(kwargs)):
            return None
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None
    top = qualname.split(".")[0]
    slug = _re.sub(r"[^0-9a-zA-Z]+", "_", f"{qualname}_{kind}").strip("_").lower()
    call_src = show_call(qualname, tuple(args), dict(kwargs), limit=10**9)
    head_desc = show_outcome(head, limit=120, kind=kind)
    lines = []
    if kind == MUTATION and args and isinstance(args[0], (list, dict, set)) and base.get("args_after"):
        before = repr(args[0])
        rest = ", ".join(["arg"] + [repr(a) for a in args[1:]] + [f"{k}={v!r}" for k, v in kwargs.items()])
        lines = [f"    arg = {before}", f"    {qualname}({rest})",
                 f"    assert arg == {before}  # head mutates it: {head_desc}"]
    elif base["kind"] == "exc" and base["type"] in _BUILTIN_EXC and kind in (REMOVED_EXC, EXC_CHANGE):
        match = ""
        if base.get("msg") and (kind == EXC_CHANGE or base["type"] == head.get("type")):
            match = f", match={'^' + _re.escape(base['msg']) + '$'!r}"  # same type: the message is the contract
        lines = [f"    with pytest.raises({base['type']}{match}):", f"        {call_src}",
                 f"    # head instead: {head_desc}"]
    elif base["kind"] == "ok" and kind in (VALUE, TYPE, NEW_EXC):
        expected = _literal(base["value"])
        if expected is None:
            return None
        rhs = f"pytest.approx({expected})" if _has_float(base["value"]) and kind != TYPE else expected
        lines = [f"    assert {call_src} == {rhs}  # head: {head_desc}"]
        if kind == TYPE:
            lines.append(f"    assert type({call_src}) is {base['type']}")
    else:
        return None
    needs_pytest = any("pytest." in ln for ln in lines)
    header = (["import pytest", ""] if needs_pytest else []) + [f"from {module} import {top}", "", ""]
    body = [f"def test_{slug}():",
            "    # Pins the merge-base behavior. If the change is intended, update the expectation."] + lines
    return "\n".join(header + body) + "\n"
