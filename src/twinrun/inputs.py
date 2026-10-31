"""Build Hypothesis strategies for a function without importing the code under test.

Sources of information, strongest first:
  1. Type hints on the parameters (parsed from the AST, never evaluated).
  2. Literal default values.
  3. Call sites and tests elsewhere in the repo: literal arguments give both the
     parameter types (for unhinted functions) and concrete seed calls.
  4. Constants that appear in the function body at base or head (and in the
     module's top-level assignments). Branch boundaries live there, so for an
     int constant c we also try c-1 and c+1. That is how a flipped `>`/`>=` or
     an off-by-one gets hit without luck.
"""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass, field
from pathlib import Path

from hypothesis import strategies as st

from .astdiff import Candidate, iter_py_files

INT_RANGE = 10_000
MAX_COLL = 8

SCALARS = {"int", "float", "str", "bool", "bytes", "None", "NoneType", "complex"}
GENERICS = {"list", "List", "Sequence", "Iterable", "MutableSequence", "Collection",
            "set", "Set", "frozenset", "FrozenSet", "AbstractSet",
            "dict", "Dict", "Mapping", "MutableMapping",
            "tuple", "Tuple", "Optional", "Union"}
IO_NAMES = {"open", "input", "urlopen", "requests", "httpx", "socket", "subprocess", "urllib",
            "shutil", "sqlite3", "boto3", "aiohttp", "smtplib", "ftplib", "pathlib"}
IO_ATTRS = {"remove", "unlink", "rmdir", "makedirs", "mkdir", "rename", "system", "popen",
            "read_text", "write_text", "read_bytes", "write_bytes", "urlopen", "connect",
            "get", "post", "put", "delete", "request"}
IO_ANY_ATTRS = {"read_text", "write_text", "read_bytes", "write_bytes", "unlink", "urlopen",
                "makedirs", "rmtree", "recv", "sendall"}
IO_ATTR_OWNERS = {"os", "shutil", "requests", "httpx", "socket", "subprocess", "urllib", "Path", "path"}


class Unsupported(Exception):
    """Raised when we cannot build inputs for a parameter; carries a human reason."""


@dataclass
class Param:
    name: str
    kind: str                      # "pos" | "kwonly"
    annotation: ast.expr | None
    default: object = None
    has_default: bool = False


@dataclass
class Plan:
    params: list[Param]
    strategy: st.SearchStrategy     # draws (args_tuple, kwargs_dict)
    seeds: list[tuple[tuple, dict]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    consts: Constants | None = None

    def boundary_sweep(self, templates: list[tuple[tuple, dict]], per_template: int = 3) -> list:
        """Deterministic boundary probes: for each template call, replace one numeric
        argument at a time with every constant-derived boundary value (c-1, c, c+1)."""
        if self.consts is None:
            return []
        ints, floats = self.consts.int_candidates(), self.consts.float_candidates()
        out = []
        for args, kwargs in templates[:per_template]:
            for i, v in enumerate(args):
                pool = ints if type(v) is int else floats if type(v) is float else None
                for c in pool or ():
                    out.append((args[:i] + (c,) + args[i + 1:], dict(kwargs)))
            for k, v in kwargs.items():
                pool = ints if type(v) is int else floats if type(v) is float else None
                for c in pool or ():
                    out.append((tuple(args), {**kwargs, k: c}))
        return out


# --------------------------------------------------------------------- params

def signature_params(fn: ast.FunctionDef | ast.AsyncFunctionDef, drop_first: bool) -> list[Param]:
    a = fn.args
    positional = list(a.posonlyargs) + list(a.args)
    defaults: list = [None] * (len(positional) - len(a.defaults)) + list(a.defaults)
    out: list[Param] = []
    for i, (arg, d) in enumerate(zip(positional, defaults, strict=True)):
        if drop_first and i == 0:
            continue
        p = Param(arg.arg, "pos", arg.annotation)
        if d is not None:
            p.has_default = True
            try:
                p.default = ast.literal_eval(d)
            except (ValueError, SyntaxError, TypeError):
                p.default = _NonLiteral
        out.append(p)
    for arg, d in zip(a.kwonlyargs, a.kw_defaults, strict=True):
        p = Param(arg.arg, "kwonly", arg.annotation)
        if d is not None:
            p.has_default = True
            try:
                p.default = ast.literal_eval(d)
            except (ValueError, SyntaxError, TypeError):
                p.default = _NonLiteral
        out.append(p)
    return out


class _NonLiteral:
    """Default value that isn't a literal (e.g. a module constant). We omit the arg."""


# --------------------------------------------------------------------- constants

@dataclass
class Constants:
    ints: set = field(default_factory=set)
    floats: set = field(default_factory=set)
    strs: set = field(default_factory=set)

    def add_node(self, node: ast.AST) -> None:
        for n in ast.walk(node):
            if isinstance(n, ast.Constant):
                v = n.value
                if isinstance(v, bool):
                    continue
                if isinstance(v, int) and abs(v) <= 10**9:
                    self.ints.add(v)
                elif isinstance(v, float) and math.isfinite(v):
                    self.floats.add(v)
                elif isinstance(v, str) and len(v) <= 40:
                    self.strs.add(v)
            elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub) and isinstance(n.operand, ast.Constant):
                v = n.operand.value
                if isinstance(v, int) and not isinstance(v, bool) and abs(v) <= 10**9:
                    self.ints.add(-v)
                elif isinstance(v, float) and math.isfinite(v):
                    self.floats.add(-v)

    def int_candidates(self) -> list[int]:
        out = {0, 1, -1}
        for c in self.ints:
            out.update((c - 1, c, c + 1))
        return sorted(out, key=_simplicity)

    def float_candidates(self) -> list[float]:
        out = {0.0, 1.0, -1.0, 0.5}
        for c in self.floats:
            out.update((c, math.nextafter(c, math.inf), math.nextafter(c, -math.inf), c + 0.5, c - 0.5))
        for c in self.ints:
            out.update((float(c), c + 0.5, c - 0.5))
        return sorted(out, key=_simplicity)

    def str_candidates(self) -> list[str]:
        out = {"", "a", " ", "A b"}
        for c in self.strs:
            out.update((c, c.upper(), c + "x", " " + c))
        return sorted(out, key=lambda v: (len(v), v))


def _simplicity(v):
    """Hypothesis shrinks sampled_from toward index 0, so put the simplest values first:
    0, 1, -1, 2, -2, ... (ties broken positive-first)."""
    return (abs(v), v < 0, v)


def collect_constants(cand: Candidate) -> Constants:
    c = Constants()
    for fn, mod in ((cand.base.node, cand.base_module), (cand.head.node, cand.head_module)):
        c.add_node(fn)
        for stmt in mod.tree.body:
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                c.add_node(stmt)
    return c


# --------------------------------------------------------------------- types -> strategies

def _ann_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and node.value is None:
        return "None"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value  # string annotation like "int"
    return ""


def strategy_for(node: ast.expr | None, consts: Constants, depth: int = 0) -> st.SearchStrategy:
    if node is None:
        raise Unsupported("no type hint")
    if depth > 4:
        raise Unsupported("type too deeply nested")
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value, mode="eval").body
        except SyntaxError as e:
            raise Unsupported(f"unparseable annotation {node.value!r}") from e
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return st.one_of(strategy_for(node.left, consts, depth + 1), strategy_for(node.right, consts, depth + 1))
    if isinstance(node, ast.Subscript):
        base = _ann_name(node.value)
        sl = node.slice
        elts = list(sl.elts) if isinstance(sl, ast.Tuple) else [sl]
        if base == "Optional":
            return st.one_of(st.none(), strategy_for(elts[0], consts, depth + 1))
        if base == "Union":
            return st.one_of(*[strategy_for(e, consts, depth + 1) for e in elts])
        if base in ("list", "List", "Sequence", "Iterable", "MutableSequence", "Collection"):
            return st.lists(strategy_for(elts[0], consts, depth + 1), max_size=MAX_COLL)
        if base in ("set", "Set", "AbstractSet"):
            return st.sets(_hashable(strategy_for(elts[0], consts, depth + 1)), max_size=MAX_COLL)
        if base in ("frozenset", "FrozenSet"):
            return st.frozensets(_hashable(strategy_for(elts[0], consts, depth + 1)), max_size=MAX_COLL)
        if base in ("dict", "Dict", "Mapping", "MutableMapping"):
            if len(elts) != 2:
                raise Unsupported("dict hint needs key and value types")
            return st.dictionaries(_hashable(strategy_for(elts[0], consts, depth + 1)),
                                   strategy_for(elts[1], consts, depth + 1), max_size=6)
        if base in ("tuple", "Tuple"):
            if len(elts) == 2 and isinstance(elts[1], ast.Constant) and elts[1].value is Ellipsis:
                return st.lists(strategy_for(elts[0], consts, depth + 1), max_size=MAX_COLL).map(tuple)
            return st.tuples(*[strategy_for(e, consts, depth + 1) for e in elts])
        raise Unsupported(f"unsupported generic type {ast.unparse(node)}")
    name = _ann_name(node)
    if name in ("list", "List"):
        return st.lists(_any_scalar(consts), max_size=MAX_COLL)
    if name in ("dict", "Dict"):
        return st.dictionaries(st.text(max_size=6), _any_scalar(consts), max_size=6)
    return scalar_strategy(name, consts)


def scalar_strategy(name: str, consts: Constants) -> st.SearchStrategy:
    if name == "int":
        return st.one_of(st.sampled_from(consts.int_candidates()),
                         st.integers(-INT_RANGE, INT_RANGE))
    if name == "float":
        return st.one_of(st.sampled_from(consts.float_candidates()),
                         st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False),
                         st.integers(-1000, 1000).map(lambda i: i / 4))
    if name == "str":
        return st.one_of(st.sampled_from(consts.str_candidates()), st.text(max_size=12))
    if name == "bool":
        return st.booleans()
    if name == "bytes":
        return st.binary(max_size=12)
    if name in ("None", "NoneType"):
        return st.none()
    if name == "complex":
        return st.complex_numbers(max_magnitude=1e6, allow_nan=False, allow_infinity=False)
    raise Unsupported(f"type {name or '?'} is not a built-in we can generate")


def _any_scalar(consts: Constants) -> st.SearchStrategy:
    return st.one_of(scalar_strategy("int", consts), scalar_strategy("str", consts))


def _hashable(s: st.SearchStrategy) -> st.SearchStrategy:
    return s.filter(lambda v: _is_hashable(v))


def _is_hashable(v) -> bool:
    try:
        hash(v)
        return True
    except TypeError:
        return False


def strategy_from_values(values: list, consts: Constants) -> st.SearchStrategy:
    """For an unhinted parameter: generate values of the types seen at call sites."""
    kinds = []
    for v in values:
        t = type(v).__name__
        if t == "NoneType":
            t = "None"
        if t not in kinds:
            kinds.append(t)
    parts = [st.sampled_from(_dedupe(values))]
    for k in kinds:
        if k in SCALARS:
            parts.append(scalar_strategy(k, consts))
        elif k in ("list", "tuple", "set", "dict"):
            inner = [x for v in values if type(v).__name__ == k for x in (v.items() if k == "dict" else v)]
            if k == "dict":
                ks = [a for a, _ in inner]
                vs = [b for _, b in inner]
                if ks and vs:
                    parts.append(st.dictionaries(strategy_from_values(ks, consts), strategy_from_values(vs, consts),
                                                 max_size=6))
            elif inner:
                el = strategy_from_values(inner, consts)
                coll = st.lists(el, max_size=MAX_COLL)
                parts.append(coll.map(tuple) if k == "tuple" else coll.map(set) if k == "set" else coll)
    return st.one_of(*parts)


def _dedupe(values: list) -> list:
    out, seen = [], set()
    for v in values:
        r = (type(v).__name__, repr(v))
        if r not in seen:
            seen.add(r)
            out.append(v)
    return out


# --------------------------------------------------------------------- call sites

@dataclass
class CallSites:
    positional: dict[int, list] = field(default_factory=dict)
    keyword: dict[str, list] = field(default_factory=dict)
    whole: list[tuple[tuple, dict]] = field(default_factory=list)
    count: int = 0


class CallSiteIndex:
    """Index of every call in a checkout, keyed by the called name."""

    def __init__(self, root: Path):
        self.calls: dict[str, list[ast.Call]] = {}
        for path in iter_py_files(Path(root)):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except (SyntaxError, ValueError):
                continue
            for n in ast.walk(tree):
                if isinstance(n, ast.Call):
                    name = n.func.id if isinstance(n.func, ast.Name) else \
                        n.func.attr if isinstance(n.func, ast.Attribute) else None
                    if name:
                        self.calls.setdefault(name, []).append(n)

    def sites(self, func_name: str, params: list[Param]) -> CallSites:
        cs = CallSites()
        names = [p.name for p in params]
        for call in self.calls.get(func_name, []):
            if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
                continue
            args, kwargs, ok = [], {}, True
            for i, a in enumerate(call.args):
                try:
                    v = ast.literal_eval(a)
                except (ValueError, SyntaxError, TypeError):
                    ok = False
                    continue
                cs.positional.setdefault(i, []).append(v)
                args.append(v)
            for k in call.keywords:
                try:
                    v = ast.literal_eval(k.value)
                except (ValueError, SyntaxError, TypeError):
                    ok = False
                    continue
                cs.keyword.setdefault(k.arg, []).append(v)
                kwargs[k.arg] = v
            if len(call.args) > len([p for p in params if p.kind == "pos"]):
                continue
            if any(k not in names for k in kwargs):
                continue
            cs.count += 1
            if ok:
                cs.whole.append((tuple(args), kwargs))
        return cs


# --------------------------------------------------------------------- plan

def static_io_reason(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name) and f.id in IO_NAMES:
                return f"calls {f.id}()"
            if isinstance(f, ast.Attribute):
                owner = f.value.id if isinstance(f.value, ast.Name) else \
                    f.value.attr if isinstance(f.value, ast.Attribute) else ""
                if owner in IO_ATTR_OWNERS and f.attr in IO_ATTRS:
                    return f"calls {owner}.{f.attr}()"
                if f.attr in IO_ANY_ATTRS:
                    return f"calls .{f.attr}()"
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""]
            for m in mods:
                if m.split(".")[0] in IO_NAMES:
                    return f"imports {m}"
    return None


def build_plan(cand: Candidate, index: CallSiteIndex) -> Plan:
    """Raise Unsupported(reason) when the function can't be driven safely."""
    if cand.kind == "method":
        raise Unsupported("instance method: needs an object to call it on")
    head_fn, base_fn = cand.head.node, cand.base.node
    a = head_fn.args
    if a.vararg is not None and not (a.args or a.posonlyargs or a.kwonlyargs):
        raise Unsupported("only takes *args")
    for fn in (head_fn, base_fn):
        r = static_io_reason(fn)
        if r:
            raise Unsupported(f"I/O-heavy: {r}")

    params = signature_params(head_fn, drop_first=cand.kind == "classmethod")
    base_params = signature_params(base_fn, drop_first=cand.kind == "classmethod")
    consts = collect_constants(cand)
    sites = index.sites(cand.qualname.split(".")[-1], params)
    notes: list[str] = []
    if [p.name for p in params] != [p.name for p in base_params]:
        notes.append("signature changed: "
                     f"({', '.join(p.name for p in base_params)}) → ({', '.join(p.name for p in params)}); "
                     "calling both with head's parameter positions")

    strategies_pos, strategies_kw = [], {}
    inferred = []
    omitted_pos = False
    for i, p in enumerate(params):
        s = None
        if p.annotation is not None:
            try:
                s = strategy_for(p.annotation, consts)
            except Unsupported as e:
                if not p.has_default:
                    raise Unsupported(f"parameter '{p.name}': {e}") from e
        if s is None:
            observed = (sites.positional.get(i, []) if p.kind == "pos" else []) + sites.keyword.get(p.name, [])
            if observed:
                s = strategy_from_values(observed, consts)
                inferred.append(p.name)
            elif p.has_default and p.default is not _NonLiteral:
                dt = type(p.default).__name__
                s = st.just(p.default)
                if dt in SCALARS and dt != "NoneType":
                    s = st.one_of(st.just(p.default), scalar_strategy(dt, consts))
            elif p.has_default:
                s = None  # leave it out, the function's own default applies
            else:
                raise Unsupported(f"parameter '{p.name}' has no type hint, no literal default, "
                                  "and no call site with a literal argument to infer its type from")
        elif p.has_default and p.default is not _NonLiteral:
            s = st.one_of(st.just(p.default), s)
        if s is None:
            omitted_pos = omitted_pos or p.kind == "pos"
            continue
        if p.kind == "pos" and not omitted_pos:
            strategies_pos.append(s)
        else:  # keyword-only, or a positional after one we left out
            strategies_kw[p.name] = s
    if inferred:
        notes.append(f"types inferred from {sites.count} call site(s) for: {', '.join(inferred)}")

    strategy = st.tuples(st.tuples(*strategies_pos), st.fixed_dictionaries(strategies_kw))
    return Plan(params=params, strategy=strategy, seeds=_dedupe_calls(sites.whole), notes=notes, consts=consts)


def _dedupe_calls(calls):
    out, seen = [], set()
    for c in calls:
        r = repr(c)
        if r not in seen:
            seen.add(r)
            out.append(c)
    return out
