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
        return all(trees_equal(x, y) for x, y in zip(a, b))
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


def show_outcome(o: dict, limit: int = 80) -> str:
    k = o["kind"]
    if k == "ok":
        s = show_tree(o["value"], limit)
    elif k == "exc":
        s = f"raises {o['type']}({o['msg']!r})" if o["msg"] else f"raises {o['type']}"
    elif k == "timeout":
        s = "timeout"
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
