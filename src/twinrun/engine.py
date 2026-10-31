"""Run the whole analysis for one PR: diff -> plan -> execute both sides -> group -> shrink."""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from hypothesis import HealthCheck, Phase, find, given, settings
from hypothesis.errors import NoSuchExample, Unsatisfiable

from . import compare as C
from .astdiff import Candidate, diff_trees, import_target
from .inputs import CallSiteIndex, Unsupported, build_plan
from .sandbox import Worker, WorkerDied, sandbox_kind

# Hypothesis is not designed to run concurrently in threads, so every
# generate/shrink call goes through this lock. Sandbox calls happen outside it.
_HYP_LOCK = threading.RLock()
_QUIET = list(HealthCheck)


@dataclass
class Group:
    kind: str
    count: int
    call: str                 # rendered smallest input, e.g. "f(3, b=2)"
    base: str
    head: str
    shrunk: bool              # True when the input is Hypothesis' minimal example
    raw_input: tuple = field(default=((), {}), repr=False)
    test: str | None = None   # ready-to-paste pytest regression test, when expressible


@dataclass
class FunctionResult:
    file: str
    qualname: str
    status: str               # "compared" | "skipped"
    reason: str = ""          # why it was skipped / why it was a candidate
    inputs: int = 0
    groups: list[Group] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def diverged(self) -> bool:
        return bool(self.groups)


@dataclass
class Report:
    functions: list[FunctionResult]
    files_changed: list[str]
    added: list[str]
    removed: list[str]
    parse_errors: list[str]
    seconds: float
    sandbox: str

    @property
    def divergent(self) -> list[FunctionResult]:
        return [f for f in self.functions if f.diverged]

    @property
    def compared(self) -> list[FunctionResult]:
        return [f for f in self.functions if f.status == "compared"]

    @property
    def skipped(self) -> list[FunctionResult]:
        return [f for f in self.functions if f.status == "skipped"]


@dataclass
class Options:
    max_examples: int = 150
    call_timeout: float = 1.0
    jobs: int = 8
    shrink: bool = True
    shrink_examples: int = 250


def draw_inputs(strategy, n: int) -> list:
    out: list = []

    @settings(max_examples=n, database=None, derandomize=True, phases=[Phase.generate],
              deadline=None, suppress_health_check=_QUIET)
    @given(strategy)
    def collect(x):
        out.append(x)

    with _HYP_LOCK:
        collect()
    return out


def _dedupe(calls: list) -> list:
    seen, out = set(), []
    for c in calls:
        r = repr(c)
        if r not in seen:
            seen.add(r)
            out.append(c)
    return out


def analyze_candidate(cand: Candidate, base_root: Path, head_root: Path,
                      index: CallSiteIndex, opts: Options) -> FunctionResult:
    t0 = time.monotonic()
    res = FunctionResult(cand.relpath, cand.qualname, "compared", reason=cand.reason)
    try:
        plan = build_plan(cand, index)
    except Unsupported as e:
        res.status, res.reason = "skipped", str(e)
        return res
    res.notes.extend(plan.notes)
    drawn = draw_inputs(plan.strategy, opts.max_examples)
    templates = list(plan.seeds) + drawn
    calls = _dedupe(list(plan.seeds) + drawn + plan.boundary_sweep(templates))

    b_root, b_mod = import_target(base_root, cand.relpath)
    h_root, h_mod = import_target(head_root, cand.relpath)
    base = Worker(b_root, b_mod, cand.qualname, opts.call_timeout)
    head = Worker(h_root, h_mod, cand.qualname, opts.call_timeout)
    starters = [threading.Thread(target=w.start) for w in (base, head)]
    for t in starters:
        t.start()
    for t in starters:
        t.join()
    try:
        for side, w in (("base", base), ("head", head)):
            if w.import_error:
                res.status = "skipped"
                res.reason = f"could not import at {side}: {w.import_error}"
                return res

        groups: dict[str, list] = {}
        blocked: list[str] = []
        timeouts_both = 0
        timeouts_one_side = 0
        for call in calls:
            args, kwargs = call
            try:
                bo = base.call(args, kwargs)
                ho = head.call(args, kwargs)
            except WorkerDied as e:
                res.status, res.reason = "skipped", f"worker failed: {e}"
                return res
            res.inputs += 1
            for o in (bo, ho):
                if o["kind"] == "blocked":
                    blocked.append(o["what"])
            if bo["kind"] == ho["kind"] == "timeout":
                timeouts_both += 1
                if timeouts_both >= 5 and timeouts_both == res.inputs:
                    break  # everything hangs on both sides; stop burning time
            kind = C.classify(bo, ho)
            if kind:
                groups.setdefault(kind, []).append((call, bo, ho))
            if kind == C.TIMEOUT:
                timeouts_one_side += 1
                if timeouts_one_side >= 3:  # proven; every further probe costs a full timeout
                    res.notes.append(f"stopped after {timeouts_one_side} timeouts on one side "
                                     f"({res.inputs}/{len(calls)} inputs run)")
                    break

        if blocked and len(blocked) >= res.inputs:
            res.status = "skipped"
            res.reason = f"I/O-heavy: the sandbox blocked {blocked[0]}"
            res.groups = []
            return res
        if blocked:
            res.notes.append(f"sandbox blocked {len(blocked)} call(s), e.g. {blocked[0]}; those inputs were ignored")
        if timeouts_both:
            res.notes.append(f"{timeouts_both} input(s) timed out on both sides")

        name = cand.qualname
        for kind in sorted(groups, key=C.KIND_ORDER.index):
            items = groups[kind]
            smallest = min(items, key=lambda it: C.input_size(_strip_defaults(plan, it[0])))
            shrunk_call, shrunk = _strip_defaults(plan, smallest[0]), False
            if opts.shrink and kind != C.TIMEOUT:  # each timeout probe costs a full second
                found = _shrink(plan.strategy, base, head, kind, opts.shrink_examples)
                found = _strip_defaults(plan, found) if found is not None else None
                # Hypothesis' result is minimal under its own ordering; keep it unless what we
                # already saw is strictly simpler by repr length.
                if found is not None and C.input_size(found)[0] <= C.input_size(shrunk_call)[0]:
                    shrunk_call, shrunk = found, True
            bo = base.call(*shrunk_call)
            ho = head.call(*shrunk_call)
            if C.classify(bo, ho) != kind:  # flaky function: fall back to what we observed
                shrunk_call, bo, ho, shrunk = smallest[0], smallest[1], smallest[2], False
            res.groups.append(Group(kind, len(items), C.show_call(name, *shrunk_call),
                                    C.show_outcome(bo, kind=kind), C.show_outcome(ho, kind=kind),
                                    shrunk, shrunk_call,
                                    C.regression_test(h_mod, name, shrunk_call, bo, ho, kind)))
        return res
    finally:
        base.close()
        head.close()
        res.seconds = round(time.monotonic() - t0, 2)


def _strip_defaults(plan, call):
    """Drop arguments that only repeat the parameter's literal default: f(40.0, False) -> f(40.0)."""
    args, kwargs = list(call[0]), dict(call[1])
    pos = [p for p in plan.params if p.kind == "pos"]

    def is_default(p, v):
        return p.has_default and type(v) is type(p.default) and v == p.default

    while args and len(args) <= len(pos) and is_default(pos[len(args) - 1], args[-1]) and not kwargs:
        args.pop()
    by_name = {p.name: p for p in plan.params}
    kwargs = {k: v for k, v in kwargs.items() if not (k in by_name and is_default(by_name[k], v))}
    return (tuple(args), kwargs)


def _shrink(strategy, base: Worker, head: Worker, kind: str, budget: int):
    """Ask Hypothesis for the simplest input that still shows this kind of divergence."""

    def still_diverges(call) -> bool:
        args, kwargs = call
        return C.classify(base.call(args, kwargs), head.call(args, kwargs)) == kind

    with _HYP_LOCK:
        try:
            return find(strategy, still_diverges,
                        settings=settings(max_examples=budget, database=None, deadline=None,
                                          suppress_health_check=_QUIET,
                                          phases=[Phase.generate, Phase.shrink]),
                        random=random.Random(0))
        except (NoSuchExample, Unsatisfiable):
            return None


def analyze(base_root: str | Path, head_root: str | Path, opts: Options | None = None,
            log=None) -> Report:
    opts = opts or Options()
    t0 = time.monotonic()
    base_root, head_root = Path(base_root).resolve(), Path(head_root).resolve()
    diff = diff_trees(base_root, head_root)
    index = CallSiteIndex(head_root)
    results: list[FunctionResult] = []

    def run(c: Candidate) -> FunctionResult:
        r = analyze_candidate(c, base_root, head_root, index, opts)
        if log:
            log(f"{r.file}::{r.qualname}: {r.status} {len(r.groups)} divergence group(s) in {r.seconds}s")
        return r

    with ThreadPoolExecutor(max_workers=max(1, opts.jobs)) as pool:
        results = list(pool.map(run, diff.candidates))
    return Report(results, diff.files_changed, diff.added, diff.removed, diff.parse_errors,
                  round(time.monotonic() - t0, 2), sandbox_kind())
