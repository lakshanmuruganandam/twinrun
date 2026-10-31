"""Find the Python functions whose behaviour a PR could have changed.

We compare syntax trees, not text. Formatting, comments, and docstrings are not
part of the comparison, so a reformat-only PR produces no candidates. A function
is a candidate when:
  * its own normalized AST differs between merge-base and head, or
  * the module-level code around it changed (constants, imports, globals), since
    that can change what an untouched function does.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist", ".tox", ".mypy_cache"}


@dataclass
class FuncInfo:
    qualname: str           # "f" or "Class.method"
    node: ast.FunctionDef | ast.AsyncFunctionDef
    kind: str               # "function" | "staticmethod" | "classmethod" | "method"
    fingerprint: str


@dataclass
class ModuleInfo:
    relpath: str            # path relative to the checkout root, posix style
    tree: ast.Module
    funcs: dict[str, FuncInfo] = field(default_factory=dict)
    context_fingerprint: str = ""


@dataclass
class Candidate:
    relpath: str
    qualname: str
    kind: str
    reason: str             # "body changed" | "module context changed"
    base: FuncInfo
    head: FuncInfo
    base_module: ModuleInfo
    head_module: ModuleInfo


@dataclass
class DiffResult:
    candidates: list[Candidate]
    added: list[str]          # functions that only exist at head
    removed: list[str]
    parse_errors: list[str]
    files_changed: list[str]  # .py files whose bytes differ


def _strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:] or [ast.Pass()]
    return body


def _fingerprint(node: ast.AST) -> str:
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def _func_fingerprint(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    clone = ast.parse(ast.unparse(fn)).body[0]
    assert isinstance(clone, (ast.FunctionDef, ast.AsyncFunctionDef))
    clone.body = _strip_docstring(clone.body)
    return _fingerprint(clone)


def _decorator_names(fn) -> set[str]:
    out = set()
    for d in fn.decorator_list:
        if isinstance(d, ast.Name):
            out.add(d.id)
        elif isinstance(d, ast.Attribute):
            out.add(d.attr)
    return out


def parse_module(relpath: str, source: str) -> ModuleInfo:
    tree = ast.parse(source, filename=relpath)
    info = ModuleInfo(relpath=relpath, tree=tree)
    context: list[ast.stmt] = []
    for stmt in _strip_docstring(list(tree.body)):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            info.funcs[stmt.name] = FuncInfo(stmt.name, stmt, "function", _func_fingerprint(stmt))
        elif isinstance(stmt, ast.ClassDef):
            class_ctx = []
            for sub in _strip_docstring(list(stmt.body)):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    decos = _decorator_names(sub)
                    kind = "staticmethod" if "staticmethod" in decos else \
                        "classmethod" if "classmethod" in decos else "method"
                    q = f"{stmt.name}.{sub.name}"
                    info.funcs[q] = FuncInfo(q, sub, kind, _func_fingerprint(sub))
                else:
                    class_ctx.append(sub)
            # Class-level attributes and bases count as context for that module.
            context.append(ast.ClassDef(name=stmt.name, bases=stmt.bases, keywords=stmt.keywords,
                                        body=class_ctx or [ast.Pass()], decorator_list=stmt.decorator_list,
                                        type_params=[]))
        else:
            context.append(stmt)
    info.context_fingerprint = _fingerprint(ast.Module(body=context, type_ignores=[]))
    return info


def iter_py_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


def is_test_file(relpath: str) -> bool:
    parts = Path(relpath).parts
    name = parts[-1]
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py" \
        or any(p in ("tests", "test") for p in parts[:-1])


def diff_trees(base_root: Path, head_root: Path, include_tests: bool = False) -> DiffResult:
    base_root, head_root = Path(base_root), Path(head_root)
    head_files = {p.relative_to(head_root).as_posix() for p in iter_py_files(head_root)}
    base_files = {p.relative_to(base_root).as_posix() for p in iter_py_files(base_root)}
    candidates: list[Candidate] = []
    added: list[str] = []
    removed: list[str] = []
    errors: list[str] = []
    changed_files: list[str] = []

    for rel in sorted(head_files | base_files):
        if not include_tests and is_test_file(rel):
            continue
        if rel not in base_files or rel not in head_files:
            side = head_files if rel in head_files else base_files
            root = head_root if rel in head_files else base_root
            try:
                mod = parse_module(rel, (root / rel).read_text(encoding="utf-8", errors="replace"))
                names = [f"{rel}::{q}" for q in mod.funcs]
            except SyntaxError:
                names = []
            (added if side is head_files else removed).extend(names)
            if rel in head_files:
                changed_files.append(rel)
            continue
        b_src = (base_root / rel).read_bytes()
        h_src = (head_root / rel).read_bytes()
        if b_src == h_src:
            continue
        changed_files.append(rel)
        try:
            bm = parse_module(rel, b_src.decode("utf-8", errors="replace"))
            hm = parse_module(rel, h_src.decode("utf-8", errors="replace"))
        except SyntaxError as e:
            errors.append(f"{rel}: {e.msg} (line {e.lineno})")
            continue
        ctx_changed = bm.context_fingerprint != hm.context_fingerprint
        for q, hf in hm.funcs.items():
            bf = bm.funcs.get(q)
            if bf is None:
                added.append(f"{rel}::{q}")
                continue
            if bf.fingerprint != hf.fingerprint:
                candidates.append(Candidate(rel, q, hf.kind, "body changed", bf, hf, bm, hm))
            elif ctx_changed:
                candidates.append(Candidate(rel, q, hf.kind, "module context changed", bf, hf, bm, hm))
        removed.extend(f"{rel}::{q}" for q in bm.funcs if q not in hm.funcs)
    return DiffResult(candidates, added, removed, errors, changed_files)


def import_target(root: Path, relpath: str) -> tuple[str, str]:
    """Return (import_root, dotted_module) for a file inside a checkout.

    Walk up through packages (dirs with __init__.py); the directory above the
    topmost package is the import root. Handles flat, package, and src/ layouts.
    """
    path = (Path(root) / relpath).resolve()
    parts = [path.stem]
    d = path.parent
    while (d / "__init__.py").exists() and d != Path(root).resolve().parent:
        parts.insert(0, d.name)
        d = d.parent
    if parts[-1] == "__init__":
        parts.pop()
    return str(d), ".".join(parts)
