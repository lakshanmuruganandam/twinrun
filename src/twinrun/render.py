"""Turn a Report into the check-run output and the (single, updated-in-place) PR comment."""

from __future__ import annotations

from dataclasses import dataclass

from .engine import Report

MARKER = "<!-- twinrun:behavior-diff -->"
LOCK_LABELS = {"refactor", "no-behavior-change"}
CHECK_NAME = "Behavior Diff"


@dataclass
class Verdict:
    conclusion: str   # "success" | "neutral" | "failure"
    title: str
    locked: bool


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def verdict(report: Report, labels: set[str] | list[str]) -> Verdict:
    locked = bool(LOCK_LABELS & {lbl.lower() for lbl in labels})
    n = len(report.divergent)
    groups = sum(len(f.groups) for f in report.functions)
    if n == 0:
        title = f"No behavior change in {plural(len(report.compared), 'compared function')}"
        if not report.compared:
            title = "No changed functions could be compared"
        return Verdict("success", title, locked)
    title = (f"{plural(n, 'function')} {'behaves' if n == 1 else 'behave'} differently "
             f"({plural(groups, 'kind')} of divergence)")
    if locked:
        return Verdict("failure", "Behavior lock: " + title, locked)
    return Verdict("neutral", title, locked)


def _cell(s: str) -> str:
    s = s.replace("\r", " ").replace("\n", "⏎").replace("|", "\\|").replace("`", "ˋ")
    return f"`{s}`" if s else " "


STATUS = {  # words first: emoji alone are announced oddly by screen readers
    "success": ("✅", "Passed"),
    "neutral": ("ℹ️", "For review"),
    "failure": ("❌", "Failed"),
}


def table(report: Report) -> str:
    rows = ["| Function | What changed | Smallest input | Before (base) | After (head) |",
            "|---|---|---|---|---|"]
    for f in report.divergent:
        for g in f.groups:
            note = "" if g.shrunk else " (not shrunk)"
            seen = plural(g.count, "input")
            rows.append(f"| `{f.qualname}` | {g.kind.capitalize()} ({seen}) | {_cell(g.call)}{note} | "
                        f"{_cell(g.base)} | {_cell(g.head)} |")
    return "\n".join(rows)


def merge_tests(tests: list[str]) -> str:
    """Combine several generated test modules into one file: imports once at the top, then the tests."""
    imports: list[str] = []
    bodies: list[str] = []
    for t in tests:
        lines = t.strip("\n").splitlines()
        i = 0
        while i < len(lines) and (lines[i].startswith(("import ", "from ")) or not lines[i].strip()):
            if lines[i].strip() and lines[i] not in imports:
                imports.append(lines[i])
            i += 1
        bodies.append("\n".join(lines[i:]).rstrip())
    imports.sort(key=lambda ln: (not ln.startswith("import "), ln))
    head = [ln for ln in imports if ln.startswith("import ")]
    froms = [ln for ln in imports if ln.startswith("from ")]
    top = "\n".join(head) + ("\n\n" if head and froms else "") + "\n".join(froms)
    return top + "\n\n\n" + "\n\n\n".join(bodies)


def tests_block(report: Report) -> str:
    tests = [g.test for f in report.divergent for g in f.groups if g.test]
    if not tests:
        return ""
    code = merge_tests(tests)
    return ("<details><summary>Regression tests for these inputs "
            f"({plural(len(tests), 'test')}, ready to paste)</summary>\n\n"
            "Each test pins the **base** behavior, so it passes before this PR and fails after it. "
            "Keep the ones for changes you did not intend.\n\n"
            f"```python\n{code}\n```\n\n</details>")


def body(report: Report, v: Verdict, *, head_sha: str = "", base_sha: str = "") -> str:
    lines: list[str] = []
    icon, word = STATUS[v.conclusion]
    lines.append(f"### {icon} TwinRun: {word}. {v.title}")
    if base_sha and head_sha:
        lines.append(f"Every changed function ran at merge-base `{base_sha[:7]}` and at head `{head_sha[:7]}` "
                     "on the same generated inputs, inside a sandbox.")
    if v.locked:
        lines.append("")
        lines.append("> [!IMPORTANT]\n> **Behavior lock is on** (label `refactor` or `no-behavior-change`), "
                     "so any difference fails this check.")
    lines.append("")
    if report.divergent:
        lines.append(table(report))
        lines.append("")
        lines.append("<sub>Each input was shrunk to the simplest one Hypothesis could find, unless it is marked "
                     "(not shrunk). The count is how many generated inputs showed that difference.</sub>")
        lines.append("")
        block = tests_block(report)
        if block:
            lines.append(block)
            lines.append("")
    stats = (f"**{len(report.compared)}** compared · **{len(report.divergent)}** changed behavior · "
             f"**{len(report.skipped)}** skipped · "
             f"{sum(f.inputs for f in report.compared)} inputs in {report.seconds}s · sandbox: {report.sandbox}")
    lines.append(stats)
    same = [f for f in report.compared if not f.diverged]
    if same:
        lines.append("")
        lines.append("<details><summary>Same behavior on every input "
                     f"({len(same)})</summary>\n")
        for f in same:
            lines.append(f"- `{f.file}::{f.qualname}` — {f.inputs} inputs ({f.reason})")
        lines.append("\n</details>")
    if report.skipped:
        lines.append("")
        lines.append(f"<details><summary>Skipped ({len(report.skipped)}) — not checked, so not covered "
                     "by this report</summary>\n")
        for f in report.skipped:
            lines.append(f"- `{f.file}::{f.qualname}` — {f.reason}")
        lines.append("\n</details>")
    notes = [(f, n) for f in report.functions for n in f.notes]
    if notes or report.parse_errors:
        lines.append("")
        lines.append("<details><summary>Notes</summary>\n")
        for f, n in notes:
            lines.append(f"- `{f.qualname}`: {n}")
        for e in report.parse_errors:
            lines.append(f"- syntax error, file not analyzed: {e}")
        lines.append("\n</details>")
    return "\n".join(lines)


def comment(report: Report, v: Verdict, **kw) -> str:
    return MARKER + "\n" + body(report, v, **kw)


def check_output(report: Report, v: Verdict, **kw) -> dict:
    text = body(report, v, **kw)
    if len(text) > 65000:  # GitHub's limit for check-run summary
        text = text[:64000] + "\n\n…truncated"
    return {"title": v.title[:250], "summary": text}
